"""Bounded phase reacquisition; ordinary responsive following is unchanged.

A first valid contact after visual loss can replace a sufficiently distant,
uncommitted hold prediction with one reactive return cue. It is phase evidence,
not a new tempo interval. Existing audio admission/stop/fault gates remain active.
"""
import math
from .responsive_clock import ResponsiveScreenClock


class ReentryResponsiveScreenClock(ResponsiveScreenClock):
    def __init__(self, config=None, *, commit_horizon_s=.20):
        if (isinstance(commit_horizon_s, bool) or not isinstance(commit_horizon_s, (int, float))
                or not math.isfinite(commit_horizon_s) or not 0 <= commit_horizon_s <= .5):
            raise ValueError('commit_horizon_s must be a finite value from 0 to .5')
        self.commit_horizon_s = commit_horizon_s
        super().__init__(config)

    def _clear_evidence(self):
        super()._clear_evidence()
        self.return_cue_anchor = None
        self.phase_reentry_pending = False

    def _begin_reentry(self):
        super()._begin_reentry()
        if self.period is not None and self.running:
            self.phase_reentry_pending = True

    def beat_line_changed(self, now):
        super().beat_line_changed(now)
        # A line edit alone is not a visual-loss return gesture.
        self.phase_reentry_pending = False

    def _estimate(self, stamp, now):
        returning_anchor = (self.phase_reentry_pending and self.raw_anchor is None
                            and self.period is not None)
        used = super()._estimate(stamp, now)
        if returning_anchor and self.running:
            self.phase_reentry_pending = False
            pending = self.next_tick
            protected = pending <= now+self.commit_horizon_s+1e-9
            recent_tick = (self.last_tick is not None and
                           now-self.last_tick < self.cfg.min_tick_spacing*self.period-1e-9)
            accepted = not protected and not recent_tick
            if accepted:
                # Replace the pending prediction while retaining its identity.
                # Never add another identity for a possibly committed prediction.
                self.next_tick = now
                self.return_cue_anchor = stamp
            self._log('REENTRY_PHASE_DECISION', now, observed_time_s=stamp,
                      accepted=accepted,
                      reason='REACTIVE_RETURN_CUE' if accepted else
                             'PENDING_PREDICTION_PROTECTED' if protected else 'RECENT_TICK_KEEP_SPACING',
                      previous_pending_time_s=pending, next_predicted_time_s=self.next_tick,
                      commit_horizon_s=self.commit_horizon_s,
                      tempo_unchanged=True, acoustic_alignment_verified=False)
        return used

    def _schedule_following_tick(self, scheduled, now):
        if self.return_cue_anchor is None:
            return super()._schedule_following_tick(scheduled, now)
        target = self.return_cue_anchor+self.period
        self.return_cue_anchor = None
        return max(target, now+self.cfg.min_tick_spacing*self.period)

    def snapshot(self, now):
        state = super().snapshot(now)
        state.update(follow_policy='responsive_reentry_phase_v2',
                     phase_policy='reentry', commit_horizon_s=self.commit_horizon_s)
        return state
