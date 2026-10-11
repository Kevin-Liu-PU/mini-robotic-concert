"""Follow each valid observed interval; no inferred intention or tempo smoothing.

Shares source watchdogs, user stop/fault latches and audio admission protections
with ScreenClock. A plausible missed/extra contact is intrinsically ambiguous.
"""
from .screen_clock import ScreenClock


class ResponsiveScreenClock(ScreenClock):
    @property
    def required_fresh_intervals(self):
        return 1

    def timed_reentry_due(self, now):
        # A longer interval with continuous vision can be a real slowdown.
        # Explicit tracking loss and invalid (> maximum) intervals break evidence.
        return False

    def _begin_reentry(self):
        super()._begin_reentry()
        self.tempo_feedback = None
        self.anchor = self.raw_anchor = None
        self.return_intervals.clear()
        self.phase_confirmations = 0
        self.phase_error = None
        self.confidence = 0.0

    def _estimate(self, stamp, now):
        previous = self.raw_anchor
        if previous is None:
            self.raw_anchor = self.anchor = stamp
            self.state = 'REJOIN_PENDING' if self.period is not None else 'ACQUIRING'
            self._log('TEMPO_DECISION', now, reason='NEW_OBSERVED_ANCHOR')
            return False
        interval = stamp-previous
        if interval < 60/self.cfg.max_bpm-1e-9:
            # A clearly too-close contact must not replace the valid anchor.
            self._log('TEMPO_DECISION', now, reason='TOO_SHORT_CONTACT_REJECTED', interval_s=interval)
            return False
        self.raw_anchor = self.anchor = stamp
        if not self.valid_interval(interval):
            self.reentry = self.period is not None
            self.phase_confirmations = 0
            self.confidence = 0.0
            self._log('TEMPO_DECISION', now, reason='OUT_OF_RANGE_INTERVAL_NEW_ANCHOR', interval_s=interval)
            return False
        initial, returning = self.period is None, self.reentry or self.resume_pending
        self.period = interval
        self.initial.clear(); self.initial.append(interval)
        self.recent.clear(); self.recent.append(interval)
        self.return_intervals.clear()
        self.resume_pending = self.reentry = False
        self.confidence = .5  # One interval of support; never a calibrated probability.
        self.state = 'MEASURED'
        self._log('TEMPO_ESTIMATE', now,
                  reason='FIRST_VALID_INTERVAL' if initial else
                         'OBSERVED_RETURN_INTERVAL' if returning else 'LATEST_OBSERVED_INTERVAL',
                  interval_s=interval, bpm=60/interval)
        return True

    def _align(self, stamp, now, previous_period):
        if self.next_tick is None:
            super()._align(stamp, now, previous_period)
            return
        # Preserve the pending prediction identity. Moving blindly to stamp+T
        # could erase a due tick whose audio is already committed to the driver.
        scaled = now+(self.next_tick-now)*self.period/previous_period
        nearest = scaled+round((stamp-scaled)/self.period)*self.period
        error = stamp-nearest
        target = scaled+error
        earliest = now
        if self.last_tick is not None:
            earliest = max(earliest, self.last_tick+self.cfg.min_tick_spacing*min(previous_period,self.period))
        self.next_tick = max(target, earliest)
        self.phase_error = error
        self.phase_confirmations = 0
        self._log('PHASE_ADJUSTMENT', now, observed_time_s=stamp, error_s=error,
                  requested_correction_s=error, next_predicted_time_s=self.next_tick,
                  spacing_limited=self.next_tick>target+1e-9, policy='OBSERVED_GRID')

    def snapshot(self, now):
        state = super().snapshot(now)
        state.update(follow_policy='responsive_observed_interval_v1', required_initial_intervals=1)
        if state['running'] and state['state'].startswith('MEASURED'):
            state['phase_status'] = 'VISUAL GRID ESTIMATE'
        return state
