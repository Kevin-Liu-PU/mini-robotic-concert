"""Causal musical clock. No I/O, vision imports, or hardware access."""
from collections import deque
from dataclasses import dataclass
import math
import statistics
import uuid

SINGLE_PULSE_MAX_RATIO = 1.8


def inferred_span(elapsed, period):
    """Do not turn a permitted slower single pulse into a missing-beat claim.

    The same clock permits coherent single-pulse slowdown up to this ratio.
    Nearest-integer rounding at 1.5 contradicts that rule and can inject an
    extra ordinal, then use that inferred ordinal to accelerate the tempo.
    This remains an inference; explicit beat_span / observed role wins.
    """
    ratio=elapsed/period
    return 1 if ratio<=SINGLE_PULSE_MAX_RATIO else max(1,round(ratio))


@dataclass(frozen=True)
class BeatObservation:
    stamp: float
    received: float
    confidence: float = 1.0
    kind: str = "wrist_rebound"
    frame: int = -1
    source_s: float = 0.0
    # Only use when an independent source establishes elapsed musical beats.
    beat_span: int | None = None
    decision_stamp: float | None = None
    decision_frame: int | None = None
    origin: str = "vision"
    predicted: bool = False


class MusicalClock:
    def __init__(self, initial_bpm: float, meter=4, session_id=None, score_beats=48):
        if not math.isfinite(initial_bpm) or not 30 <= initial_bpm <= 180:
            raise ValueError("initial_bpm must explicitly be set in [30, 180]")
        if meter not in (3, 4):
            raise ValueError("meter must be 3 or 4")
        self.initial_bpm = float(initial_bpm)
        self.bpm = float(initial_bpm)
        self.meter = meter
        self.score_beats = score_beats
        self.session_id = session_id or str(uuid.uuid4())
        self.state = "idle"
        self.armed_at = None
        self.last_start_rejection = None
        self.seq = 0
        self.phase = 0.0
        self.now = None
        self.correction = 0.0
        self.correction_end = 0.0
        self.last_beat = -1
        self.last_observed = None
        self.last_observed_position = 0
        self.last_observed_kind = None
        self.last_tempo_observed = None
        self.last_tempo_position = 0
        self.last_received_stamp = None
        self.last_visual = None
        self.history = deque(maxlen=4096)
        self.intervals = deque(maxlen=6)
        self.cadence_segments = deque(maxlen=8)
        self.tempo_evidence = "initial_explicit"
        self.accepted = 0
        self.rejected = 0
        self.deferred = 0
        self.skipped_deadlines = 0
        self.stopped_reason = None
        self.predicted_start_stamp=None
        self.start_visual_confirmed=False
        self.pending_phase_observation = None
        self.bar_position_ambiguous = False

    @property
    def period(self):
        return 60.0 / self.bpm

    def advance(self, now):
        if not math.isfinite(now) or self.now is not None and now < self.now:
            raise ValueError("clock requires finite nondecreasing monotonic time")
        if self.now is not None and self.state == "running":
            duration = now - self.now
            active = max(0.0, min(now, self.correction_end) - self.now)
            self.phase += duration * self.bpm / 60 + active * self.correction
        self.now = now
        if now >= self.correction_end:
            self.correction = 0.0
        self.history.append((now, self.phase))

    def _quality(self, now):
        age = None if self.last_observed is None else max(0.0, now - self.last_observed)
        visible = self.last_visual is not None and now - self.last_visual < .25
        return dict(mode="observed" if age is not None and age < 1.5 * self.period else "hold",
                    observation_age_s=age, tempo_evidence=self.tempo_evidence,
                    vision_available=visible, accepted_observations=self.accepted,
                    rejected_observations=self.rejected,
                    deferred_observations=self.deferred,
                    start_evidence=('visually_confirmed' if self.start_visual_confirmed else 'predicted') if self.predicted_start_stamp is not None else 'visual_or_manual',
                    bar_position_status=('ambiguous' if self.bar_position_ambiguous else 'estimated'))

    def message(self, now, kind="clock", **extra):
        self.seq += 1
        return dict(schema="conductor.clock.v1", session_id=self.session_id, seq=self.seq,
                    monotonic_ns=round(now * 1e9), kind=kind, state=self.state,
                    beat_position=self.phase, bpm=self.bpm, meter=self.meter,
                    phase_rate_bps=self.bpm / 60 + self.correction if self.state == "running" else 0.,
                    stop=self.state == "stopped", quality=self._quality(now), **extra)

    def arm(self, now, reason="manual"):
        self.advance(now)
        if self.state in ("armed", "running", "stopped", "ended"):
            return []
        self.state = "armed"
        self.armed_at = now
        return [self.message(now, "armed", reason=reason)]

    def start_prepared(self, now, plan):
        """A causal prediction from N observed preparation cues, never cue N+1."""
        from .count_in import PreparationPlan
        if not isinstance(plan,PreparationPlan) or len(plan.cues)!=self.meter:
            self.last_start_rejection='invalid_preparation_plan';return []
        cues=plan.cues
        valid=(all(isinstance(o,BeatObservation) and o.origin=='vision' and not o.predicted
                   and o.frame>=0 and math.isfinite(o.stamp) and math.isfinite(o.received)
                   and .65<=o.confidence<=1 and o.stamp<=o.received<=plan.available_s+.001
                   for o in cues) and
               self.armed_at is not None and plan.boundary_s>=self.armed_at and
               cues[0].stamp>plan.boundary_s and cues[0].kind=='wrist_rebound' and
               all(b.stamp>a.stamp for a,b in zip(cues,cues[1:])) and
               1/3-1e-9<=plan.period_s<=2.+1e-9 and
               abs(plan.period_s-(cues[-1].stamp-cues[0].stamp)/(self.meter-1))<1e-8 and
               abs(plan.start_s-(cues[-1].stamp+plan.period_s))<1e-8 and
               plan.available_s<=now+.001)
        if not valid:self.last_start_rejection='invalid_preparation_plan';return []
        if now<plan.start_s:
            self.last_start_rejection='before_preparation_deadline';return []
        if now-plan.start_s>min(.1,.25*plan.period_s):
            self.last_start_rejection='missed_preparation_deadline';return []
        self._validated_preparation=plan
        try:return self.start(now,'predicted_count_in_boundary',preparation=plan)
        finally:self._validated_preparation=None

    def start(self, now, reason="manual", observation=None, preparation=None):
        self.advance(now)
        if preparation is not None and preparation is not getattr(self,"_validated_preparation",None):
            self.last_start_rejection="unvalidated_preparation_plan";return []
        if self.state != "armed":
            self.last_start_rejection = "not_armed"
            return []
        if observation is None and preparation is None and reason not in ("manual","keyboard_start","explicit_test_start"):
            self.last_start_rejection="automatic_start_requires_visual_evidence"
            self.rejected+=1
            return []
        if observation is not None:
            obs=observation
            rejected=None
            if not isinstance(obs,BeatObservation):
                rejected="not_a_visual_observation"
            elif not all(math.isfinite(v) for v in (obs.stamp,obs.received,obs.confidence)) or not 0<=obs.confidence<=1:
                rejected="invalid_observation"
            elif obs.origin!="vision" or obs.predicted or obs.kind!="wrist_rebound" or obs.frame<0:
                rejected="not_a_real_downbeat_observation"
            elif obs.confidence<.65:
                rejected="low_quality_start"
            elif self.armed_at is None or obs.stamp<=self.armed_at:
                rejected="observation_not_after_prepare"
            elif obs.stamp>now+.001 or now-obs.stamp>.40 or obs.received<obs.stamp or obs.received>now+.001:
                rejected="stale_or_wrong_clock_domain_start"
            if rejected is None and now-obs.stamp>=60/self.initial_bpm:
                rejected="start_evidence_older_than_one_beat"
            if rejected:
                self.last_start_rejection=rejected
                self.rejected+=1
                return []
        self.last_start_rejection=None
        if preparation is not None:self.initial_bpm=60/preparation.period_s
        self.predicted_start_stamp=preparation.start_s if preparation is not None else None
        self.start_visual_confirmed=False
        self.state = "running"
        # Detection becomes available after its source frame. Emit the first
        # beat NOW, but anchor subsequent phase to the observed downbeat rather
        # than adding that confirmation latency to every following deadline.
        # No past beat is published or audio onset backdated.
        start_age = (now-preparation.start_s if preparation is not None else max(0., now-observation.stamp) if observation else 0.)
        self.phase = start_age*self.initial_bpm/60
        self.last_beat = 0
        self.bpm = self.initial_bpm
        self.intervals.clear()
        self.cadence_segments.clear()
        self.pending_phase_observation = None
        self.bar_position_ambiguous = False
        self.last_observed = observation.stamp if observation else None
        self.last_observed_position = 0
        self.last_observed_kind = observation.kind if observation else None
        self.last_tempo_observed = observation.stamp if observation else None
        self.last_tempo_position = 0
        self.last_received_stamp = observation.stamp if observation else preparation.cues[-1].stamp if preparation is not None else None
        self.correction = 0.
        self.history.clear()
        if observation and observation.stamp < now:
            self.history.append((observation.stamp,0.))
        if preparation is not None and preparation.start_s<now:self.history.append((preparation.start_s,0.))
        self.history.append((now,self.phase))
        self.stopped_reason = None
        self.tempo_evidence = 'observed_preparation_predicted_boundary' if preparation is not None else "completed_causal_count_in" if reason == 'causal_count_in_downbeat' else "initial_explicit"
        return [self.message(now, "start", reason=reason),
                self.message(now, "beat", beat_index=0, origin="predicted_count_in" if preparation is not None else "start_command", lateness_s=start_age)]

    def retry_count_in(self, now, reason):
        """Discard an ambiguous preparation; distinct from Stop/tracking loss.

        Only a fresh arming event can establish another preparation boundary.
        Normal repeated L/Prepare while armed remains idempotent.
        """
        self.advance(now)
        if self.state != 'armed':return []
        self.state='idle'
        self.armed_at=None
        self.correction=0.
        return [self.message(now,'count_in_retry',reason=reason)]

    def pause_tracking(self, now):
        """Beat-only safety pause; not a Stop gesture and never auto-resumes."""
        self.advance(now)
        if self.state not in ('armed','running'):
            return []
        self.state = 'paused'
        self.correction = 0.
        return [self.message(now, 'tracking_pause', reason='tracking_lost_pause')]

    def stop(self, now, reason="manual"):
        self.advance(now)
        if self.state in ("stopped", "ended"):
            return []
        self.state = "stopped"
        self.correction = 0.
        self.stopped_reason = reason
        return [self.message(now, "stop", reason=reason)]

    def _phase_at(self, stamp):
        previous = None
        for t, phase in self.history:
            if t >= stamp:
                if previous is None:
                    return phase
                pt, pp = previous
                return pp + (phase - pp) * ((stamp - pt) / (t - pt)) if t > pt else phase
            previous = (t, phase)
        return self.phase

    def observe(self, obs: BeatObservation, now):
        self.advance(now)
        record = dict(stamp=obs.stamp, received=obs.received, processed=now,
                      frame=obs.frame, source_s=obs.source_s, kind=obs.kind,
                      confidence=obs.confidence, accepted=False, phase_adjusted=False,
                      bpm_before=self.bpm, bpm_after=self.bpm)
        record.update(decision_stamp=obs.decision_stamp, decision_frame=obs.decision_frame)
        reason = None
        if not all(math.isfinite(x) for x in (obs.stamp, obs.received, obs.confidence)):
            reason = "invalid"
        elif self.state != "running":
            reason = "inactive"
        elif obs.predicted or obs.origin!="vision":
            reason = "not_visual_evidence"
        elif obs.beat_span is not None and (type(obs.beat_span) is not int or not 1<=obs.beat_span<=16):
            reason = "invalid_beat_span"
        elif self.last_received_stamp is not None and obs.stamp <= self.last_received_stamp:
            reason = "duplicate_or_out_of_order"
        elif obs.stamp > now + .001 or now - obs.stamp > .40:
            reason = "future_or_stale"
        elif obs.confidence < .65:
            reason = "low_quality"
        elif self.last_observed is not None and obs.stamp - self.last_observed < .30 * self.period:
            reason = "too_close"
        if reason:
            self.rejected += 1
            return dict(record, reason=reason)
        previous = self.last_tempo_observed
        previous_position = self.last_tempo_position
        # Keep an UNWRAPPED observed beat ordinal anchored at Start. Rounding
        # current oscillator phase independently on every observation causes
        # permanent one-beat cycle slips after genuine tempo changes.
        old_period_for_position = self.period
        pending = self.pending_phase_observation
        if pending is not None and obs.kind != 'chin_crossing':
            self.pending_phase_observation = None
            earlier = pending['observation']
            first = pending['interval_s']
            second = obs.stamp-earlier.stamp
            # A later real observation can support a sustained slower cadence.
            # It cannot prove that every intervening musical beat was seen, so
            # the bar index stays explicitly ambiguous until a fresh start.
            if (obs.beat_span is None and obs.kind != 'chin_crossing' and
                    earlier.stamp < obs.stamp and
                    previous == pending['previous_stamp'] and
                    previous_position == pending['previous_position'] and
                    (self.last_received_stamp is None or earlier.stamp > self.last_received_stamp) and
                    1.4*old_period_for_position <= second <= 1.8*old_period_for_position and
                    abs(first-second) <= .10*statistics.median((first,second))):
                self.last_observed = earlier.stamp
                self.last_observed_position = self.last_tempo_position+1
                self.last_observed_kind = earlier.kind
                self.last_received_stamp = earlier.stamp
                self.last_tempo_observed = earlier.stamp
                self.last_tempo_position += 1
                self.intervals.append(first)
                self.cadence_segments.append((first,1))
                self.accepted += 1
                previous = earlier.stamp
                previous_position = self.last_tempo_position
                record['resolved_deferred_frame'] = earlier.frame
                record['resolution'] = 'consistent_observed_slow_cadence_bar_still_ambiguous'
        if previous is None:
            target_position = round(self._phase_at(obs.stamp))
        else:
            # Accepted upstrokes establish intervening observed beats. Do not
            # re-round the entire wrist gap against an unsettled startup tempo.
            if obs.beat_span is not None:
                target_position = self.last_tempo_position + obs.beat_span
            elif self.last_observed_kind == 'chin_crossing' and obs.kind == 'wrist_rebound' and obs.stamp-self.last_observed < 1.8*old_period_for_position:
                # Project convention: upward chin crossing is the last beat;
                # the next qualified rebound is the following beat. This is
                # semantic association, not a new detection or inferred BPM.
                target_position = self.last_observed_position + 1
                record['ordinal_association'] = 'rebound_after_observed_upstroke'
            else:
                ratio=(obs.stamp-previous)/old_period_for_position
                span = inferred_span(obs.stamp-previous,old_period_for_position)
                record['single_pulse_or_gap_ambiguous']=span!=max(1,round(ratio))
                target_position = self.last_tempo_position + span
        if obs.kind == 'chin_crossing' and target_position % self.meter != self.meter-1:
            # Crossing is last-beat evidence for the configured pattern, not
            # an extra beat wherever a normal rebound happens to lift a finger.
            # A wrist endpoint may already have supplied the last beat.
            self.rejected += 1
            return dict(record, reason='upstroke_outside_expected_last_beat',
                        proposed_beat_position=target_position)
        if (previous is not None and obs.beat_span is None and
                record.get('ordinal_association') != 'rebound_after_observed_upstroke' and
                target_position-self.last_tempo_position > 1):
            # The missing-beat count is inferred from timing, not seen.
            # A real slowdown can yield the same observations.
            self.bar_position_ambiguous = True
            record['bar_position_status'] = 'ambiguous'
        observed_phase = self._phase_at(obs.stamp)
        if previous is not None and obs.beat_span is None and obs.kind != 'chin_crossing':
            interval = obs.stamp-previous
            error = target_position-observed_phase
            if ('resolved_deferred_frame' not in record and len(self.cadence_segments) >= 2 and
                    1.4*old_period_for_position <= interval <= 1.8*old_period_for_position
                    and error < -.5):
                # A late single beat and an extra/missed motion are both
                # plausible. Keep the oscillator running, but do not let this
                # one event force a backward phase or tempo correction.
                self.pending_phase_observation = dict(observation=obs,interval_s=interval,
                    previous_stamp=previous,previous_position=previous_position)
                self.bar_position_ambiguous = True
                self.deferred += 1
                return dict(record, reason='ambiguous_single_or_gap_deferred',
                            deferred=True,
                            target_beat_position=target_position,
                            alternate_beat_position=target_position+1,
                            phase_error_beats=error,
                            bar_position_status='ambiguous')
        self.last_received_stamp = obs.stamp
        self.last_observed = obs.stamp
        self.last_observed_position = target_position
        self.last_observed_kind = obs.kind
        self.accepted += 1
        if (self.predicted_start_stamp is not None and not self.start_visual_confirmed and
                obs.kind=='wrist_rebound' and abs(obs.stamp-self.predicted_start_stamp)<=.35*self.period):
            self.start_visual_confirmed=True
            record['confirms_predicted_start']=True
            record['predicted_start_s']=self.predicted_start_stamp
        record["accepted"] = True
        record["reason"] = "phase_only"
        coherent = False
        tempo_eligible = obs.kind != "chin_crossing"
        if tempo_eligible:
            self.last_tempo_observed = obs.stamp
            self.last_tempo_position = target_position
        else:
            record["reason"] = "chin_phase_only"
        if previous is not None and tempo_eligible:
            dt = obs.stamp - previous
            old_period = self.period
            multiple = min(8,inferred_span(dt,old_period))
            if obs.beat_span is not None:
                if not isinstance(obs.beat_span, int) or not 1 <= obs.beat_span <= 16:
                    raise ValueError("beat_span must be a reliable integer 1..16")
                candidate = dt / obs.beat_span
                self.intervals.append(candidate)
                record["beat_span"] = obs.beat_span
            else:
                candidate = dt
                self.intervals.append(dt)
            # One interval never changes tempo. A separately tracked raw cadence
            # can re-lock after genuine changes instead of permanently gating them.
            # Two short intervals can belong to a repeating within-bar shape.
            # Three coherent intervals are needed for the fast-change route;
            # ordinary smoothing and full-bar balancing remain available.
            recent = list(self.intervals)[-3:]
            elapsed_beats = target_position - previous_position
            if 1 <= elapsed_beats <= 8:
                self.cadence_segments.append((dt, elapsed_beats))
            balanced = []
            for segment in reversed(self.cadence_segments):
                balanced.append(segment)
                if sum(n for _, n in balanced) >= self.meter:
                    break
            balanced_span = sum(n for _, n in balanced)
            balanced_period = sum(d for d, _ in balanced)/balanced_span if balanced_span else None
            fast_change = False
            if len(recent) >= 2:
                med = statistics.median(recent)
                coherent = max(abs(x / med - 1) for x in recent) <= .12
                ratio = med / old_period
                if coherent and .45 <= ratio <= SINGLE_PULSE_MAX_RATIO and 30 <= 60 / med <= 180:
                    fast_change = len(recent) >= 3 and abs(ratio - 1) > .12
                    gain = .55 if fast_change else .30
                    new_period = old_period + gain * (med - old_period)
                    self.bpm = min(180., max(30., 60 / new_period))
                    self.tempo_evidence = "coherent_observed_cadence"
                    record["reason"] = "tempo_and_phase"
                elif coherent and multiple >= 2 and abs(dt / multiple / old_period - 1) < .15:
                    # Indistinguishable from observing every Nth beat. Preserve
                    # tempo unless a reliable independent beat_span resolves it.
                    self.tempo_evidence = "integer_gap_hold"
                    record["reason"] = "integer_gap_hold"
                elif not coherent:
                    record["reason"] = "irregular_interval_hold"
            # A rebound-only median favours the shorter early-bar intervals:
            # the longer beat-3 -> next downbeat interval was discarded.
            # Include elapsed beat spans in a causal, short window; the ordinal
            # gap is inferred and is logged, never represented as new detection.
            # Keep the existing coherent-change route for rapid acceleration.
            if not fast_change and len(balanced) >= 2 and balanced_span >= self.meter and any(n == 1 for _, n in balanced) and balanced_period is not None:
                ratio = balanced_period / old_period
                if .55 <= ratio <= 1.8 and 30 <= 60/balanced_period <= 180:
                    self.bpm = 60/(old_period + .35*(balanced_period-old_period))
                    self.tempo_evidence = "balanced_observed_intervals"
                    record["reason"] = "balanced_tempo_and_phase"
            record.update(cadence_window_beats=balanced_span, cadence_period_s=balanced_period,
                          inferred_segment_beats=elapsed_beats)
            record["interval_s"] = dt
            record["inferred_gap_multiple"] = multiple
        error = target_position - observed_phase
        # Reject isolated far-phase evidence; sustained changed cadence may
        # reacquire. Only a bounded rate adjustment changes FUTURE times.
        if abs(error) <= .22 or coherent or obs.confidence >= .8:
            horizon = max(.5, self.period)
            shift = max(-.25, min(.25, error * .60))
            self.correction = max(-.30 * self.bpm / 60,
                                  min(.30 * self.bpm / 60, shift / horizon))
            self.correction = max(.5-self.bpm/60,self.correction)
            self.correction = min(3-self.bpm/60,self.correction)
            self.correction_end = now + horizon
            record["phase_adjusted"] = True
        record.update(phase_error_beats=error, target_beat_position=target_position, bpm_after=self.bpm)
        return record

    def tick(self, now):
        self.advance(now)
        if self.state != "running":
            return []
        if self.score_beats is not None and self.phase >= self.score_beats:
            self.phase = float(self.score_beats)
            self.state = "ended"
            self.correction = 0.
            return [self.message(now, "end", reason="score_end")]
        index = math.floor(self.phase + 1e-9)
        if index <= self.last_beat:
            return []
        crossed = index - self.last_beat
        self.last_beat = index
        # Never emit a catch-up burst after a scheduler stall.
        self.skipped_deadlines += max(0, crossed - 1)
        lateness = (self.phase - index) / max(.01, self.bpm / 60 + self.correction)
        if lateness > .10:
            self.skipped_deadlines += 1
            return []
        return [self.message(now, "beat", beat_index=index, origin="internal_clock",
                             lateness_s=lateness, skipped_before=max(0, crossed - 1))]
