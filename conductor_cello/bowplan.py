"""Per-bow planning: one moveL, constant cruise speed, decided at the bow change.

The controller plans a zero-to-zero trapezoid for moveL(pose, v, a). Measured on
this robot (500 full-bow strokes, a = 1.2, v = 0.09..0.25) the
blocking call lasts  T = v/a + L/v  plus 3.2 ms median (max 7.1 ms), so that
formula is used here as the motion time.

Policy (stage 1):
  * Tempo decides how long the bow lasts; the bow speed stays inside the range
    that was actually recorded on this instrument (0.09..0.25 m/s) and close to
    a preferred value, so loudness does not swing with tempo.
  * The stroke length follows from speed and duration, capped so slow tempi do
    not ask for more bow than exists; then the speed drops instead.
  * Each bow aims at a centred end point, so alternating strokes of unequal
    length do not walk the playing region along the stick.
  * A stroke is never planned past the remaining travel in its direction. If
    even the slowest allowed speed cannot fill the duration with the bow that
    is left, the bow runs out early and waits on the string (flagged).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from . import geometry as g


@dataclass(frozen=True)
class BowLimits:
    accel: float = g.MOVE_ACCEL
    v_min: float = g.SPEED_MIN_RECORDED
    v_max: float = g.SPEED_MAX_RECORDED
    v_pref: float = 0.14            # preferred cruise speed, m/s
    slack: float = 0.20             # allowed bow-to-bow deviation from the nominal speed
    u_lo: float = 0.06              # executor travel bounds, inside taught U_MIN..U_MAX
    u_hi: float = 0.94
    u_center: float = 0.50
    l_cap: float = 0.40             # longest nominal stroke, m
    l_min: float = 0.02             # shortest stroke worth commanding, m
    t_min: float = 0.60             # shortest bow duration accepted, s

    def validate(self) -> "BowLimits":
        ok = (0 < self.accel <= g.MOVE_ACCEL + 1e-12
              and g.SPEED_MIN_RECORDED - 1e-12 <= self.v_min <= self.v_pref <= self.v_max <= g.SPEED_MAX_RECORDED + 1e-12
              and 0 <= self.slack < 1
              and g.U_MIN <= self.u_lo < self.u_center < self.u_hi <= g.U_MAX
              and 0 < self.l_min < self.l_cap <= (self.u_hi - self.u_lo) * g.BOW_LENGTH
              and self.t_min > 2 * self.v_min / self.accel)
        if not ok:
            raise ValueError("bow limits outside the taught / recorded envelope")
        return self


@dataclass(frozen=True)
class BowPlan:
    direction: int                  # +1 down-bow (frog -> tip, u rising), -1 up-bow
    u_start: float
    u_end: float
    length: float                   # m
    speed: float                    # cruise speed, m/s
    accel: float
    duration: float                 # requested time to the next bow change, s
    move_time: float                # trapezoid time of the moveL, s
    wait_after: float               # rest on the string after the move, s
    flags: tuple = field(default_factory=tuple)

    @property
    def rest_only(self) -> bool:
        return self.length <= 0.0


def trapezoid_time(length: float, speed: float, accel: float) -> float:
    """Zero-to-zero moveL time. Triangular when the cruise speed is never reached."""
    if length <= 0:
        return 0.0
    if speed * speed / accel >= length:
        return 2.0 * math.sqrt(length / accel)
    return speed / accel + length / speed


def length_for(speed: float, duration: float, accel: float) -> float:
    """Stroke length that lasts `duration` at cruise speed `speed`."""
    if duration < 2.0 * speed / accel:
        raise ValueError("duration too short to reach this cruise speed")
    return speed * (duration - speed / accel)


def speed_for(length: float, duration: float, accel: float):
    """Cruise speed that covers `length` in `duration`, or None if impossible."""
    disc = duration * duration - 4.0 * length / accel
    if disc < 0:
        return None
    return 2.0 * length / (duration + math.sqrt(disc))


def nominal(duration: float, lim: BowLimits) -> tuple:
    """(speed, length) a steady performance uses for bows of this duration."""
    span = (lim.u_hi - lim.u_lo) * g.BOW_LENGTH
    v = min(lim.v_pref, lim.accel * duration / 2.0)
    length = length_for(v, duration, lim.accel)
    if length > lim.l_cap:
        length = lim.l_cap
        v = speed_for(length, duration, lim.accel)
    if v < lim.v_min:
        # Slow tempo: hold the slowest recorded speed and spend more bow,
        # up to everything between the travel bounds.
        v = lim.v_min
        length = min(length_for(v, duration, lim.accel), span)
    return v, length


def first_start_u(duration: float, lim: BowLimits, direction: int = 1) -> float:
    """Where the first bow starts so a steady performance is centred on the stick."""
    _, length = nominal(duration, lim)
    return lim.u_center - direction * length / (2.0 * g.BOW_LENGTH)


def plan_bow(u: float, direction: int, duration: float, lim: BowLimits) -> BowPlan:
    """Plan the bow that starts now at fraction `u` and must end `duration` s from now."""
    if direction not in (1, -1):
        raise ValueError("direction must be +1 (down) or -1 (up)")
    if not (math.isfinite(u) and math.isfinite(duration)) or duration <= 0:
        raise ValueError("non-finite or non-positive planning input")
    a = lim.accel
    flags = []
    avail = ((lim.u_hi - u) if direction > 0 else (u - lim.u_lo)) * g.BOW_LENGTH
    if avail < lim.l_min or duration < lim.t_min:
        why = "no_travel_left" if avail < lim.l_min else "duration_below_minimum"
        return BowPlan(direction, u, u, 0.0, 0.0, a, duration, 0.0, duration, (why,))

    v_nom, l_nom = nominal(duration, lim)
    u_target = lim.u_center + direction * l_nom / (2.0 * g.BOW_LENGTH)
    length = direction * (u_target - u) * g.BOW_LENGTH       # aim at the centred end point
    length = min(max(length, lim.l_min), avail)
    v_lo = max(lim.v_min, v_nom * (1.0 - lim.slack))
    v_hi = min(lim.v_max, v_nom * (1.0 + lim.slack), a * duration / 2.0)
    v = speed_for(length, duration, a)

    if v is None or v > v_hi:
        v = v_hi
        length = length_for(v, duration, a)
        flags.append("speed_capped")
    elif v < v_lo:
        longer = length_for(v_lo, duration, a)
        if longer <= avail:
            v, length = v_lo, longer
            flags.append("speed_floored")
        else:
            length = avail
            fit = speed_for(avail, duration, a)
            if fit is not None and fit >= lim.v_min:
                v = fit
                flags.append("fit_remaining_travel")
            else:
                v = lim.v_min
                flags.append("bow_exhausted")
    if length > avail:                                        # numerical guard, never plan past the bound
        length = avail
    move_time = trapezoid_time(length, v, a)
    wait_after = max(0.0, duration - move_time)
    if wait_after < 1e-9:
        wait_after = 0.0
    u_end = u + direction * length / g.BOW_LENGTH
    return BowPlan(direction, u, u_end, length, v, a, duration, move_time, wait_after, tuple(flags))


@dataclass(frozen=True)
class ScoreGrid:
    """Rhythm baseline: equal bows of `beats_per_bow`, `total_beats` in all."""
    name: str
    beats_per_bow: float
    total_beats: float

    @property
    def planned_bows(self) -> int:
        return int(round(self.total_beats / self.beats_per_bow))


SCORES = {
    "twinkle": ScoreGrid("twinkle", 4.0, 48.0),     # 12 four-beat bows
    "arirang": ScoreGrid("arirang", 3.0, 48.0),     # 16 three-beat bows
}


def next_boundary(phase: float, grid: ScoreGrid, min_fraction: float = 0.5):
    """Score beat at which the bow starting at `phase` should end, or None at the end.

    The bow always ends on a bar line of the Conductor's grid. If less than
    `min_fraction` of a bow is left before the next line, the bow runs through
    to the following one, so a late start or a phase correction merges into a
    longer bow instead of producing a stub.
    """
    b = grid.beats_per_bow
    if phase >= grid.total_beats - min_fraction * b:
        return None
    k = math.floor(phase / b + 1e-9) + 1
    if k * b - phase < min_fraction * b:
        k += 1
    return min(k * b, grid.total_beats)
