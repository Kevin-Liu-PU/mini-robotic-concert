"""Offline tests for the gesture beats, the rolling count-in and the bar clock.

Synthetic wrist paths shaped like the conductor's recorded three-beat and
four-beat patterns. No camera, model, network, audio or robot.
"""
import math
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "conductor"))

from conductor_rt.gesture_beats import GestureBeat, GestureBeats, body_wrist      # noqa: E402
from conductor_rt.gesture_clock import GestureClock, GestureCountIn, GesturePlan  # noqa: E402

FPS = 30.0


def path(keys, fps=FPS):
    """Smooth path through (t, x, y) keys; the hand is at rest at every key. Returns [(t, x, y)]."""
    out = []
    t = keys[0][0]
    i = 0
    while t <= keys[-1][0] + 1e-9:
        while i + 1 < len(keys) - 1 and t > keys[i + 1][0]:
            i += 1
        (t0, x0, y0), (t1, x1, y1) = keys[i], keys[i + 1]
        u = 0.0 if t1 == t0 else min(1.0, max(0.0, (t - t0) / (t1 - t0)))
        s = 0.5 - 0.5 * math.cos(math.pi * u)
        out.append((t, x0 + (x1 - x0) * s, y0 + (y1 - y0) * s))
        t += 1.0 / fps
    return out


def three_beat(bars, period=60 / 70, start=2.0, rebound=0.6, second_low=1.15, top=-0.6):
    """Top -> down to the inner side (1) -> swing outwards through a low point (2) -> lift (3) -> top."""
    keys = [(0.0, 0.7, top), (start - 0.35 * period, 0.7, top)]
    truth = []
    for b in range(bars):
        t0 = start + b * 3 * period
        keys += [(t0, -0.8, 1.0), (t0 + 0.4 * period, -0.5, rebound), (t0 + 1.0 * period, 0.5, second_low),
                 (t0 + 1.35 * period, 1.3, 1.0), (t0 + 1.75 * period, 0.8, 1.1), (t0 + 2.25 * period, 0.7, top),
                 (t0 + 2.65 * period, 0.7, top)]
        truth += [(t0, 1), (t0 + period, 2), (t0 + 2.0 * period, 3)]
    keys.append((keys[-1][0] + 1.5, 0.7, top))
    return path(keys), truth


def four_beat(bars, period=60 / 70, start=2.0, top=-0.6, rebound1=0.1):
    """Top -> centre low (1) -> inner low (2) -> outer low (3) -> lift (4) -> top."""
    keys = [(0.0, 0.5, top), (start - 0.4 * period, 0.5, top)]
    truth = []
    for b in range(bars):
        t0 = start + b * 4 * period
        keys += [(t0, 0.3, 0.65), (t0 + 0.45 * period, 0.2, rebound1), (t0 + 1.0 * period, -0.8, 0.6),
                 (t0 + 1.5 * period, 0.0, 0.2), (t0 + 2.0 * period, 0.95, 0.62), (t0 + 2.4 * period, 0.8, 0.5),
                 (t0 + 2.75 * period, 0.6, 0.58), (t0 + 3.3 * period, 0.5, top), (t0 + 3.6 * period, 0.5, top)]
        truth += [(t0, 1), (t0 + period, 2), (t0 + 2 * period, 3), (t0 + 3.025 * period, 4)]   # half way up the lift
    keys.append((keys[-1][0] + 1.5, 0.5, top))
    return path(keys), truth


def detect(samples, meter, bpm=70.0, holes=()):
    det = GestureBeats(meter, bpm)
    found = []
    for i, (t, x, y) in enumerate(samples):
        ok = not any(a <= t <= b for a, b in holes)
        found += det.step(t, x, y, ok, frame=i)
    return found, det


def matched(found, truth, tolerance=0.07, lift_tolerance=0.10):
    """Every true beat has exactly one detected beat with the same number within the tolerance.

    A lowest point is a sharp instant. "Half way up the lift" depends on where the hand took off
    and how high the pattern is, so the lift gets a little more room.
    """
    left = list(found)
    for t, n in truth:
        near = [b for b in left if b.number == n and abs(b.stamp - t) <= (lift_tolerance if b.kind == "lift" else tolerance)]
        if len(near) != 1:
            return False, (t, n, [(round(b.stamp, 2), b.number) for b in found if abs(b.stamp - t) < 0.5])
        left.remove(near[0])
    return True, left


# ── the gesture numbers its own beats ────────────────────────────────

@pytest.mark.parametrize("bpm", [56, 70, 92])
def test_three_beat_pattern_numbers_itself(bpm):
    samples, truth = three_beat(6, 60 / bpm)
    found, det = detect(samples, 3, 70.0)
    ok, rest = matched(found, truth)
    assert ok, rest
    assert rest == []                                            # and nothing else was reported
    assert [b.kind for b in found if b.number == 3] == ["lift"] * 6


@pytest.mark.parametrize("bpm", [60, 70, 88])
def test_four_beat_pattern_numbers_itself(bpm):
    samples, truth = four_beat(5, 60 / bpm)
    found, det = detect(samples, 4, 70.0)
    ok, rest = matched(found, truth)
    assert ok, rest
    assert rest == []


def test_evidence_time_and_decision_time_are_kept_apart():
    samples, truth = three_beat(3)
    found, _ = detect(samples, 3)
    for b in found:
        assert b.decision_stamp >= b.stamp                      # known later than it happened, never earlier
        assert b.decision_stamp - b.stamp < 0.35
        assert b.origin == "vision" and not b.predicted


def test_high_rebound_after_beat_one_is_not_a_second_downstroke():
    # labelled four-beat take: the rebound of beat 1 sometimes reaches the height of the top
    samples, truth = four_beat(4, rebound1=-0.45)
    found, _ = detect(samples, 4)
    ones = [b for b in found if b.number == 1]
    assert len(ones) == 4
    ok, rest = matched(found, truth)
    assert ok, rest


def test_hand_that_arrives_and_stays_is_a_beat_without_any_rebound():
    # beat 1 as "down and stop", then a second descent for beat 2
    period = 60 / 70
    keys = [(0.0, 0.7, -0.6), (1.7, 0.7, -0.6), (2.0, 0.6, 0.6), (2.0 + 0.8 * period, 0.6, 0.6),
            (2.0 + period, -0.6, 1.3), (2.0 + 1.3 * period, -0.5, 1.15), (2.0 + 1.75 * period, 0.6, 1.2),
            (2.0 + 2.25 * period, 0.7, -0.6), (6.0, 0.7, -0.6)]
    found, _ = detect(path(keys), 3)
    assert [b.number for b in found] == [1, 2, 3]
    assert abs(found[0].stamp - 2.0) < 0.08 and "arrived" in found[0].note
    assert abs(found[1].stamp - (2.0 + period)) < 0.07


def test_small_wobbles_and_the_dip_before_the_lift_are_not_beats():
    samples, truth = three_beat(4)
    found, det = detect(samples, 3)
    assert len(found) == 12
    assert any(e["what"] in ("small_low", "extra_low") for e in det.trace)       # the take-off dip was seen and dropped


def test_tracking_gap_forgets_the_place_in_the_bar_until_the_next_lift():
    samples, truth = three_beat(5)
    period = 60 / 70
    gap = (2.0 + 3 * period + 0.3 * period, 2.0 + 3 * period + 0.9 * period)     # hand not seen during bar 2
    found, _ = detect(samples, 3, holes=[gap])
    second_bar = [b for b in found if gap[1] < b.stamp < 2.0 + 6 * period - 0.5 * period]
    assert all(b.number in (None, 3) for b in second_bar)                        # no guessed number
    later = [b for b in found if b.stamp > 2.0 + 6 * period - 0.2]
    assert [b.number for b in later][:6] == [1, 2, 3, 1, 2, 3]


def test_body_coordinates_need_only_shoulders_and_the_wrist():
    pose = [[0.5, 0.5, 0.0]] * 33
    pose = [list(p) for p in pose]
    pose[11] = [0.40, 0.40, 0.99]; pose[12] = [0.60, 0.40, 0.99]; pose[15] = [0.50, 0.60, 0.95]
    x, y, ok = body_wrist(pose, 0, 4 / 3)
    assert ok and x == pytest.approx(0.0, abs=1e-9) and y == pytest.approx(0.2 / (0.2 * 4 / 3), abs=1e-9)
    pose[15][2] = 0.2
    assert body_wrist(pose, 0, 4 / 3)[2] is False                # wrist not reliable: no sample
    assert body_wrist(None, 0, 4 / 3)[2] is False


# ── rolling count-in ─────────────────────────────────────────────────

def beat(number, stamp, meter=3, kind=None):
    kind = kind or ("lift" if number == meter else "low_point")
    return GestureBeat(stamp, stamp + 0.03, 0.9, kind, int(stamp * 30), stamp, decision_stamp=stamp + 0.05,
                       decision_frame=int(stamp * 30) + 2, number=number)


def feed(count, beats, armed_at=0.0):
    plan_bpm = None
    for b in beats:
        record, estimated = count.observe(b, b.stamp + 0.06, armed_at)
        plan_bpm = estimated or plan_bpm
    return plan_bpm


def first_bar(t1, T, meter=3):
    """A bar as it looks when the hand comes from rest: the lift is stamped 0.13 beat before its place."""
    early = GestureCountIn.LIFT_EARLY
    return [beat(n, t1 + (n - 1) * T, meter) for n in range(1, meter)] + [beat(meter, t1 + (meter - 1 - early) * T, meter)]


def test_a_bar_alone_starts_the_piece_one_bar_after_its_downstroke():
    # measured: in a bar that follows a rest the lift comes 0.13 beat early, so "one beat after the
    # lift" at the bar's own downstroke-to-lift tempo was too early (0.37 s in one live take)
    count = GestureCountIn(3, 70.0)
    count.restart(0.0, 0.0, "operator_prepare")
    T = 60 / 72
    bpm = feed(count, first_bar(1.0, T))
    assert bpm == pytest.approx(72, abs=0.01) and count.stage == "scheduled"
    assert count.plan.start_s == pytest.approx(1.0 + 3 * T) and count.plan.period_s == pytest.approx(T)
    # four beats alike
    count = GestureCountIn(4, 70.0)
    count.restart(0.0, 0.0, "operator_prepare")
    assert feed(count, first_bar(1.0, T, 4)) == pytest.approx(72, abs=0.01)
    assert count.plan.start_s == pytest.approx(1.0 + 4 * T)


def test_a_bar_from_a_live_take_starts_on_time():
    # times since the camera started. The hand's next downstroke came at 16.58 s (metronome: 70 BPM).
    count = GestureCountIn(3, 70.0)
    count.restart(6.18, 6.18, "auto_prepare")
    record, bpm = None, None
    for b in (beat(3, 12.61), beat(1, 14.01), beat(2, 15.08), beat(3, 15.48)):
        record, bpm = count.observe(b, b.stamp + 0.08, 6.18)
    assert record["tempo_basis"] == "one_bar_agrees_with_hint"
    assert record["bar_tempo_bpm"] == pytest.approx(81.6, abs=0.3)       # what the bar itself shows, and what was used before
    assert bpm == pytest.approx(76.3, abs=0.3)
    assert count.plan.start_s == pytest.approx(16.37, abs=0.01)           # was 16.21


def test_two_bars_start_one_beat_after_the_lift_at_the_tempo_from_lift_to_lift():
    count = GestureCountIn(3, 70.0)
    count.restart(0.0, 0.0)
    T = 60 / 66
    assert feed(count, [beat(1, 1.0), beat(3, 1.0 + 2 * T)]) is None
    assert feed(count, [beat(1, 1.0 + 3 * T), beat(3, 1.0 + 5 * T)]) == pytest.approx(66)
    assert count.plan.start_s == pytest.approx(1.0 + 6 * T) and count.plan.period_s == pytest.approx(T)


def test_hand_down_then_up_after_prepare_is_not_a_bar():
    # after the Prepare gesture the hand drops to rest and is raised again a moment later: that is
    # a downstroke and a lift about two beats apart, and it must not start the piece
    count = GestureCountIn(3, 70.0)
    count.restart(0.0, 0.0)
    assert feed(count, [beat(1, 1.0), beat(3, 1.0 + 2 * 60 / 70)]) is None
    assert count.plan is None and count.last_reason == "tempo_not_confirmed_next_bar_counts"
    # the conductor then holds the hand up for a while and gives a real bar: that bar counts
    T = 60 / 70
    assert feed(count, first_bar(6.0, T)) == pytest.approx(70)
    assert count.plan.start_s == pytest.approx(6.0 + 3 * T)


def test_two_bars_without_a_middle_beat_still_count():
    # the middle beat is the least clear one; two bars whose length agrees are enough without it
    count = GestureCountIn(3, 70.0)
    count.restart(0.0, 0.0)
    T = 60 / 70
    assert feed(count, [beat(1, 1.0), beat(3, 1.0 + 2 * T)]) is None
    assert feed(count, [beat(1, 1.0 + 3 * T), beat(3, 1.0 + 5 * T)]) == pytest.approx(70)
    assert count.plan.start_s == pytest.approx(1.0 + 6 * T)


def test_a_middle_beat_in_the_wrong_place_does_not_vouch_for_the_bar():
    count = GestureCountIn(3, 70.0)
    count.restart(0.0, 0.0)
    T = 60 / 70
    assert feed(count, [beat(1, 1.0), beat(2, 1.0 + 0.3 * T), beat(3, 1.0 + 2 * T)]) is None


def test_a_bar_at_another_tempo_than_the_hint_needs_a_second_bar():
    count = GestureCountIn(4, 90.0)                              # the labelled take: hint 90, conducted at 66
    count.restart(0.0, 0.0)
    T = 60 / 66
    first = [beat(1, 1.0, 4), beat(2, 1.0 + T, 4), beat(3, 1.0 + 2 * T, 4), beat(4, 1.0 + 3 * T, 4)]
    assert feed(count, first) is None and count.last_reason == "tempo_not_confirmed_next_bar_counts"
    second = [beat(n, 1.0 + (3 + n) * T, 4) for n in (1, 2, 3, 4)]
    assert feed(count, second) == pytest.approx(66, abs=0.5)
    assert count.plan.start_s == pytest.approx(1.0 + 8 * T, abs=1e-6)


def test_after_a_bad_bar_the_next_bar_simply_counts():
    # old count-in: one uneven cue, then every later cue was refused until the hand stood still
    count = GestureCountIn(3, 70.0)
    count.restart(0.0, 0.0)
    T = 60 / 70
    assert feed(count, [beat(1, 1.0), beat(2, 1.0 + T)]) is None          # no lift: the bar never finished
    assert count.tick(1.0 + T + 3.2, 0.0)["reason"] == "bar_not_finished"
    assert feed(count, first_bar(7.0, T)) == pytest.approx(70)


def test_the_lift_that_raises_the_hand_before_the_first_bar_starts_nothing():
    count = GestureCountIn(3, 70.0)
    count.restart(0.0, 0.0)
    record, bpm = count.observe(beat(3, 0.8), 0.86, 0.0)
    assert bpm is None and record["reason"] == "no_downbeat_yet" and count.plan is None


def test_beats_from_before_prepare_do_not_count():
    count = GestureCountIn(3, 70.0)
    count.restart(5.0, 5.0)
    record, bpm = count.observe(beat(1, 4.9), 5.05, 5.0)
    assert bpm is None and record["reason"] == "before_prepare"


def test_a_downstroke_that_comes_before_the_predicted_start_is_the_start():
    # simulated from a recorded take: the preparation bars gave 61 BPM, the real downstroke came
    # 0.23 s before the predicted time. The piece starts where the hand really was.
    clock = GestureClock(70.0, 3, "test", score_beats=None)
    count = GestureCountIn(3, 70.0)
    clock.arm(0.5, "keyboard_prepare")
    count.restart(0.5, clock.armed_at)
    T = 60 / 64
    feed(count, [beat(1, 1.0), beat(2, 1.0 + T), beat(3, 1.0 + 2 * T)], clock.armed_at)
    predicted = count.plan.start_s
    down = beat(1, predicted - 0.23)
    record, _ = count.observe(down, down.stamp + 0.12, clock.armed_at)        # known 120 ms after it happened
    assert record["reason"] == "start_on_the_conductors_downstroke" and count.plan.start_s == down.stamp
    out = clock.start_prepared(down.stamp + 0.125, count.plan)
    assert [m["kind"] for m in out] == ["start", "beat"]
    assert clock.phase == pytest.approx(0.125 / T, abs=0.01)                  # already this far into beat 1
    again = clock.observe(down, down.stamp + 0.13)                            # the same downstroke is not counted twice
    assert not again["accepted"] and again["reason"] == "duplicate_or_out_of_order"
    assert not clock.start_downstroke_due                                     # the start already is the conductor's downstroke


def test_a_downstroke_long_before_the_predicted_start_changes_nothing():
    count = GestureCountIn(3, 70.0)
    count.restart(0.5, 0.5)
    T = 60 / 70
    feed(count, [beat(1, 1.0), beat(2, 1.0 + T), beat(3, 1.0 + 2 * T)], 0.5)
    plan = count.plan
    early = beat(1, plan.start_s - 0.6 * T)
    record, _ = count.observe(early, early.stamp + 0.05, 0.5)
    assert record["reason"] == "awaiting_scheduled_start" and count.plan is plan


# ── bar clock ────────────────────────────────────────────────────────

def started_clock(meter=3, bpm=70.0, t1=1.0):
    clock = GestureClock(bpm, meter, "test", score_beats=None)
    count = GestureCountIn(meter, bpm)
    clock.arm(0.5, "keyboard_prepare")
    count.restart(0.5, clock.armed_at)
    T = 60 / bpm
    feed(count, first_bar(t1, T, meter), clock.armed_at)
    plan = count.plan
    assert plan.period_s == pytest.approx(T) and plan.start_s == pytest.approx(t1 + meter * T)
    assert clock.start_prepared(plan.start_s - 0.01, plan) == [] and clock.last_start_rejection == "before_preparation_deadline"
    out = clock.start_prepared(plan.start_s + 0.004, plan)
    assert [m["kind"] for m in out] == ["start", "beat"] and clock.state == "running"
    return clock, plan


def run(clock, beats, until, step=0.005):
    """Deliver beats 60 ms after they happened; return [(time, beat_index)] of the clock's own beats."""
    out = []
    now = clock.now
    pending = sorted(beats, key=lambda b: b.stamp)
    records = []
    while now < until:
        now += step
        while pending and pending[0].stamp + 0.06 <= now:
            records.append(clock.observe(pending.pop(0), now))
        for m in clock.tick(now):
            if m["kind"] == "beat":
                out.append((now, m["beat_index"]))
    return out, records


def test_clock_follows_a_steady_conductor_and_never_refuses_the_lift():
    clock, plan = started_clock()
    T = 60 / 70
    beats = [beat(1 + k % 3, plan.start_s + k * T) for k in range(30)]
    ticks, records = run(clock, beats, plan.start_s + 30 * T)
    assert all(r["accepted"] for r in records)
    assert not any("outside" in r["reason"] for r in records)
    lines = [t for t, i in ticks if i % 3 == 0]
    assert all(abs(t - (plan.start_s + k * 3 * T)) < 0.03 for k, t in enumerate(lines, 1))
    assert clock.bpm == pytest.approx(70, abs=0.3)


def test_tempo_follows_the_length_of_the_bar():
    clock, plan = started_clock()
    slow = 60 / 60
    beats = [beat(1 + k % 3, plan.start_s + k * slow) for k in range(36)]
    ticks, records = run(clock, beats, plan.start_s + 36 * slow)
    assert clock.bpm == pytest.approx(60, abs=1.0)
    lines = [t for t, i in ticks if i % 3 == 0][-3:]
    truth = [plan.start_s + k * 3 * slow for k in range(1, 13)]
    assert all(min(abs(t - x) for x in truth) < 0.08 for t in lines)


def test_one_stray_downstroke_does_not_move_the_clock():
    clock, plan = started_clock()
    T = 60 / 70
    beats = [beat(1 + k % 3, plan.start_s + k * T) for k in range(18)]
    beats.append(beat(1, plan.start_s + 7.5 * T))                 # a downstroke where beat 2/3 should be
    ticks, records = run(clock, beats, plan.start_s + 18 * T)
    stray = [r for r in records if abs(r["stamp"] - (plan.start_s + 7.5 * T)) < 1e-6][0]
    assert stray["reason"] == "bar_position_disagrees_waiting_for_confirmation" and not stray["phase_adjusted"]
    lines = [t for t, i in ticks if i % 3 == 0]
    assert all(abs(t - (plan.start_s + k * 3 * T)) < 0.05 for k, t in enumerate(lines, 1))
    assert clock.relocks == 0


def test_two_bars_that_agree_move_the_bar_line_instead_of_being_thrown_away():
    # the conductor skips one beat (or the clock started one beat off): the old clock then refused
    # every lift as "outside the expected last beat"; this one follows the gesture within two bars
    clock, plan = started_clock()
    T = 60 / 70
    shift = plan.start_s + T                                       # the conductor's bars begin one beat later
    beats = [beat(1 + k % 3, shift + k * T) for k in range(24)]
    ticks, records = run(clock, beats, shift + 24 * T)
    assert clock.relocks >= 1
    lines = [t for t, i in ticks if i % 3 == 0][-3:]
    truth = [shift + k * 3 * T for k in range(0, 9)]
    assert all(min(abs(t - x) for x in truth) < 0.06 for t in lines)
    assert all(r["accepted"] for r in records)


def test_after_a_silence_the_clock_follows_the_first_bar_that_comes_back():
    # labelled take: the conductor stops for five seconds, hand up, and starts again somewhere else in the bar
    clock, plan = started_clock()
    T = 60 / 70
    first = [beat(1 + k % 3, plan.start_s + k * T) for k in range(9)]
    back = plan.start_s + 9 * T + 4.3                              # not a whole number of bars later
    second = [beat(1 + k % 3, back + k * T) for k in range(12)]
    ticks, records = run(clock, first + second, back + 12 * T)
    assert [r["reason"] for r in records if "silence" in r["reason"]] == ["bar_position_relocked_after_silence"]
    lines = [t for t, i in ticks if i % 3 == 0 and t > back + 3 * T]    # from the second bar after the return
    truth = [back + k * 3 * T for k in range(5)]
    assert lines and all(min(abs(t - x) for x in truth) < 0.08 for t in lines)


def test_a_beat_whose_place_is_unknown_changes_nothing():
    clock, plan = started_clock()
    before = (clock.bpm, clock.correction)
    record = clock.observe(GestureBeat(plan.start_s + 0.3, plan.start_s + 0.33, 0.7, "low_point", 5, 0.0, number=None),
                           plan.start_s + 0.36)
    assert record["accepted"] and record["reason"] == "seen_place_in_bar_unknown" and not record["phase_adjusted"]
    assert (clock.bpm, clock.correction) == before


def test_stale_and_stopped_are_still_refused():
    clock, plan = started_clock()
    old = clock.observe(beat(2, plan.start_s + 0.2), plan.start_s + 1.2)
    assert not old["accepted"] and old["reason"] == "future_or_stale"
    clock.stop(plan.start_s + 1.3, "manual")
    after = clock.observe(beat(3, plan.start_s + 1.25), plan.start_s + 1.31)
    assert not after["accepted"] and after["reason"] == "inactive"
    assert clock.start_prepared(plan.start_s + 1.4, plan) == []   # stopped stays stopped


def test_logged_beats_carry_no_hand_position():
    from conductor_rt.runner import beat_record
    row = beat_record(GestureBeat(1.0, 1.03, 0.9, "low_point", 30, 1.0, number=1, x=-0.8, y=1.0, drop=1.5))
    assert "x" not in row and "y" not in row and row["number"] == 1 and row["kind"] == "low_point"


# ── the first downstroke after a predicted start ──
def clock_started(early_beats, meter=3, bpm=70.0, t1=1.0):
    """A running clock whose predicted start lies `early_beats` before the conductor's real downstroke, which
    comes one steady bar after the downstroke of the preparation bar. Returns the clock and that real time."""
    clock = GestureClock(bpm, meter, "test", score_beats=None)
    clock.arm(0.5, "keyboard_prepare")
    T = 60 / bpm
    cues = tuple(first_bar(t1, T, meter))
    real = t1 + meter * T
    plan = GesturePlan(cues, T, real - early_beats * T, cues[-1].stamp + 0.06, 0.5, 1)
    assert [m["kind"] for m in clock.start_prepared(plan.start_s + 0.004, plan)] == ["start", "beat"]
    assert clock.start_downstroke_due
    return clock, real


def test_the_first_downstroke_after_a_predicted_start_moves_the_bar_line_onto_itself():
    # from a live take: the start was predicted 0.37 s early, the hand's downstroke was then
    # 0.501 beat from the clock and was set aside; the bar line needed three bars to reach the hand
    clock, real = clock_started(0.6)
    T = 60 / 70
    beats = [beat(1 + k % 3, real + k * T) for k in range(9)]
    ticks, records = run(clock, beats, real + 9 * T)
    first = records[0]
    assert first["reason"] == "start_relocked_on_the_first_downstroke" and first["phase_adjusted"]
    assert first["phase_error_beats"] == pytest.approx(-0.6, abs=0.01) and first["confirms_predicted_start"]
    assert clock.start_visual_confirmed and not clock.bar_position_ambiguous and not clock.start_downstroke_due
    lines = [t for t, i in ticks if i % 3 == 0]
    assert abs(lines[0] - (real + 3 * T)) < 0.04 and abs(lines[1] - (real + 6 * T)) < 0.04
    assert all(r["accepted"] for r in records) and clock.relocks == 0


def test_a_small_error_at_the_first_downstroke_is_also_taken_in_full():
    clock, real = clock_started(0.25)
    T = 60 / 70
    ticks, records = run(clock, [beat(1 + k % 3, real + k * T) for k in range(6)], real + 6 * T)
    assert records[0]["reason"] == "start_relocked_on_the_first_downstroke"
    lines = [t for t, i in ticks if i % 3 == 0]
    assert abs(lines[0] - (real + 3 * T)) < 0.03          # before: 60 % of the error at most, 0.25 beat at most


def test_a_first_downstroke_far_from_the_predicted_start_is_not_taken_as_the_start():
    clock, plan = started_clock()
    T = 60 / 70
    late = beat(1, plan.start_s + 0.9 * T)
    record = clock.observe(late, late.stamp + 0.06)
    assert record["reason"] == "bar_position_disagrees_waiting_for_confirmation" and not record["phase_adjusted"]
    assert not clock.start_downstroke_due                  # only the first downstroke is asked this question


def test_the_start_rule_is_for_the_first_downstroke_only():
    clock, real = clock_started(0.3)
    T = 60 / 70
    first = clock.observe(beat(1, real), real + 0.06)
    assert first["reason"] == "start_relocked_on_the_first_downstroke"
    ticks, records = run(clock, [beat(2, real + T), beat(3, real + 2 * T), beat(1, real + 3.7 * T)], real + 5 * T)
    assert records[-1]["reason"] == "bar_position_disagrees_waiting_for_confirmation"


def test_when_the_first_downstroke_is_not_seen_the_later_ones_follow_the_ordinary_rules():
    clock, real = clock_started(0.3)
    T = 60 / 70
    ticks, records = run(clock, [beat(2, real + T), beat(3, real + 2 * T), beat(1, real + 3 * T)], real + 4 * T)
    assert all(r["reason"] != "start_relocked_on_the_first_downstroke" for r in records)
    assert not clock.start_downstroke_due


# ── what the window tells the conductor while the bars are counted ──
def window(count, cues_seen, state="armed"):
    """The fields of live-status.json that the prompt is made from, read off a real count-in."""
    return dict(state=state, meter=count.meter, tracking=dict(tracking_ready=True), count_in_cues=len(count.cues),
                count_in_stage=count.stage, count_in_reason=count.last_reason, visual_candidates=cues_seen)


def prompt(count, cues_seen, memory, now, state="armed"):
    from conductor_rt.dashboard import bar_not_counted, preparation_feedback
    memory = bar_not_counted(window(count, cues_seen, state), memory, now)
    return preparation_feedback(window(count, cues_seen, state), memory is not None), memory


def test_after_a_bar_that_did_not_count_the_window_says_keep_going_not_ready_again():
    # the old prompt fell back to "ready, you may listen first", which reads as "begin again"
    count = GestureCountIn(3, 70.0)
    count.restart(0.0, 0.0, "auto_prepare")
    T = 60 / 70
    (label, _), memory = prompt(count, 0, None, 0.5)
    assert "listen first" in label and memory is None                    # nothing conducted yet: the first text
    feed(count, [beat(1, 1.0)])
    (label, _), memory = prompt(count, 1, memory, 1.1)
    assert label == "Count-in beat 1 / 3"
    feed(count, [beat(3, 1.0 + 2 * T)])                            # no middle beat: this bar alone is not enough
    assert count.last_reason == "tempo_not_confirmed_next_bar_counts" and not count.cues
    (label, hint), memory = prompt(count, 2, memory, 1.1 + 2 * T)
    assert label == "Keep conducting" and "No need to prepare again" in hint
    feed(count, [beat(1, 1.0 + 3 * T)])                            # the next bar simply counts
    (label, _), memory = prompt(count, 3, memory, 1.1 + 3 * T)
    assert label == "Count-in beat 1 / 3"
    assert feed(count, [beat(3, 1.0 + 5 * T)]) == pytest.approx(70)
    (label, _), memory = prompt(count, 4, memory, 1.1 + 5 * T)
    assert label == "Count-in complete 3 / 3"


def test_keep_going_stays_while_cues_come_and_goes_when_the_hand_rests():
    from conductor_rt.dashboard import KEEP_GOING_S
    count = GestureCountIn(3, 70.0)
    count.restart(0.0, 0.0, "auto_prepare")
    T = 60 / 70
    feed(count, [beat(1, 1.0), beat(2, 1.0 + T)])
    _, memory = prompt(count, 2, None, 1.1 + T)
    assert count.tick(1.0 + T + 3.2, 0.0)["reason"] == "bar_not_finished"          # no lift: the bar was dropped
    (label, _), memory = prompt(count, 2, memory, 1.0 + T + 3.3)
    assert label == "Keep conducting"
    # the next downstroke is missed and only the lift is seen: the count-in is "waiting for a
    # downbeat" again, but the conductor is plainly conducting, so the prompt must not change
    feed(count, [beat(3, 7.0)])
    assert count.last_reason == "waiting_first_downbeat"
    (label, _), memory = prompt(count, 3, memory, 7.1)
    assert label == "Keep conducting"
    (label, _), memory = prompt(count, 3, memory, 7.1 + KEEP_GOING_S - 0.5)
    assert label == "Keep conducting"
    (label, _), memory = prompt(count, 3, memory, 7.1 + KEEP_GOING_S + 0.5)        # nothing seen since: the hand rests
    assert "listen first" in label and memory is None


def test_keep_going_is_not_shown_after_a_fresh_prepare_or_outside_the_count_in():
    count = GestureCountIn(3, 70.0)
    count.restart(0.0, 0.0, "auto_prepare")
    T = 60 / 70
    feed(count, [beat(1, 1.0), beat(3, 1.0 + 2 * T)])
    (label, _), memory = prompt(count, 2, None, 3.0)
    assert "listen first" in label                                        # this window never saw the bar being counted
    feed(count, [beat(1, 1.0 + 3 * T)])
    _, memory = prompt(count, 3, memory, 1.1 + 3 * T)
    assert memory is not None
    count.restart(1.2 + 3 * T, 0.0, "operator_prepare")            # the operator asked for a fresh count
    (label, _), memory = prompt(count, 3, memory, 1.3 + 3 * T)
    assert "listen first" in label and memory is None
    for state in ("running", "stopped", "ended", "idle"):
        assert prompt(count, 3, (1.0, 3), 1.5, state)[1] is None


def test_the_old_count_in_keeps_its_own_prompts():
    # the window is told which count-in it shows; without that nothing changes for the legacy chain
    from conductor_rt.dashboard import preparation_feedback
    count = GestureCountIn(3, 70.0)
    count.restart(0.0, 0.0, "auto_prepare")
    feed(count, [beat(1, 1.0), beat(3, 1.0 + 2 * 60 / 70)])
    assert "listen first" in preparation_feedback(window(count, 2))[0]


# ── what the operator is told when a session cannot start ──
def session(tmp_path, console="", status=None):
    import json
    (tmp_path / "conductor").mkdir(parents=True)
    (tmp_path / "sender-console.log").write_text(console, encoding="utf-8")
    if status is not None:
        (tmp_path / "conductor" / "startup-status.json").write_text(json.dumps(status), encoding="utf-8")
    return tmp_path


CAMERA_ERROR = "Traceback (most recent call last):\n  File \"runner.py\", line 277, in read_camera\nRuntimeError: Selected camera could not be opened\n"


def test_a_terminal_without_camera_permission_is_named_as_the_reason(tmp_path):
    # without this the window would stay on its first text and the reason would be in a log file only
    from conductor_rt.status_view import session_failure
    console = ("OpenCV: not authorized to capture video (status 0), requesting...\n"
               "OpenCV: can not spin main run loop from other thread, set OPENCV_AVFOUNDATION_SKIP_AUTH=1\n"
               "OpenCV: camera failed to properly initialize!\n")
    f = session_failure(session(tmp_path, console, dict(phase="failed", error=CAMERA_ERROR)), 1)
    assert f["kind"] == "camera_permission" and "camera permission" in f["title"] and "restart both terminals" in f["advice"]


def test_a_camera_that_does_not_open_for_another_reason(tmp_path):
    from conductor_rt.status_view import session_failure
    f = session_failure(session(tmp_path, "", dict(phase="failed", error=CAMERA_ERROR)), 1)
    assert f["kind"] == "camera" and f["title"] == "Start failed: the camera cannot open"


def test_a_missing_receiver_is_named_as_the_reason(tmp_path):
    # the receiver ends with a failed start; starting only the Conductor again finds no receiver
    from conductor_rt.status_view import session_failure
    console = "Traceback (most recent call last):\n  File \"runner.py\", line 510, in run\nRuntimeError: No consumer within --wait-client deadline\n"
    f = session_failure(session(tmp_path, console, dict(phase="model_ready")), 1)
    assert f["kind"] == "no_receiver" and "receiver" in f["title"] and "terminal 1" in f["advice"]


def test_any_other_failure_shows_its_last_error_line(tmp_path):
    from conductor_rt.status_view import session_failure
    console = "some output\nTraceback (most recent call last):\n  File \"x.py\", line 1\nOSError: [Errno 48] Address already in use\n"
    f = session_failure(session(tmp_path, console, dict(phase="model_ready")), 1)
    assert f["kind"] == "error" and f["title"] == "Start failed" and "Address already in use" in f["advice"]
    f = session_failure(session(tmp_path / "later", console, dict(phase="first_frame_received")), 1)
    assert f["title"].startswith("Error while running")


def test_a_clean_end_and_an_unknown_exit_are_not_failures(tmp_path):
    from conductor_rt.status_view import session_failure
    folder = session(tmp_path, "RuntimeError: No consumer within --wait-client deadline\n")
    assert session_failure(folder, 0) is None and session_failure(folder, None) is None
    assert session_failure(tmp_path / "nothing-here", 1)["kind"] == "error"        # no files at all: still an answer


# ── window texts where there is no New test button (the live TCP launcher) ──
def test_the_window_does_not_point_at_a_button_it_does_not_have():
    from conductor_rt.dashboard import formal_recording_feedback, preparation_feedback
    ended = dict(state="ended", meter=3, tracking=dict(tracking_ready=True))
    for rec, finished in ((dict(finalized=True, written=1587), True), (dict(error="disk full"), True), ({}, True)):
        assert "New test" in formal_recording_feedback(rec, True, finished)[1]                 # where the button exists
        text = formal_recording_feedback(rec, True, finished, False)[1]
        assert "New test" not in text and text.endswith(".")
    assert "1587" in formal_recording_feedback(dict(finalized=True, written=1587), True, True, False)[1]
    assert "New test" in preparation_feedback(ended)[1]
    assert preparation_feedback(ended, False, False) == ("Stopped", "Run ended.")


def test_the_camera_line_says_what_is_true():
    # before the first frame nothing has ended; after a complete piece the line says so
    from conductor_rt.dashboard import camera_off_text
    assert camera_off_text(dict(state="idle", camera_frames=0, stop_reason=None), False) == "CAMERA OFF — camera not opened yet"
    assert camera_off_text(dict(state="ended", camera_frames=1587, stop_reason=None), True) == "CAMERA OFF — run ended / piece complete"
    assert camera_off_text(dict(state="stopped", camera_frames=1027, stop_reason="operator_pipe_stop"), True) == "CAMERA OFF — run ended / operator_pipe_stop"
    assert camera_off_text(dict(state="stopped", stop_reason=None), True) == "CAMERA OFF — run ended / Stopped"


def test_the_metronome_line_shows_no_bar_number_of_its_own():
    # the metronome counts bars from the moment it was started, so its count is not the bar of the piece
    from conductor_rt.dashboard import metronome_running_note
    note = metronome_running_note(dict(state="running", bpm=70.0, bar=22, expected_beat=1, meter=3))
    assert note == "70.0 BPM · accent on beat 1" and "22" not in note and "bar " not in note


# ── a piece that ran to its end while only the recording failed ──
def test_a_failed_recording_after_a_complete_piece_is_not_called_an_error_while_running(tmp_path):
    # a complete piece whose only failure is the recording must not be reported as "Error while running"
    import json
    from conductor_rt.status_view import session_failure
    folder = session(tmp_path, "", dict(phase="first_frame_received"))
    metrics = dict(final_state="ended", errors=[], publisher_error=None,
                   recording=dict(enabled=True, finalized=True, written=1, error="RuntimeError: Camera dimensions changed during recording"))
    (folder / "conductor" / "metrics.json").write_text(json.dumps(metrics), encoding="utf-8")
    f = session_failure(folder, 1)
    assert f["kind"] == "recording" and f["title"] == "The piece finished normally, but the local recording failed"
    assert "were not affected" in f["advice"] and "Camera dimensions changed" in f["advice"]
    metrics["errors"] = ["Traceback ...\nRuntimeError: something else\n"]            # a real error is still an error
    (folder / "conductor" / "metrics.json").write_text(json.dumps(metrics), encoding="utf-8")
    assert session_failure(folder, 1)["kind"] == "error"


def offer_all(rec, frames):
    """Hand frames to the recorder one by one, as fast as its encoder takes them (its queue holds three)."""
    import time
    for i, image in enumerate(frames):
        deadline = time.time() + 2.0
        while not rec.queue.empty() and time.time() < deadline:
            time.sleep(0.002)
        rec.offer(image, i, i / 30, i / 30 + 0.001)


def test_a_size_change_in_the_first_second_begins_the_recording_again(tmp_path):
    # a camera may deliver its first frame at 640x360 and the next in another size; the recording must survive that
    pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")
    import json
    from conductor_rt.recording import CameraRecording
    frame = lambda w, h, v: np.full((h, w, 3), v, dtype=np.uint8)
    rec = CameraRecording(tmp_path)
    offer_all(rec, [frame(640, 360, 10)] + [frame(640, 480, 40 + i) for i in range(40)])
    assert rec.close()
    r = rec.final_report
    assert r["error"] is None and r["written"] == 40 and r["dropped"] == 0
    assert r["size_restarts"] == [dict(frames_discarded=1, from_size=[640, 360], to_size=[640, 480])]
    assert r["decode"]["container_frames"] == 40 and r["decode"]["first_frame"] and r["decode"]["last_frame"]
    rows = [json.loads(line) for line in open(tmp_path / "camera-frames.jsonl")]
    assert len(rows) == 40 and rows[0]["video_frame"] == 0 and rows[0]["capture_frame"] == 1 and rows[-1]["video_frame"] == 39


def test_a_size_change_later_in_a_recording_is_still_an_error(tmp_path):
    pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")
    from conductor_rt.recording import CameraRecording
    frame = lambda w, h, v: np.full((h, w, 3), v, dtype=np.uint8)
    rec = CameraRecording(tmp_path)
    offer_all(rec, [frame(640, 480, 40 + i) for i in range(45)] + [frame(640, 360, 10)])
    rec.close()
    r = rec.final_report
    assert r["error"] == "RuntimeError: Camera dimensions changed during recording" and r["written"] == 45 and r["size_restarts"] == []
