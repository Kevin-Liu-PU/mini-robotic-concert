"""Single-threaded, dependency-free MOCK bow; no hardware adapter or queue.

All times share the app's monotonic clock (or a synthetic clock in tests).
Beat timestamps identify observations; Beat.count can restart after a reset.
"""
import math


class MockCello:
    def __init__(self, duration_s=.5, max_event_age_s=.25):
        if not all(math.isfinite(v) and v > 0 for v in (duration_s, max_event_age_s)):
            raise ValueError('Mock timings must be finite and positive')
        self.duration_s = duration_s
        self.max_event_age_s = max_event_age_s
        self.state = 'DISARMED'
        self.reason = 'STARTUP'
        self.fault = None
        self.started = self.completed = self.dropped = 0
        self._last_now = self._last_event = -math.inf
        self._armed_at = self._due = None

    def fail(self, reason):
        self.state = 'FAULT'
        self.fault = self.fault or reason
        self.reason = self.fault
        self._armed_at = self._due = None

    def _time(self, now):
        if not math.isfinite(now) or now < self._last_now:
            self.fail('INVALID_MONOTONIC_TIME')
            return False
        self._last_now = now
        return True

    def stop(self, reason='STOP'):
        """Cancel this simulated deadline; stop never clears a latched fault."""
        self._armed_at = self._due = None
        if self.state != 'FAULT':
            self.state, self.reason = 'DISARMED', reason

    def reset(self, now):
        if self._time(now):
            self.fault = None
            self.state = 'DISARMED'
            self.stop('RESET')

    def arm(self, now):
        if not self._time(now) or self.state != 'DISARMED':
            return False
        self._armed_at = now
        self.state, self.reason = 'ARMED', 'WAITING_FOR_FRESH_OBSERVED_BEAT'
        return True

    def offer(self, event, now):
        """Offer ONLY an observed Beat, before tick; a BUSY arrival is dropped.

        Rejected observations are never queued. The timestamp watermark survives
        stop/reset/re-arm, including observations dropped while BUSY or DISARMED.
        """
        if not self._time(now):
            return False
        stamp = event.t
        reason = None
        if not math.isfinite(stamp) or stamp > now:
            reason = 'INVALID_EVENT_TIME'
        elif stamp <= self._last_event:
            reason = 'DUPLICATE_OR_REORDERED'
        else:
            self._last_event = stamp
            if self.state != 'ARMED':
                reason = self.state
            elif stamp <= self._armed_at:
                reason = 'PRE_ARM'
            elif now-stamp > self.max_event_age_s:
                reason = 'STALE'
        if reason:
            self.dropped += 1
            if self.state != 'FAULT':
                self.reason = 'DROPPED_' + reason
            return False
        # Consume authorization BEFORE starting even the mock operation.
        self.state, self.reason = 'BUSY', 'MOCK_BOW'
        self._armed_at = None
        try:
            self._start(now)
        except Exception as exc:
            self.fail(f'MOCK_START_ERROR: {type(exc).__name__}: {exc}')
            return False
        self.started += 1
        return True

    def _start(self, now):
        self._due = now + self.duration_s

    def tick(self, now):
        """Poll a deadline; never sleep, spawn work, or call a device."""
        if not self._time(now) or self.state != 'BUSY':
            return
        try:
            if now >= self._due:
                self.completed += 1
                self.stop('MOCK_COMPLETED')
        except Exception as exc:
            self.fail(f'MOCK_COMPLETION_ERROR: {type(exc).__name__}: {exc}')

    def snapshot(self):
        return dict(mode='MOCK', state=self.state, reason=self.reason,
                    fault=self.fault, started=self.started, completed=self.completed,
                    dropped=self.dropped, queued=0)


class MockCelloControls:
    """App seam: observed MAIN frames + A then B for each one-shot authorization.

    Two different keys prevent held-key repeats from re-arming after completion.
    Neither Enter nor clock predictions can authorize or start a bow.
    """
    def __init__(self):
        self.bow = MockCello()
        self._frame_time = None
        self._token = None
        self._confirm = False

    def stop(self, reason='STOP'):
        self._confirm = False
        self.bow.stop(reason)

    def _fresh(self, now):
        return (self._frame_time is not None and self._token is not None
                and 0 <= now-self._frame_time <= self.bow.max_event_age_s)

    def step(self, now, *, sample, event, source_token):
        # A fresh frame after a loop/camera gap cannot revive old authorization.
        if not self._fresh(now):
            self.stop('SOURCE_NOT_FRESH')
        if source_token != self._token:
            self.stop('MAIN_CHANGED')
            self._frame_time = None
            self._token = source_token
        if sample is not None:
            stamp, x, y = sample
            if (x is None or y is None or not all(math.isfinite(v) for v in sample)
                    or (self._frame_time is not None and stamp <= self._frame_time)):
                self._frame_time = None
                self.stop('HAND_LOST_OR_INVALID_FRAME')
            else:
                self._frame_time = stamp
        if not self._fresh(now):
            self.stop('SOURCE_NOT_FRESH')
        if event is not None:
            self.bow.offer(event, now)
        self.bow.tick(now)

    def key(self, key, now):
        # Invalidate on every control which changes the detector's context.
        if key in (ord('r'), ord('R')):
            self.stop('RESET')
            self.bow.reset(now)
            self._frame_time = None
        elif key in (32, 10, 13, 27, ord('q'), ord('Q'), ord('1'), ord('2'),
                     ord('w'), ord('W'), ord('s'), ord('S'), ord('x'), ord('X')):
            self.stop('CONTROL_STOP')
            self._frame_time = None
        elif key in (ord('a'), ord('A')):
            self._confirm = self.bow.state == 'DISARMED' and self._fresh(now)
        elif key in (ord('b'), ord('B')):
            confirm, self._confirm = self._confirm, False
            if confirm and self._fresh(now):
                return self.bow.arm(now)
        return False

    def snapshot(self):
        return dict(self.bow.snapshot(), arm_confirmation_pending=self._confirm)

    def label(self):
        return (f"CELLO MOCK | {self.bow.state} | bows {self.bow.started} | "
                + ('press B to confirm ONE bow' if self._confirm else 'A then B: arm ONE bow')
                + ' | X/Space: stop | R: reset')
