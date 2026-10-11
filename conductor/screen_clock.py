"""Opt-in screen clock. Observations, tempo estimates and predictions stay separate.

No camera, audio, robot, or file access. All times are caller-supplied monotonic
seconds. Loss of a hand is not loss of the processing heartbeat.
"""
from collections import deque
from dataclasses import dataclass
import math
from statistics import median


@dataclass(frozen=True)
class ScreenClockConfig:
    min_bpm: float = 40.0
    max_bpm: float = 180.0
    initial_intervals: int = 3
    consistency: float = .12
    period_alpha: float = .55
    phase_gain: float = .75
    phase_step_limit: float = .20
    min_tick_spacing: float = .60
    heartbeat_timeout_s: float = .50
    result_max_age_s: float = .25
    reentry_periods: float = 2.35
    startup_timeout_s: float = 15.0

    def __post_init__(self):
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
               for v in vars(self).values()):
            raise ValueError('Screen clock settings must be finite numbers')
        if not 0 < self.min_bpm < self.max_bpm:
            raise ValueError('Invalid screen clock BPM range')
        if type(self.initial_intervals) is not int or not 3 <= self.initial_intervals <= 8:
            raise ValueError('initial_intervals must be an integer from 3 to 8')
        if not 0 < self.consistency <= .25 or not 0 < self.period_alpha <= 1:
            raise ValueError('Invalid consistency or period_alpha')
        if not 0 < self.phase_gain <= 1 or not 0 < self.phase_step_limit <= .25:
            raise ValueError('Invalid phase correction settings')
        if not .5 <= self.min_tick_spacing <= 1:
            raise ValueError('min_tick_spacing must be between .5 and 1')
        if not 0 < self.result_max_age_s <= self.heartbeat_timeout_s <= 2:
            raise ValueError('Invalid heartbeat/result timeouts')
        if not 2.25 < self.reentry_periods <= 4:
            raise ValueError('reentry_periods must exceed the isolated missed-beat window')
        if not 1 <= self.startup_timeout_s <= 60 or self.startup_timeout_s <= self.heartbeat_timeout_s:
            raise ValueError('startup_timeout_s must be 1..60 seconds and exceed the runtime heartbeat')


class ScreenClock:
    def __init__(self, config=None):
        self.cfg = config or ScreenClockConfig()
        self.events = deque()
        self.observed_count = self.predicted_count = self.sequence = 0
        self.last_observed = self.last_accepted = self.last_frame = None
        self.last_processed = self.last_step = self.started = None
        self.first_valid_frame_at = None
        self.tracking = False
        self.armed = True
        self.fault_reason = None
        self.stop_reason = None
        self.healthy = False
        self.restart_barrier = -math.inf
        self._clear_evidence()
        self.state = 'STARTING'

    def begin_startup(self, now):
        """Start the bounded initialization deadline before camera/model loading."""
        if self.started is None:
            if not math.isfinite(now):
                raise ValueError('Startup time must be finite')
            self.started = now
            self._log('SOURCE_STATUS', now, reason='STARTING_CAMERA_MODEL',
                      deadline_s=now+self.cfg.startup_timeout_s)

    def _clear_evidence(self):
        self.tempo_feedback = None
        self.period = None
        self.last_accepted = None
        self.anchor = self.raw_anchor = None
        self.initial = deque(maxlen=self.cfg.initial_intervals)
        self.recent = deque(maxlen=3)
        self.proposal = deque(maxlen=3)
        self.proposal_side = None
        self.reentry = False
        self.return_intervals = deque(maxlen=max(3, self.cfg.initial_intervals))
        self.resume_pending = False
        self.next_tick = self.last_tick = None
        self.phase_error = None
        self.phase_confirmations = 0
        self.confidence = 0.0

    def _log(self, kind, now, **values):
        if kind == 'TEMPO_DECISION':
            self.tempo_feedback = dict(time_s=now, **values)
        elif kind == 'TEMPO_ESTIMATE':
            self.tempo_feedback = None
        self.sequence += 1
        self.events.append(dict(sequence=self.sequence, kind=kind, time_s=now, **values))

    def drain_events(self):
        result = list(self.events)
        self.events.clear()
        return result

    @property
    def required_fresh_intervals(self):
        return self.cfg.initial_intervals

    def timed_reentry_due(self, now):
        return self.anchor is not None and now-self.anchor > self.cfg.reentry_periods*self.period

    def stop(self, now, reason='USER_STOP'):
        self.armed = False
        self.stop_reason = reason
        self.next_tick = None
        self.phase_confirmations = 0
        self.state = 'STOPPED'
        self.restart_barrier = now
        self._log('CLOCK_CONTROL', now, action='STOP', reason=reason)

    def arm(self, now):
        """Resume saved tempo after Space; other stops need fresh acquisition.

        Saved cadence is not current visual/phase evidence. Never replay missed
        ticks, and do not resume automatically after source recovery.
        """
        if not self.healthy or self.last_processed is None or now-self.last_processed > self.cfg.heartbeat_timeout_s:
            self._log('CLOCK_CONTROL', now, action='ARM_REFUSED', reason='SOURCE_NOT_HEALTHY')
            return False
        if self.armed and self.resume_pending:
            self._log('CLOCK_CONTROL', now, action='ARM_IGNORED', reason='RESUME_ALREADY_ACTIVE')
            return False
        resume = (not self.armed and self.stop_reason == 'USER_STOP'
                  and self.fault_reason is None and self.period is not None)
        saved_period, saved_tick = self.period, self.last_tick
        self._clear_evidence()
        self.restart_barrier = now
        self.fault_reason = None
        self.stop_reason = None
        self.armed = True
        self.state = 'ACQUIRING'
        if resume:
            self.period = saved_period
            self.last_tick = saved_tick
            # The next UI update may flash, unless a recent tick requires spacing.
            # This starts a new schedule; no claim of retained conducting phase.
            self.next_tick = max(now, saved_tick+self.cfg.min_tick_spacing*saved_period
                                 if saved_tick is not None else now)
            self.reentry = self.resume_pending = True
            self.state = 'RESUMING_LAST_TEMPO'
            self._log('CLOCK_CONTROL', now, action='ARM', reason='RESUME_LAST_TEMPO',
                      bpm=60/self.period, next_predicted_time_s=self.next_tick,
                      required_fresh_intervals=self.required_fresh_intervals)
        else:
            self._log('CLOCK_CONTROL', now, action='ARM', reason='FRESH_BEATS_REQUIRED')
        return True

    def reset(self, now):
        """Clear tempo/phase while preserving the actual event counters and stop latch."""
        self.stop(now, 'MANUAL_RESET')
        self._clear_evidence()

    def beat_line_changed(self, now):
        """Keep the schedule and stop/fault latches; require new line evidence.

        Moving a calibration line is not a gesture or an arm/stop command.
        Never join an interval across the edit, even during reacquisition.
        """
        if not math.isfinite(now) or (self.last_step is not None and now < self.last_step):
            raise ValueError('Beat-line changes require current monotonic time')
        self.restart_barrier = max(self.restart_barrier, now)
        self.tempo_feedback = None
        self.anchor = self.raw_anchor = self.last_accepted = None
        for values in (self.initial, self.recent, self.proposal, self.return_intervals):
            values.clear()
        self.proposal_side = None
        self.reentry = self.period is not None
        self.phase_error = None
        self.phase_confirmations = 0
        self.confidence = 0.0
        if self.armed and self.fault_reason is None:
            self.state = 'HOLDING_LAST_TEMPO' if self.running else 'ACQUIRING'
        self._log('CLOCK_CONTROL', now, action='ADJUST_BEAT_LINE',
                  reason='KEEP_SCHEDULE_REACQUIRE_EVIDENCE',
                  bpm=60/self.period if self.period else None,
                  running=self.running, next_predicted_time_s=self.next_tick)

    def fail(self, now, reason):
        self.healthy = False
        if self.fault_reason is None:
            self.fault_reason = reason
            self.armed = False
            self.next_tick = None
            self.phase_confirmations = 0
            self.restart_barrier = now
            self._log('SYSTEM_FAULT', now, reason=reason)
        self.state = 'FAULT_STOPPED'

    def coherent(self, values):
        return bool(values) and max(abs(v/median(values)-1) for v in values) <= self.cfg.consistency

    def valid_interval(self, value):
        return 60/self.cfg.max_bpm-1e-9 <= value <= 60/self.cfg.min_bpm+1e-9

    def _begin_reentry(self):
        if self.resume_pending:
            # New visual confirmation may not bridge another hand/tip loss.
            # Keep the saved cadence running while clearing only fresh evidence.
            self.raw_anchor = None
            self.return_intervals.clear()
            return
        if self.period is None:
            # Acquisition must use an uninterrupted run of visible-hand evidence.
            self.initial.clear()
            self.raw_anchor = self.anchor = None
            return
        if self.period is not None and not self.reentry:
            self.reentry = True
            self.raw_anchor = None
            self.return_intervals.clear()
            self.proposal.clear()
            self.phase_confirmations = 0

    def _estimate(self, stamp, now):
        raw = None if self.raw_anchor is None else stamp-self.raw_anchor
        self.raw_anchor = stamp
        if self.period is None:
            if raw is not None:
                if self.valid_interval(raw):
                    self.initial.append(raw)
                else:
                    self.initial.clear()
            self.anchor = stamp
            if len(self.initial) < self.cfg.initial_intervals or not self.coherent(self.initial):
                return False
            self.period = median(self.initial)
            self.recent.extend(self.initial)
            self.confidence = .8
            self.state = 'MEASURED'
            self._log('TEMPO_ESTIMATE', now, reason='INITIAL_CONSISTENT_INTERVALS', bpm=60/self.period)
            return True

        old = self.period
        if self.reentry:
            if raw is not None:
                if self.valid_interval(raw):
                    self.return_intervals.append(raw)
                else:
                    self.return_intervals.clear()
            # Three half-period intervals prevent one midpoint extra from
            # becoming a claimed doubling. Otherwise two intervals confirm return.
            required = (self.cfg.initial_intervals if self.resume_pending else
                        3 if self.return_intervals and median(self.return_intervals)/old < .60 else 2)
            values = list(self.return_intervals)[-required:]
            self.state = 'REJOIN_PENDING'
            if len(values) < required or not self.coherent(values):
                return False
            target = median(values)
            self.period = old+self.cfg.period_alpha*(target-old) if .75 <= target/old <= 1.35 else target
            self.recent.clear()
            self.recent.extend(values)
            resumed = self.resume_pending
            self.resume_pending = False
            self.reentry = False
            self.anchor = stamp
            self.confidence = .75
            self.state = 'MEASURED'
            self._log('TEMPO_ESTIMATE', now,
                      reason='CONFIRMED_RESUMED_TEMPO' if resumed else 'CONFIRMED_RETURN',
                      bpm=60/self.period)
            return True

        delta = stamp-self.anchor
        ratio = delta/old
        raw_ratio = raw/old if raw is not None else 1
        side = 'FASTER' if raw_ratio < .75 else 'SLOWER' if raw_ratio > 1.35 else None
        if side and raw is not None and self.valid_interval(raw):
            if self.proposal_side != side:
                self.proposal.clear()
            self.proposal_side = side
            self.proposal.append(raw)
            required = 3 if raw_ratio < .60 else 2
            values = list(self.proposal)[-required:]
            if len(values) >= required and self.coherent(values):
                self.period = median(values)
                self.anchor = stamp
                self.recent.clear()
                self.recent.extend(values)
                self.proposal.clear()
                self.confidence = .75
                self.state = 'MEASURED_CHANGE'
                self._log('TEMPO_ESTIMATE', now, reason='CONFIRMED_TEMPO_CHANGE', bpm=60/self.period)
                return True
        else:
            self.proposal.clear()
            self.proposal_side = None

        if .75-1e-9 <= ratio <= 1.35+1e-9 and self.valid_interval(delta):
            self.recent.append(delta)
            target = median(self.recent)
            self.period = old+self.cfg.period_alpha*(target-old)
            self.anchor = stamp
            self.confidence = min(.95, self.confidence+.05)
            self.state = 'CHANGE_PENDING' if self.proposal else 'MEASURED'
            self._log('TEMPO_ESTIMATE', now, reason='ROBUST_INTERVAL_UPDATE', bpm=60/self.period)
            return True
        if ratio < .75:
            self.state = 'EARLY_EVENT_PENDING'
            self._log('TEMPO_DECISION', now, reason='EARLY_OR_OUT_OF_RANGE_EXCLUDED', interval_s=delta)
            return False
        self.anchor = stamp
        self.phase_confirmations = 0
        self.state = 'MISSED_OR_CHANGE_PENDING' if ratio > 1.35 else 'OUT_OF_RANGE_PENDING'
        self._log('TEMPO_DECISION', now, reason='LONG_OR_OUT_OF_RANGE_KEEP_TEMPO', interval_s=delta)
        return False

    def _align(self, stamp, now, previous_period):
        if self.next_tick is None:
            self.next_tick = stamp+self.period
            while self.next_tick <= now:
                self.next_tick += self.period
            self.phase_confirmations = 0
            return
        # Change remaining fractional period, then slew only the next future tick.
        self.next_tick = now+(self.next_tick-now)*self.period/previous_period
        nearest = self.next_tick+round((stamp-self.next_tick)/self.period)*self.period
        error = stamp-nearest
        correction = max(-self.cfg.phase_step_limit*self.period,
                         min(self.cfg.phase_step_limit*self.period, self.cfg.phase_gain*error))
        self.next_tick += correction
        # Equality means a pulse on the next UI update, never in this callback.
        # An epsilon here creates a systematic receipt-latency phase trap when
        # the following observation always arrives just before that epsilon.
        earliest = now
        if self.last_tick is not None:
            earliest = max(earliest, self.last_tick+self.cfg.min_tick_spacing*min(previous_period, self.period))
        self.next_tick = max(self.next_tick, earliest)
        self.phase_error = error
        self.phase_confirmations = min(3, self.phase_confirmations+1) if abs(error) <= .08*self.period else 0
        self._log('PHASE_ADJUSTMENT', now, observed_time_s=stamp, error_s=error,
                  requested_correction_s=correction, next_predicted_time_s=self.next_tick)

    def _schedule_following_tick(self, scheduled, now):
        return max(scheduled+self.period, now+self.cfg.min_tick_spacing*self.period)

    def step(self, now, *, frame_time=None, track_good=None, beat_time=None, camera_time=None, fault=None):
        """Call on UI updates; frame_time only for a newly processed source frame.

        A successfully processed no-hand frame renews system health. Repeated
        frame timestamps do not renew it or admit a second observed detection.
        """
        if not math.isfinite(now) or (self.last_step is not None and now <= self.last_step):
            return self.snapshot(self.last_step or 0)
        if self.started is None:
            self.begin_startup(now)
        was_ready = self.first_valid_frame_at is not None
        gap = was_ready and self.last_step is not None and now-self.last_step > self.cfg.heartbeat_timeout_s
        self.last_step = now
        frame_valid = (frame_time is not None and math.isfinite(frame_time) and frame_time <= now
                       and (self.last_frame is None or frame_time > self.last_frame))
        problem = fault
        if gap:
            problem = problem or 'PROCESSING_HEARTBEAT_GAP'
        if frame_valid and now-frame_time > self.cfg.result_max_age_s:
            frame_valid = False
            if was_ready:
                problem = problem or 'STALE_PROCESSING_RESULT'
        # A late first valid result cannot erase a startup timeout. After a
        # timeout it can restore source health, but Enter must rearm the clock.
        if not was_ready and now-self.started >= self.cfg.startup_timeout_s:
            problem = problem or 'STARTUP_TIMEOUT'
        if frame_valid:
            self.last_frame = frame_time
            self.last_processed = now
            if self.first_valid_frame_at is None:
                self.first_valid_frame_at = now
                if self.armed and self.fault_reason is None and problem is None:
                    self.state = 'ACQUIRING'
                self._log('SOURCE_STATUS', now, reason='FIRST_VALID_PROCESSED_FRAME',
                          startup_elapsed_s=now-self.started)
            self.tracking = bool(track_good)
            if not self.tracking:
                self._begin_reentry()
        camera_reference = camera_time if camera_time is not None else self.last_frame
        camera_age = now-(camera_reference if camera_reference is not None else self.started)
        process_age = now-(self.last_processed if self.last_processed is not None else self.started)
        if self.first_valid_frame_at is not None and camera_age > self.cfg.heartbeat_timeout_s:
            problem = problem or 'CAMERA_HEARTBEAT_TIMEOUT'
        if self.first_valid_frame_at is not None and process_age > self.cfg.heartbeat_timeout_s:
            problem = problem or 'PROCESSING_HEARTBEAT_TIMEOUT'
        self.healthy = not problem and self.last_processed is not None
        if problem:
            self.fail(now, problem)

        if self.running:
            if self.timed_reentry_due(now):
                self._begin_reentry()
            if now >= self.next_tick-1e-9:
                # Exactly one screen pulse; never replay missed clock ticks.
                scheduled = self.next_tick
                self.last_tick = now
                self.predicted_count += 1
                self._log('PREDICTED_CLOCK_TICK', now, scheduled_time_s=scheduled, bpm=60/self.period)
                self.next_tick = self._schedule_following_tick(scheduled, now)

        if frame_valid and beat_time is not None and math.isfinite(beat_time) and beat_time <= now:
            if self.last_observed is None or beat_time > self.last_observed:
                self.observed_count += 1
                self.last_observed = beat_time
                self._log('OBSERVED_DETECTION', now, observed_time_s=beat_time, count=self.observed_count)
                eligible = (self.armed and self.fault_reason is None and self.tracking
                            and beat_time > self.restart_barrier and now-beat_time <= self.cfg.result_max_age_s)
                if eligible:
                    previous = self.period
                    used = self._estimate(beat_time, now)
                    if used:
                        self.last_accepted = beat_time
                        self._align(beat_time, now, previous)
                else:
                    self._log('TEMPO_DECISION', now, reason='DETECTION_LOGGED_NOT_USED')
        return self.snapshot(now)

    @property
    def running(self):
        return self.armed and self.fault_reason is None and self.next_tick is not None

    def snapshot(self, now):
        state = self.state
        if self.fault_reason:
            state = 'FAULT_STOPPED'
        elif not self.armed:
            state = 'STOPPED'
        elif self.first_valid_frame_at is None:
            state = 'STARTING'
        elif self.resume_pending:
            state = 'RESUMING_LAST_TEMPO'
        elif self.period is not None and (not self.tracking or self.reentry or self.last_accepted is None
                                           or now-self.last_accepted > 1.35*self.period):
            state = 'HOLDING_LAST_TEMPO' if not self.tracking or self.raw_anchor is None else 'REJOIN_PENDING'
        age = None if self.last_accepted is None else max(0, now-self.last_accepted)
        support = self.confidence if state.startswith('MEASURED') else 0.0
        return dict(time_s=now, bpm=60/self.period if self.period is not None else None,
                    supported_min_bpm=self.cfg.min_bpm, supported_max_bpm=self.cfg.max_bpm,
                    tempo_feedback=self.tempo_feedback,
                    state=state, running=self.running, armed=self.armed, healthy=self.healthy,
                    fault=self.fault_reason, tracking=self.tracking, observed_count=self.observed_count,
                    stop_reason=self.stop_reason, resume_pending=self.resume_pending,
                    fresh_resume_intervals=len(self.return_intervals) if self.resume_pending else 0,
                    required_resume_intervals=self.required_fresh_intervals,
                    source_initialized=self.first_valid_frame_at is not None,
                    first_valid_frame_at=self.first_valid_frame_at,
                    startup_remaining_s=max(0,self.cfg.startup_timeout_s-(now-self.started)) if self.started is not None else self.cfg.startup_timeout_s,
                    predicted_count=self.predicted_count, observed_time_s=self.last_observed,
                    last_predicted_time_s=self.last_tick, next_predicted_time_s=self.next_tick,
                    tempo_evidence_age_s=age, current_evidence_confidence=support,
                    phase_status=('SLEWING' if self.phase_confirmations < 2 else 'ESTIMATED_ALIGNMENT')
                    if self.running and state.startswith('MEASURED') else 'UNCONFIRMED',
                    phase_error_s=self.phase_error, initial_intervals=len(self.initial))
