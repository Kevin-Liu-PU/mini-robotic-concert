"""Count-in and musical clock for gesture beats. No I/O.

The old clock decided which beat an observation was by counting from the
start, so one wrong count shifted every later beat and the lift was then
thrown away as "not the last beat". Here the gesture says which beat it is
(`GestureBeat.number`): the clock only has to decide which BAR, and that is
the nearest one. Tempo comes from the two events that are clear in every
bar, the downstroke (beat 1) and the lift (last beat), each compared with
itself one bar earlier. A middle beat only nudges the phase.

The count-in is rolling: every bar that shows beat 1 and then the lift is a
complete preparation, and the piece starts on the next downbeat, predicted
from that bar. Nothing has to be repeated after a bad bar; the next bar
simply counts. The first downstroke the conductor then really makes is the
start: the clock moves its bar line onto it at once.
"""
from dataclasses import dataclass
import math

from .clock import MusicalClock
from .gesture_beats import GestureBeat


@dataclass(frozen=True)
class GesturePlan:
    cues: tuple              # the beats of the preparation bar that were seen, beat 1 first, lift last
    period_s: float          # one beat
    start_s: float           # predicted time of the first beat of the piece
    available_s: float       # when this became known
    boundary_s: float        # the Prepare that this bar followed
    generation: int
    downstroke: object = None  # the conductor's own first downstroke, when it came before the predicted time


class GestureCountIn:
    """Same calls as the old CountIn, so the runner drives either one."""

    MIN_BPM, MAX_BPM = 40.0, 160.0
    AGREE = 0.20                 # two tempo readings agree when they differ by no more than this
    # In a bar that follows a rest the lift is stamped this much of a beat before the last beat's
    # place, counted in the bar's real length from its downstroke to the next one. In recorded
    # conducting the median is 0.13 of a beat, in three and in four beats; it is 0.04 to 0.07 once
    # the conducting is steady. A single bar's tempo taken from downstroke to lift is therefore too
    # fast, and "one beat after the lift" too early.
    LIFT_EARLY = 0.13

    def __init__(self, meter=3, bpm_hint=None):
        if meter not in (3, 4):
            raise ValueError("count-in meter must be 3 or 4")
        self.meter = meter
        self.hint_period = 60.0 / bpm_hint if bpm_hint else None
        self.previous_lift = None    # stamp of the lift that closed the bar before this one
        self.generation = 0
        self.resets = 0
        self.reset()

    def reset(self):
        self.cues = []
        self.plan = None
        self.failed = False
        self.recovering = False
        self.confirmation = None
        self.armed_at = None
        self.boundary = None
        self.last_reason = "waiting_first_downbeat"
        self.last_context_ready = False
        self.last_context_stamp = None

    def sync(self, armed_at):
        if armed_at != self.armed_at:
            generation, resets = self.generation, self.resets
            self.reset()
            self.armed_at = self.boundary = armed_at
            self.generation, self.resets = generation + 1, resets

    def restart(self, now, armed_at, reason="explicit_prepare"):
        self.sync(armed_at)
        self.cues, self.plan = [], None
        self.previous_lift = None
        self.boundary = now
        self.last_reason = reason
        self.generation += 1
        return dict(action="count_in_reset", reason=reason, processed=now, count=0)

    def recover(self, now, reason):
        """Drop the bar in progress. The next bar counts again; nothing has to be repeated."""
        self.cues, self.plan = [], None
        self.resets += 1
        self.generation += 1
        self.last_reason = reason
        return dict(action="count_in_reset", reason=reason, processed=now, count=0, resets=self.resets)

    def tick(self, now, armed_at):
        self.sync(armed_at)
        if self.plan is None and self.cues and now - self.cues[-1].stamp > 3.0:
            return self.recover(now, "bar_not_finished")
        return None

    def context(self, stamp, now, armed_at, tracking_ready, quiet_since=None):
        self.sync(armed_at)
        if self.last_context_stamp is not None and stamp <= self.last_context_stamp:
            return None
        self.last_context_stamp = stamp
        self.last_context_ready = bool(tracking_ready)
        return self.tick(now, armed_at)

    @property
    def stage(self):
        return "scheduled" if self.plan is not None else "counting" if self.cues else "waiting"

    def observe(self, obs, now, armed_at):
        self.sync(armed_at)
        record = dict(action="count_in", source_s=obs.source_s, frame=obs.frame, stamp=obs.stamp, processed=now,
                      kind=obs.kind, number=getattr(obs, "number", None), count=len(self.cues), accepted=False,
                      tempo_bpm=None)
        number = getattr(obs, "number", None)
        if not isinstance(obs, GestureBeat) or number is None:
            return dict(record, reason="place_in_bar_not_known"), None
        if armed_at is None or obs.stamp <= max(armed_at, self.boundary if self.boundary is not None else armed_at):
            return dict(record, reason="before_prepare"), None
        if now - obs.stamp > 0.8 or obs.stamp > now + 0.001:
            return dict(record, reason="stale"), None
        if self.plan is not None:
            plan = self.plan
            if (number == 1 and plan.downstroke is None and obs.stamp > plan.cues[-1].stamp
                    and abs(obs.stamp - plan.start_s) <= 0.5 * plan.period_s):
                # The conductor's downstroke is the start, not our prediction of it. It arrived
                # before the predicted time ran out: start from where the hand really was.
                self.plan = GesturePlan(plan.cues, plan.period_s, obs.stamp, now, plan.boundary_s, plan.generation, obs)
                return dict(record, accepted=True, reason="start_on_the_conductors_downstroke",
                            predicted_start_s=plan.start_s, scheduled_start_s=obs.stamp), None
            return dict(record, reason="awaiting_scheduled_start"), None
        if number == 1:
            self.cues = [obs]
            self.last_reason = "bar_started"
            return dict(record, accepted=True, count=1, reason="bar_started"), None
        if not self.cues:
            if obs.kind == "lift":
                self.previous_lift = obs.stamp
            self.last_reason = "waiting_first_downbeat"
            return dict(record, reason="no_downbeat_yet"), None
        if obs.stamp <= self.cues[-1].stamp or number <= self.cues[-1].number:
            return dict(record, reason="out_of_order"), None
        if obs.kind != "lift":
            self.cues.append(obs)
            self.last_reason = "counting"
            return dict(record, accepted=True, count=len(self.cues), reason="counting"), None
        inside = (obs.stamp - self.cues[0].stamp) / (self.meter - 1)      # from this bar alone
        before, self.previous_lift = self.previous_lift, obs.stamp
        whole = (obs.stamp - before) / self.meter if before is not None else None   # lift to lift: one whole bar
        if not 60.0 / self.MAX_BPM <= inside <= 60.0 / self.MIN_BPM:
            self.cues = []
            self.last_reason = "bar_tempo_out_of_range"
            return dict(record, reason="bar_tempo_out_of_range", tempo_candidate_bpm=60.0 / inside if inside > 0 else None), None
        # One bar is two or three intervals of a human hand: not enough alone. It counts when the
        # bar before agrees (two bars), or when the tempo hint agrees AND a middle beat was seen in
        # its place. Without the middle beat, "hand down, wait, hand up" after Prepare would look
        # like beat 1 and a lift and start the piece while the conductor is still getting ready.
        middle = [c for c in self.cues[1:] if 0.5 <= (c.stamp - self.cues[0].stamp) / (inside * (c.number - 1)) <= 1.5]
        if whole is not None and abs(whole / inside - 1.0) <= self.AGREE:
            period, basis = whole, "two_bars"
        elif middle and self.hint_period is not None and abs(inside / self.hint_period - 1.0) <= self.AGREE:
            period, basis = inside, "one_bar_agrees_with_hint"
        elif middle and self.hint_period is None and whole is None:
            period, basis = inside, "one_bar"
        else:
            self.cues = []
            self.last_reason = "tempo_not_confirmed_next_bar_counts"
            return dict(record, reason="tempo_not_confirmed_next_bar_counts", tempo_candidate_bpm=60.0 / inside,
                        bar_before_bpm=60.0 / whole if whole else None, middle_beats_seen=len(middle)), None
        if basis == "two_bars":
            start = obs.stamp + period          # lift to lift is one whole bar: that tempo, one beat after this lift
        else:
            # This bar alone. Its lift came early (LIFT_EARLY), so the beat is longer than downstroke to
            # lift suggests, and the next downstroke comes one whole bar after this bar's downstroke.
            period = (obs.stamp - self.cues[0].stamp) / ((self.meter - 1) - self.LIFT_EARLY)
            start = self.cues[0].stamp + self.meter * period
        cues = tuple(self.cues) + (obs,)
        self.plan = GesturePlan(cues, period, start, now, self.boundary if self.boundary is not None else armed_at,
                                self.generation)
        self.cues = list(cues)          # still shown as "counted" until the next bar begins
        self.last_reason = "preparation_complete_next_beat_predicted"
        return dict(record, accepted=True, count=len(cues), tempo_bpm=60.0 / period, tempo_basis=basis,
                    bar_tempo_bpm=60.0 / inside,
                    reason="preparation_complete_next_beat_predicted", scheduled_start_s=self.plan.start_s,
                    preparation_cues=len(cues), start_origin="predicted_count_in",
                    intervals_s=[b.stamp - a.stamp for a, b in zip(cues, cues[1:])]), 60.0 / period


class GestureClock(MusicalClock):
    STALE_S = 0.8                # a slow rebound or an arrival can take this long to become certain
    ANCHOR_GAIN = 0.6
    MIDDLE_GAIN = 0.25
    TEMPO_GAIN = 0.5
    START_RELOCK = 0.75          # beats: the first downstroke this near a predicted start is the start

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.anchors = {}            # "one" / "lift" -> (stamp, beat position) of the last one seen
        self.disagreement = None     # (stamp, error) of an anchor that pointed at another bar position
        self.relocks = 0
        self.last_numbered = None    # stamp of the last beat whose number was known
        self.start_downstroke_due = False   # started on a prediction; the conductor's first downstroke is still to come
        self.bars_measured = 0       # whole bars measured since the start or since the last silence

    # ── start from a gesture count-in ──
    def start_prepared(self, now, plan):
        if not isinstance(plan, GesturePlan):
            return super().start_prepared(now, plan)
        seen = plan.downstroke
        ok = (len(plan.cues) >= 2 and plan.cues[0].number == 1 and plan.cues[-1].kind == "lift"
              and all(math.isfinite(v) for v in (plan.period_s, plan.start_s, plan.available_s))
              and 60.0 / 180 <= plan.period_s <= 60.0 / 30
              and self.armed_at is not None and plan.cues[0].stamp > self.armed_at
              and plan.available_s <= now + 0.001
              and (plan.cues[-1].stamp + 0.5 * plan.period_s <= plan.start_s <= plan.cues[-1].stamp + 1.5 * plan.period_s
                   if seen is None else
                   isinstance(seen, GestureBeat) and seen.number == 1 and seen.stamp == plan.start_s
                   and seen.stamp > plan.cues[-1].stamp))
        if not ok:
            self.last_start_rejection = "invalid_preparation_plan"
            return []
        if now < plan.start_s:
            self.last_start_rejection = "before_preparation_deadline"
            return []
        # a predicted start must be taken on time; a start on the downstroke itself is known only
        # once the hand has rebounded, so it may be that much older
        if now - plan.start_s > (min(0.1, 0.25 * plan.period_s) if seen is None else 0.5 * plan.period_s):
            self.last_start_rejection = "missed_preparation_deadline"
            return []
        self._validated_preparation = plan
        try:
            out = self.start(now, "predicted_count_in_boundary", preparation=plan)
        finally:
            self._validated_preparation = None
        if out:
            self.anchors = {"one": (plan.cues[0].stamp, -self.meter), "lift": (plan.cues[-1].stamp, -1)}
            self.disagreement = None
            self.last_numbered = plan.cues[-1].stamp
            self.bars_measured = 0
            self.tempo_evidence = "gesture_preparation_bar"
            self.start_downstroke_due = seen is None
            if seen is not None:
                # that downstroke is beat 1 of the piece and has been used: it is not observed again
                self.anchors["one"] = (seen.stamp, 0)
                self.last_received_stamp = seen.stamp
                self.last_observed = seen.stamp
                self.start_visual_confirmed = True
        return out

    # ── observations ──
    def observe(self, obs, now):
        self.advance(now)
        number = getattr(obs, "number", None)
        record = dict(stamp=obs.stamp, received=obs.received, processed=now, frame=obs.frame, source_s=obs.source_s,
                      kind=obs.kind, number=number, confidence=obs.confidence, accepted=False, phase_adjusted=False,
                      bpm_before=self.bpm, bpm_after=self.bpm, decision_stamp=obs.decision_stamp,
                      decision_frame=obs.decision_frame)
        reason = None
        if not all(math.isfinite(v) for v in (obs.stamp, obs.received, obs.confidence)):
            reason = "invalid"
        elif self.state != "running":
            reason = "inactive"
        elif obs.predicted or obs.origin != "vision":
            reason = "not_visual_evidence"
        elif self.last_received_stamp is not None and obs.stamp <= self.last_received_stamp:
            reason = "duplicate_or_out_of_order"
        elif obs.stamp > now + 0.001 or now - obs.stamp > self.STALE_S:
            reason = "future_or_stale"
        elif obs.confidence < 0.65:
            reason = "low_quality"
        if reason:
            self.rejected += 1
            return dict(record, reason=reason)
        self.last_received_stamp = obs.stamp
        self.last_observed = obs.stamp
        self.last_observed_kind = obs.kind
        if number is None:
            # a real motion, but the gesture does not say which beat: it supports "the conductor is
            # there" and changes nothing
            self.accepted += 1
            return dict(record, accepted=True, reason="seen_place_in_bar_unknown")
        k = number - 1
        # how long the clock has been running on its own: after more than a bar and a half without a
        # single numbered beat (the conductor stopped and is coming back) it has no claim to the bar
        quiet = self.last_numbered is not None and obs.stamp - self.last_numbered > 1.5 * self.meter * self.period
        self.last_numbered = obs.stamp
        observed_phase = self._phase_at(obs.stamp)
        target = self.meter * round((observed_phase - k) / self.meter) + k
        error = target - observed_phase
        anchor = number == 1 or obs.kind == "lift"
        record.update(target_beat_position=target, phase_error_beats=error)
        self.last_observed_position = target
        self.accepted += 1
        record["accepted"] = True
        record["reason"] = "phase_only"
        if (self.predicted_start_stamp is not None and not self.start_visual_confirmed and number == 1
                and abs(obs.stamp - self.predicted_start_stamp) <= 0.5 * self.period):
            self.start_visual_confirmed = True
            record["confirms_predicted_start"] = True
            record["predicted_start_s"] = self.predicted_start_stamp
        period = self.period
        if anchor:
            name = "one" if number == 1 else "lift"
            previous = self.anchors.get(name)
            self.anchors[name] = (obs.stamp, target)
            if previous is not None:
                # The same event one bar earlier: the time between them is one bar, whatever the
                # clock currently believes about the place in the bar.
                dt = obs.stamp - previous[0]
                bars = dt / (self.meter * period)
                record.update(interval_s=dt, bars_between=bars)
                if 0.75 <= bars <= 1.33:
                    measured = dt / self.meter
                    if 30 <= 60.0 / measured <= 180:
                        # The tempo at the start comes from one preparation bar, and after a silence it
                        # is old: the first whole bar measured replaces most of it, later ones refine it.
                        gain = (0.9, 0.7)[self.bars_measured] if self.bars_measured < 2 else self.TEMPO_GAIN
                        self.bars_measured += 1
                        self.bpm = 60.0 / (period + gain * (measured - period))
                        self.tempo_evidence = "bar_anchor_interval"
                        record.update(measured_period_s=measured, reason="tempo_and_phase")
                elif 1.75 <= bars <= 2.3:
                    measured = dt / (2 * self.meter)
                    self.bpm = 60.0 / (period + 0.3 * (measured - period))
                    self.tempo_evidence = "bar_anchor_interval_two_bars"
                    record.update(measured_period_s=measured, reason="tempo_and_phase")
                else:
                    record["reason"] = "interval_not_a_bar_tempo_held"
        horizon = max(0.5, period)
        first_downstroke = self.start_downstroke_due and number == 1
        if first_downstroke:
            self.start_downstroke_due = False
        if first_downstroke and target == 0 and abs(error) <= self.START_RELOCK:
            # The start was a prediction from the preparation bar (good to about 0.15 s, sometimes
            # 0.3 s). This is where the conductor's first downstroke really was: follow it at once
            # and in full. There is no earlier bar line to defend against a single event.
            self._slew(error, period * max(1.0, abs(error) / 0.8), now, 0.80)
            self.disagreement = None
            self.bar_position_ambiguous = False
            self.start_visual_confirmed = True
            record.update(phase_adjusted=True, confirms_predicted_start=True, predicted_start_s=self.predicted_start_stamp,
                          reason="start_relocked_on_the_first_downstroke")
        elif abs(error) <= 0.5:
            if anchor:
                self.disagreement = None
                self.bar_position_ambiguous = False
            gain = self.ANCHOR_GAIN if anchor else self.MIDDLE_GAIN
            shift = max(-0.25, min(0.25, error * gain))
            self._slew(shift, horizon, now, 0.30)
            record["phase_adjusted"] = True
        elif anchor and quiet:
            # Coming back after a silence: the gesture's bar is the bar. Follow it at once.
            self._slew(error, period * max(1.0, abs(error) / 0.8), now, 0.80)
            self.disagreement = None
            self.bars_measured = 0
            self.relocks += 1
            self.bar_position_ambiguous = False
            record.update(phase_adjusted=True, reason="bar_position_relocked_after_silence")
        elif anchor:
            # The gesture and the clock disagree about the place in the bar. One such event can be a
            # false lift or a stray downstroke; two in a row that agree with each other are the truth.
            last = self.disagreement
            if (last is not None and obs.stamp - last[0] <= 2.5 * self.meter * period
                    and last[1] * error > 0 and abs(last[1] - error) <= 0.4):
                self._slew(error, self.meter * period, now, 0.50)
                self.disagreement = None
                self.relocks += 1
                self.bar_position_ambiguous = False
                record.update(phase_adjusted=True, reason="bar_position_relocked")
            else:
                self.disagreement = (obs.stamp, error)
                self.bar_position_ambiguous = True
                record["reason"] = "bar_position_disagrees_waiting_for_confirmation"
        else:
            record["reason"] = "middle_beat_far_from_clock_ignored"
        record["bpm_after"] = self.bpm
        return record

    def _slew(self, shift_beats, horizon_s, now, limit):
        """Remove `shift_beats` of phase error over `horizon_s` by a bounded change of rate. Future only."""
        base = self.bpm / 60.0
        rate = max(-limit * base, min(limit * base, shift_beats / horizon_s))
        rate = max(0.5 - base, min(3.0 - base, rate))
        self.correction = rate
        self.correction_end = now + (abs(shift_beats / rate) if rate else 0.0)
