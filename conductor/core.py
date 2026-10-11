"""Dependency-free, measured-time gesture and tempo estimation.

Image y increases downward. One quarter-note beat is a prepared downward
arrival at a narrow tolerance band around beat_y, including touch-and-rebound.
No audio, camera, wall-clock assumptions, or external side effects live here.
"""
from collections import Counter, deque
from dataclasses import dataclass, replace
import math
from statistics import median
from .continuity import displacement_limit, supported_tip_limit


@dataclass(frozen=True)
class DetectorConfig:
    beat_y: float = 0.62
    line_tolerance: float = 0.012
    rearm_distance: float = 0.06
    smoothing_tau_s: float = 0.055
    min_return: float = 0.04
    min_velocity: float = 0.08
    refractory_s: float = 0.28
    min_bpm: float = 40.0
    max_bpm: float = 180.0
    window_intervals: int = 7
    min_intervals: int = 3
    reacquire_s: float = 0.25
    max_frame_gap_s: float = 0.25
    max_position_jump: float = 0.30
    stale_min_s: float = 1.8
    stale_periods: float = 2.5
    brief_loss_s: float = 0.12
    tempo_hold_s: float = 2.0
    # Optional smaller return gate. Zero preserves the original detector.
    compact_rearm_margin: float = 0.0
    compact_min_return: float = 0.030
    compact_rearm_s: float = 0.060

    def __post_init__(self):
        values = vars(self)
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or
               not math.isfinite(v) for v in values.values()):
            raise ValueError("Detector values must be finite numbers")
        if not 0 < self.line_tolerance < self.rearm_distance < self.beat_y < 1-self.line_tolerance:
            raise ValueError("Require 0 < line_tolerance < rearm_distance < beat_y < 1-line_tolerance")
        if self.min_return > self.rearm_distance-self.line_tolerance:
            raise ValueError("min_return must fit between the contact band and rearm height")
        if not 0 < self.min_bpm < self.max_bpm:
            raise ValueError("Require 0 < min_bpm < max_bpm")
        if not (isinstance(self.min_intervals, int) and
                isinstance(self.window_intervals, int) and
                2 <= self.min_intervals <= self.window_intervals):
            raise ValueError("Interval counts must be integers, 2 <= min <= window")
        if any(values[k] <= 0 for k in values if k not in
               ("beat_y", "min_bpm", "max_bpm", "min_intervals", "window_intervals", "compact_rearm_margin")):
            raise ValueError("Timing and motion thresholds must be positive")
        if not 0 <= self.compact_rearm_margin < self.rearm_distance-self.line_tolerance:
            raise ValueError("compact_rearm_margin must be zero or inside the full return gate")
        if self.compact_rearm_margin and (self.compact_min_return < self.compact_rearm_margin or
                                         self.compact_rearm_s < .05):
            raise ValueError("Compact return requires an excursion at least its margin and >= 50 ms confirmation")
        if self.refractory_s >= 60 / self.max_bpm:
            raise ValueError("refractory_s must be shorter than the fastest beat interval")
        if self.brief_loss_s > self.max_frame_gap_s:
            raise ValueError("brief_loss_s must not exceed max_frame_gap_s")

    @property
    def arm_y(self):
        """Relative noise guard, not a second visible conducting target."""
        return self.beat_y-self.rearm_distance

    @property
    def contact_y(self):
        return self.beat_y-self.line_tolerance


@dataclass(frozen=True)
class Observation:
    t: float
    x: float
    y: float


@dataclass(frozen=True)
class Beat:
    t: float
    count: int
    interval: float | None


class Conductor:
    def __init__(self, config: DetectorConfig | None = None):
        self.config = config or DetectorConfig()
        self.last_input = None
        self.count = 0
        self.last_event = None
        self.intervals = deque(maxlen=self.config.window_intervals)
        self.diagnostic_events=deque(maxlen=256)
        self.diagnostic_counts=Counter()
        self.diagnostic_sequence=0
        self.reset(reason="startup")

    def _note(self,event,reason,**values):
        self.diagnostic_sequence+=1
        self.diagnostic_counts[f"{event}:{reason}"]+=1
        self.diagnostic_events.append(dict(seq=self.diagnostic_sequence,t=self.last_input,
                                          event=event,reason=reason,**values))
        if event in ('crossing_rejected', 'interval_rejected'):
            self.last_rejection = dict(t=self.last_input, reason=reason, **values)
        elif event in ('reset', 'interval_accepted'):
            self.last_rejection = None

    def drain_events(self):
        result=list(self.diagnostic_events)
        self.diagnostic_events.clear()
        return result

    def reset(self, reason="manual_reset"):
        """Clear motion/history; preserve the monotonic input watermark."""
        self.count = 0
        self.last_event = None
        self._clear("ACQUIRING", reason)

    def set_beat_line(self, beat_y):
        config = replace(self.config, beat_y=beat_y)
        if math.isclose(config.beat_y, self.config.beat_y, abs_tol=1e-9):
            return False
        self.config = config
        self.reset("beat_line_adjusted")
        self._note("configuration", "beat_line_adjusted", beat_line_height=beat_y)
        return True

    def _clear(self, status, reason="reset"):
        self.raw = self.smoothed = None
        self.acquired_at = None
        self.armed = False
        self.acquisition_ready = False
        self.tip_history = deque(maxlen=4)
        self.return_start_y = None
        self.previous_beat = None
        self.intervals.clear()
        self.status = status
        self.sample_dts=deque(maxlen=7)
        self.missing_since=None
        self.saved_motion=None
        self.tempo_suspended=False
        self.last_evidence_at=None
        self.held_tempo=None
        self.hold_started_at=None
        self._reset_compact_return()
        self._note("reset",reason)

    def _reset_compact_return(self):
        self.compact_since = None
        self.compact_samples = 0
        self.compact_upward = False

    def feedback(self, now):
        """Presentation evidence; does not change admission or timing."""
        rejection = self.last_rejection
        if rejection and rejection['t'] is not None and now-rejection['t'] > 3:
            rejection = None
        return dict(armed=self.armed, status=self.status, rejection=rejection,
                    visible=self.raw is not None, compact_pending=self.compact_since is not None)

    @property
    def bpm(self):
        if self.missing_since is not None or self.tempo_suspended or len(self.intervals) < self.config.min_intervals:
            return None
        return self._estimate_bpm()

    def _estimate_bpm(self):
        if len(self.intervals)<self.config.min_intervals:
            return None
        center = median(self.intervals)
        # MAD excludes isolated bad intervals without preventing sustained changes.
        mad = median(abs(v - center) for v in self.intervals)
        tolerance = max(0.08 * center, 3 * 1.4826 * mad)
        good = [v for v in self.intervals if abs(v - center) <= tolerance]
        return 60.0 / median(good) if len(good) >= self.config.min_intervals else None

    @property
    def output_bpm(self):
        """Display/oscillator tempo; a held value is explicitly stale, not a beat."""
        return self.bpm if self.bpm is not None else self.held_tempo

    @property
    def tempo_state(self):
        if self.bpm is not None:
            return "MEASURED"
        return "HOLDING / STALE" if self.held_tempo is not None else "UNAVAILABLE"

    @property
    def confidence(self):
        """Heuristic interval consistency, NOT a calibrated model probability."""
        if self.bpm is None:
            return 0.0
        center = median(self.intervals)
        deviations = [abs(v - center) / center for v in self.intervals]
        consistency = max(0, 1 - 4 * median(deviations))
        support = sum(v <= 0.12 for v in deviations) / len(deviations)
        return consistency * support * min(1, len(self.intervals) / self.config.window_intervals)

    def expire(self, t):
        if self.hold_started_at is not None and t-self.hold_started_at>self.config.tempo_hold_s:
            self._end_hold("holding_deadline")
        if self.missing_since is not None and t-self.missing_since>self.config.brief_loss_s and self.saved_motion is not None:
            self.intervals.clear()
            self.saved_motion=None
            self.last_evidence_at=None
            self._end_hold("tracking_loss_timeout")
            self._note("history_cleared","tracking_loss_timeout")
        evidence=self.last_evidence_at if self.last_evidence_at is not None else self.previous_beat
        if evidence is None:
            return
        period = median(self.intervals) if self.intervals else 60 / self.config.min_bpm
        if t - evidence > max(self.config.stale_min_s,
                                       self.config.stale_periods * period):
            self._clear("PAUSED - RETURN UP TO REARM","tempo_expired")

    def _end_hold(self, reason):
        if self.held_tempo is not None:
            self._note("tempo_hold_ended",reason)
        self.held_tempo=None
        # Keep the original deadline through repeated dropouts. A clean new
        # interval or a full clear is required to permit another hold window.

    def _missing(self,t,reason):
        self.acquisition_ready=False
        self._reset_compact_return()
        self.tip_history.clear()
        if self.missing_since is None:
            prior_bpm=self.bpm
            if prior_bpm is not None and self.hold_started_at is None:
                self.held_tempo=prior_bpm
                self.hold_started_at=t
                self._note("tempo_hold_started",reason,bpm=prior_bpm)
            self.missing_since=t
            self.saved_motion=(self.raw,self.smoothed,self.acquired_at,self.return_start_y,self.armed) if self.raw else None
            self._note("tracking_lost",reason)
        self.raw=self.smoothed=None
        self.armed=False
        self.previous_beat=None
        self.tempo_suspended=True
        self.status="LOST TRACK - SHOW ONE HAND"
        self.expire(t)

    def update(self, t: float, x: float | None, y: float | None, missing_reason="no_hand", *, wrist=None):
        """Accept unique monotonic samples. None means tracking lost.

        Missing/invalid observations suspend fresh measurement immediately.
        Confirmed tempo may be shown as bounded HOLDING / STALE. No beat is
        emitted on recovery, and no interval bridges missing data.
        """
        if not math.isfinite(t) or (self.last_input is not None and t <= self.last_input):
            self._note("sample_ignored","nonmonotonic_or_invalid_time")
            return None
        gap = self.last_input is not None and t - self.last_input > self.config.max_frame_gap_s
        self.last_input = t
        if gap:
            self._clear("REACQUIRING AFTER FRAME GAP","frame_gap")
        if x is None or y is None:
            self._missing(t,missing_reason)
            return None
        if not all(math.isfinite(v) and 0<=v<=1 for v in (x,y)):
            reason="landmark_nonfinite" if not all(math.isfinite(v) for v in (x,y)) else "landmark_out_of_frame"
            self._missing(t,reason)
            return None
        self.expire(t)
        recovering=False
        if wrist is not None and (len(wrist)!=2 or not all(math.isfinite(v) and 0<=v<=1 for v in wrist)):
            wrist=None
        if self.missing_since is not None:
            duration=t-self.missing_since
            saved=self.saved_motion
            if (saved is not None and duration<=self.config.brief_loss_s and
                    t-saved[0].t<=self.config.max_frame_gap_s and
                    math.hypot(x-saved[0].x,y-saved[0].y)<=displacement_limit(
                        self.config.max_position_jump,t-saved[0].t,self.sample_dts)):
                self.raw,self.smoothed,self.acquired_at,self.return_start_y,self.armed=saved
                self.missing_since=self.saved_motion=None
                recovering=True
                self._note("tracking_recovered","brief_gap",loss_s=duration)
            else:
                self._clear("ACQUIRING - RETURN UP","long_or_discontinuous_reacquisition")
        if self.raw:
            raw_dt=t-self.raw.t
            allowed=displacement_limit(self.config.max_position_jump,raw_dt,self.sample_dts)
            distance=math.hypot(x-self.raw.x,y-self.raw.y)
            if distance>allowed and not recovering:
                # Compute independent limits from the configured base, then
                # take their maximum; do not stack time and motion allowances.
                supported=supported_tip_limit(self.config.max_position_jump,t,x,y,wrist,self.tip_history)
                if distance<=supported:
                    self._note('motion_supported','continuous_tip_and_wrist',distance=distance,
                               base_limit=allowed,supported_limit=supported,dt_s=raw_dt)
                    allowed=supported
            if distance>allowed:
                self._clear("REACQUIRING AFTER POSITION JUMP","position_jump")
                recovering=False
            else:
                self.sample_dts.append(raw_dt)
        raw = Observation(t, x, y)
        if wrist is None:self.tip_history.clear()
        else:self.tip_history.append((t,x,y,tuple(wrist)))
        if self.smoothed is None:
            self.raw = self.smoothed = raw
            self.acquired_at = t
            self.return_start_y = y
            # Real current readiness; no missing path or old interval is reused.
            self.acquisition_ready = y<=self.config.arm_y
            self.status = "ACQUIRING - RETURN UP"
            return None
        previous = self.smoothed
        previous_raw=self.raw
        dt = t - previous.t
        alpha = -math.expm1(-dt / self.config.smoothing_tau_s)
        self.smoothed = Observation(t, previous.x + alpha * (x - previous.x),
                                   previous.y + alpha * (y - previous.y))
        self.raw = raw
        sy = self.smoothed.y
        velocity = (sy - previous.y) / dt
        self.return_start_y = max(self.return_start_y, y)
        if t - self.acquired_at < self.config.reacquire_s:
            if y>=self.config.contact_y:self.acquisition_ready=False
            self.status = "ACQUIRING - RETURN UP"
            return None
        if not self.armed:
            # A compact return must be spatially clear of the contact band,
            # have a measured upward excursion and persist across fresh frames.
            # A single tip jump or contact-band jitter cannot prepare a beat.
            compact = (self.config.compact_rearm_margin > 0 and
                       y <= self.config.contact_y-self.config.compact_rearm_margin and
                       self.return_start_y-y >= self.config.compact_min_return)
            if compact:
                if self.compact_since is None:
                    self.compact_since = t
                self.compact_samples += 1
                # Spatial excursion + dwell already reject noise; at the smaller
                # gate a slow return is decelerating toward its apex.
                self.compact_upward |= velocity <= -.5*self.config.min_velocity
            else:
                self._reset_compact_return()
            if self.acquisition_ready and y<=self.config.arm_y:
                self.armed=True
                self._note('armed','observed_ready_after_acquisition')
            elif (y <= self.config.arm_y and
                    self.return_start_y - y >= self.config.min_return and
                    velocity <= -self.config.min_velocity):
                self.armed = True
                self._note("armed","observed_upper_return",filtered_also_above_arm=sy<=self.config.arm_y)
            elif (compact and self.compact_samples >= 2 and self.compact_upward and
                  t-self.compact_since >= self.config.compact_rearm_s-1e-9):
                self.armed = True
                self._note('armed', 'confirmed_compact_return',
                           confirmation_s=t-self.compact_since, samples=self.compact_samples)
            if self.armed:
                self._reset_compact_return()
            if self.armed or y>=self.config.contact_y:self.acquisition_ready=False
            self.status = "READY - DOWN TO BEAT LINE" if self.armed else "LIFT FROM LINE TO READY"
        # Entering the small band on descent counts as contact. A wrist that
        # touches the line and immediately rebounds need not penetrate it.
        # Timestamp the observed band entry; do not infer a future reversal.
        crossing=previous_raw.y<self.config.contact_y<=y
        if crossing and not self.armed:
            self._note("crossing_rejected","not_armed")
        if not self.armed:
            return None
        self.status="READY - DOWN TO BEAT LINE"
        if crossing:
            # Consume even rejected contacts; a fresh upward return is required.
            self.armed = False
            self._reset_compact_return()
            self.return_start_y = y
            self.status = "LIFT FROM LINE TO READY"
            if recovering:
                self._note("crossing_rejected","reacquisition_frame")
                return None
            if velocity < self.config.min_velocity:
                self._note("crossing_rejected","insufficient_downward_velocity")
                return None
            fraction = (self.config.contact_y - previous_raw.y) / (y - previous_raw.y)
            beat_t = previous_raw.t + fraction * (t-previous_raw.t)
            interval = None if self.previous_beat is None else beat_t - self.previous_beat
            since_last=None if self.last_event is None else beat_t-self.last_event.t
            if since_last is not None and since_last < self.config.refractory_s:
                self._note("crossing_rejected","refractory",interval_s=since_last)
                return None
            if interval is not None:
                if 60 / self.config.max_bpm-1e-9 <= interval <= 60 / self.config.min_bpm+1e-9:
                    self.intervals.append(interval)
                    self.last_evidence_at=beat_t
                    self.tempo_suspended=False
                    self._end_hold("fresh_interval")
                    self.hold_started_at=None
                    self._note("interval_accepted","within_bpm_range",interval_s=interval)
                else:
                    # Do not destroy valid history for one unusable interval.
                    # It still expires by last VALID evidence, not this stroke.
                    self._note("interval_rejected","outside_bpm_range",interval_s=interval,
                               range_reason='below_supported_bpm' if interval>60/self.config.min_bpm else 'above_supported_bpm')
            self.previous_beat = beat_t
            self.count += 1
            self.last_event = Beat(beat_t, self.count, interval)
            self._note("beat","observed_downward_line_contact",beat_t=beat_t,interval_s=interval,
                       valid_intervals=len(self.intervals),bpm=self.bpm)
            return self.last_event
        return None


class TempoMetronome:
    """Free-running visual oscillator. Receives BPM, never beat event timestamps."""
    def __init__(self):
        self.phase = 0.0
        self.last_t = None
        self.running = False
        self.bpm = None

    def update(self, t, bpm):
        if self.last_t is not None and t <= self.last_t:
            return False
        dt = 0 if self.last_t is None else t - self.last_t
        self.last_t = t
        if bpm is None:
            self.phase = 0
            self.bpm = None
            self.running = False
            return False
        if not self.running:
            self.running = True
            self.bpm = bpm
            self.phase = 0
            return True
        # Integrate elapsed time at the previously active tempo; preserve phase
        # when BPM changes. There is no correction toward observed beat phase.
        total = self.phase + dt * self.bpm / 60
        self.phase = total % 1
        self.bpm = bpm
        return total >= 1
