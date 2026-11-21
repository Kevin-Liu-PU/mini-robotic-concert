"""Beats from the conducting wrist alone. No I/O, no model, no clock.

What the recordings of this project's conductor show (three and four beats,
metronome and hand-labelled takes): every beat but the last is the lowest
point of a stroke, and the last beat of the bar is the lift, with no lowest
point of its own. The first lowest point after a lift is beat 1. So the
gesture itself says which beat it is, bar after bar, and nothing has to be
counted across bars.

Input is the pose wrist in body coordinates (origin between the shoulders, x
towards the image right, y down, unit = shoulder width). Finger and face
landmarks are not used: they drop out on fast strokes, the pose wrist does not.

Three times are kept apart for every beat: `stamp` is when the hand was at the
lowest point (or half way up the lift), `decision_stamp` is the frame at which
that became certain, and delivery to the clock happens later still.
"""
from collections import deque
from dataclasses import dataclass
import math

from .clock import BeatObservation


@dataclass(frozen=True)
class GestureBeat(BeatObservation):
    number: int | None = None      # 1..meter within the bar; None when the gesture does not say
    x: float = 0.0                 # where the hand was, body coordinates
    y: float = 0.0
    drop: float = 0.0              # descent into a lowest point, shoulder widths
    rise: float = 0.0              # rise seen when a lift was confirmed
    note: str = ""


@dataclass
class Settings:
    smooth_s: float = 0.030        # smoothing of the wrist track
    reversal: float = 0.06         # vertical reversal that confirms a turning point, shoulder widths ...
    reversal_frac: float = 0.05    # ... or this fraction of the pattern height, whichever is larger
    still: float = 0.06            # the hand has arrived when it stays inside a box of this size ...
    dwell_s: float = 0.10          # ... for this long, without any rebound
    min_gap_beats: float = 0.55    # a middle beat cannot follow the previous beat sooner than this
    start_range: float = 1.2       # assumed height of the pattern until one full stroke has been seen
    min_range: float = 0.7
    big_drop: float = 0.35         # fraction of the pattern height: a downstroke that can be beat 1
    beat_drop: float = 0.08        # fraction of the pattern height: a lowest point that can be a middle beat
    beat_drop_min: float = 0.12    # ... and never less than this, shoulder widths
    strong_drop: float = 0.22      # fraction of the height: a clear bounce; weaker ones must be on time to count
    lift_mark: float = 0.50        # the lift is stamped where the hand passes this fraction of the height
    lift_confirm: float = 0.72     # ... and reported once it has passed this fraction
    top_zone: float = 0.30         # a turning point within this fraction of the top counts as "at the top"
    shoulder_line: float = 0.15    # y at or above this is "up" when no pattern has been learned yet
    first_drop: float = 0.60       # fraction of the height a first downstroke must cover to be taken as beat 1
    lift_min_beats: float = 0.80   # a lift cannot follow beat 1 sooner than this (a rebound can)
    gap_s: float = 0.30            # longer tracking gap: forget the stroke in progress
    level_gain: float = 0.35       # how fast the remembered top and bottom levels follow new strokes


def body_wrist(pose, side, aspect):
    """Pose landmarks [[x, y, visibility], ...] -> (x, y, ok) of the conducting wrist in body coordinates."""
    if not pose or len(pose) < 17:
        return 0.0, 0.0, False
    a, b, w = pose[11], pose[12], pose[15 + side]
    if min(a[2], b[2]) < 0.5 or w[2] < 0.5:
        return 0.0, 0.0, False
    ax, ay, bx, by = a[0] * aspect, a[1], b[0] * aspect, b[1]
    if ax > bx:
        ax, ay, bx, by = bx, by, ax, ay
    dx, dy = bx - ax, by - ay
    span = math.hypot(dx, dy)
    if not 0.03 < span < 1.2 or abs(dy) > 0.6 * span:
        return 0.0, 0.0, False
    cx, cy = (ax + bx) / 2, (ay + by) / 2
    ux, uy = dx / span, dy / span
    qx, qy = w[0] * aspect - cx, w[1] - cy
    x, y = (qx * ux + qy * uy) / span, (-qx * uy + qy * ux) / span
    if not (abs(x) < 3.5 and abs(y) < 3.5):
        return 0.0, 0.0, False
    return x, y, True


class GestureBeats:
    """Feed one wrist sample per frame with `step`; it returns the beats that became certain."""

    def __init__(self, meter=3, bpm_hint=70.0, settings=None):
        if meter not in (3, 4):
            raise ValueError("meter must be 3 or 4")
        self.meter = meter
        self.s = settings or Settings()
        self.period = 60.0 / bpm_hint          # only used to tell a rebound from a lift
        self.top_level = None                   # remembered height of the top of the pattern (y, small = high)
        self.low_level = None                   # remembered height of the lowest points
        self.sequence = None                    # number of the last beat reported in this bar, None = unknown
        self.last_down = None                   # stamp of the last beat 1
        self.last_beat_t = None                 # stamp of the last beat reported
        self.bars_seen = 0                      # bars whose length has been measured (beat 1 to beat 1)
        self.trace = deque(maxlen=4000)         # every turning point and decision, for diagnosis
        self._clear_stroke()
        self.last_t = None
        self.sx = self.sy = None

    # ── state ──
    def _clear_stroke(self):
        self.seek = None                # "bottom" or "top": what the next turning point will be
        self.ext = None                 # running extreme (t, x, y, frame, source_s)
        self.top = None                 # last confirmed top
        self.bottom = None              # last confirmed lowest point
        self.lift_mark = None           # sample at which the current rise passed the lift mark
        self.recent = []                # last samples, for the arrival (dwell) test
        self.last_beat_t = None if not hasattr(self, "last_beat_t") else self.last_beat_t
        self.lift_reported = False
        self.came_from_top = False      # the hand has been at the top since the last lowest point

    @property
    def height(self):
        if self.top_level is None or self.low_level is None:
            return self.s.start_range
        return max(self.s.min_range, self.low_level - self.top_level)

    def set_period(self, period_s):
        if period_s and 60.0 / 180 <= period_s <= 60.0 / 30:
            self.period = period_s

    def _learn(self, name, value):
        old = getattr(self, name)
        setattr(self, name, value if old is None else old + self.s.level_gain * (value - old))

    # ── one frame ──
    def step(self, stamp, x, y, ok, received=None, frame=-1, source_s=0.0):
        s = self.s
        out = []
        received = stamp if received is None else received
        if not ok or not (math.isfinite(x) and math.isfinite(y)):
            if self.last_t is not None and stamp - self.last_t > s.gap_s:
                self._lost()
            return out
        if self.last_t is not None and stamp - self.last_t > s.gap_s:
            self._lost()
        dt = 0.033 if self.last_t is None else max(1e-3, stamp - self.last_t)
        self.last_t = stamp
        if self.sx is None:
            self.sx, self.sy = x, y
        else:
            a = 1.0 - math.exp(-dt / s.smooth_s)
            self.sx += a * (x - self.sx)
            self.sy += a * (y - self.sy)
        x, y = self.sx, self.sy
        cur = (stamp, x, y, frame, source_s)
        if self.ext is None:
            self.ext, self.top, self.seek = cur, cur, None
            return out
        if self.seek is None:
            # direction not known yet: wait for the first clear move
            if y - self.ext[2] >= s.reversal:
                self.top, self.seek, self.ext = self.ext, "bottom", cur
            elif self.ext[2] - y >= s.reversal:
                self.bottom, self.seek, self.ext = self.ext, "top", cur
            elif y < self.ext[2]:
                pass
            return out
        height = self.height
        reversal = max(s.reversal, s.reversal_frac * height)
        self.recent.append(cur)
        while self.recent and stamp - self.recent[0][0] > s.dwell_s + 0.05:
            self.recent.pop(0)
        if self.seek == "bottom":
            if y >= self.ext[2]:
                self.ext = cur
            if self.ext[2] - y >= reversal:
                out.extend(self._lowest_point(self.ext, cur, received, height, "rebound"))
                self.bottom, self.seek, self.ext = self.ext, "top", cur
                self.lift_mark, self.lift_reported = None, False
            else:
                # arrival without a rebound: the hand came down and now stays where it is
                window = [p for p in self.recent if stamp - p[0] <= s.dwell_s + 1e-9]
                if (len(window) >= 3 and stamp - window[0][0] >= s.dwell_s - 0.02
                        and max(p[1] for p in window) - min(p[1] for p in window) <= s.still
                        and max(p[2] for p in window) - min(p[2] for p in window) <= s.still):
                    if window[0][0] <= self.ext[0] < stamp:
                        arrived = self.ext              # already past the lowest point: that is the instant
                    else:
                        # still settling: the first sample that is already where the hand now rests. A hand
                        # that stopped dead is stamped where it stopped, one creeping down is stamped later.
                        arrived = next((p for p in window if abs(p[2] - y) <= 0.5 * s.still and abs(p[1] - x) <= 0.5 * s.still), cur)
                    top_y = self.top[2] if self.top is not None else arrived[2]
                    if arrived[2] - top_y < max(s.beat_drop_min, s.beat_drop * height):
                        found = []                      # no stroke led here: the hand is just resting
                    else:
                        found = self._lowest_point(arrived, cur, received, height, "arrival")
                    if found:
                        out.extend(found)
                        # a further descent from here is a new stroke, measured from this level
                        self.bottom, self.top, self.ext = arrived, arrived, cur
                        self.lift_mark, self.lift_reported = None, False
        else:
            if y <= self.ext[2]:
                self.ext = cur
            elif y - self.ext[2] >= reversal:
                self.top = self.ext
                if self.top_level is not None and self.top[2] <= self.top_level + s.top_zone * height:
                    self.came_from_top = True
                elif self.top_level is None and self.bottom is not None and self.bottom[2] - self.top[2] >= s.lift_confirm * height:
                    self.came_from_top = True
                self.seek, self.ext = "bottom", cur
            if self.seek == "top" and self.bottom is not None and not self.lift_reported:
                rise = self.bottom[2] - y
                if self.lift_mark is None and rise >= s.lift_mark * height:
                    self.lift_mark = cur
                if rise >= s.lift_confirm * height:
                    out.extend(self._lift(cur, received, rise, height))
        return out

    def _lost(self):
        self.trace.append(dict(what="tracking_gap", t=self.last_t))
        self._clear_stroke()
        self.sx = self.sy = None
        self.ext = None
        self.sequence = None

    # ── decisions ──
    def _lowest_point(self, low, cur, received, height, how="rebound"):
        """A lowest point (or an arrival) was confirmed at `cur`. Decide whether it is a beat and which."""
        s = self.s
        top_y = self.top[2] if self.top is not None else low[2]
        drop = low[2] - top_y
        from_top = self.came_from_top or (self.top_level is not None and top_y <= self.top_level + s.top_zone * height)
        if not from_top and self.sequence is None and top_y <= s.shoulder_line and drop >= s.first_drop * height:
            # Nothing seen yet (or the hand was lost for a while): a long stroke that begins at
            # shoulder height or above is the downstroke of a bar, the hand was simply already up.
            from_top = True
        # A new bar needs the lift of the old one. Without a lift, a high rebound of beat 1 followed
        # by a deep beat 2 looks like another downstroke; only accept that once a bar's time has passed.
        new_bar = (self.sequence is None or self.sequence == self.meter or self.last_down is None
                   or low[0] - self.last_down >= (self.meter - 0.8) * self.period)
        number, note = None, ""
        if from_top and drop >= s.big_drop * height and new_bar:
            number, note = 1, "downstroke from the top"
            if self.last_down is not None:
                bar = low[0] - self.last_down
                if 0.6 * self.meter * self.period <= bar <= 1.6 * self.meter * self.period:
                    self.period += 0.5 * (bar / self.meter - self.period)
                    self.bars_seen += 1
            self.sequence = 1
            self.last_down = low[0]
            self._learn("top_level", top_y)
            self._learn("low_level", low[2])
        elif drop < max(s.beat_drop_min, s.beat_drop * height):
            self.trace.append(dict(what="small_low", t=low[0], frame=low[3], drop=drop))
            return []
        elif self.sequence is None:
            note = "lowest point, place in the bar not known yet"
        elif self.sequence >= self.meter - 1 or self.last_beat_t is None:
            # all middle beats of this bar are already there (or the lift came): not a beat
            self.trace.append(dict(what="extra_low", t=low[0], frame=low[3], drop=drop, sequence=self.sequence))
            if how == "rebound":
                self.came_from_top = False
            return []
        else:
            gap = (low[0] - self.last_beat_t) / self.period          # beats since the previous beat
            strong = drop >= s.strong_drop * height
            early, late = (s.min_gap_beats, 1.6) if strong else (0.75, 1.4)
            if gap < early:
                self.trace.append(dict(what="early_low", t=low[0], frame=low[3], drop=drop, gap=gap))
                if how == "rebound":
                    self.came_from_top = False
                return []
            if gap <= late:
                self.sequence += 1
                number, note = self.sequence, "lowest point one beat after beat %d" % (self.sequence - 1)
            elif self.meter == 4 and self.sequence == 1 and gap <= 2.6 and self.bars_seen >= 1:
                self.sequence = 3
                number, note = 3, "lowest point two beats after beat 1 (beat 2 was not seen)"
            else:
                self.trace.append(dict(what="late_low", t=low[0], frame=low[3], drop=drop, gap=gap))
                if how == "rebound":
                    self.came_from_top = False
                return []
            if strong:
                self._learn("low_level", low[2])
        self.came_from_top = False
        self.last_beat_t = low[0]
        beat = GestureBeat(low[0], received, 0.9 if number else 0.7, "low_point", low[3], low[4],
                           decision_stamp=cur[0], decision_frame=cur[3], number=number,
                           x=low[1], y=low[2], drop=drop, note=note + (", hand arrived and stayed" if how == "arrival" else ""))
        self.trace.append(dict(what="beat", kind="low_point", how=how, t=low[0], decided=cur[0], frame=low[3], number=number,
                               drop=drop, x=low[1], y=low[2], note=note))
        return [beat]

    def _lift(self, cur, received, rise, height):
        s = self.s
        mark = self.lift_mark or cur
        if self.last_down is not None and self.sequence == 1 and mark[0] - self.last_down < s.lift_min_beats * self.period:
            # the rebound of beat 1 can be this high; a lift cannot come this soon
            self.trace.append(dict(what="rebound_not_lift", t=mark[0], frame=mark[3], rise=rise))
            return []
        self.lift_reported = True
        self.came_from_top = True
        self.last_beat_t = mark[0]
        known = self.sequence is not None
        self.sequence = self.meter
        beat = GestureBeat(mark[0], received, 0.9, "lift", mark[3], mark[4], decision_stamp=cur[0], decision_frame=cur[3],
                           number=self.meter, x=mark[1], y=mark[2], rise=rise,
                           note="lift" if known else "lift, first one seen")
        self.trace.append(dict(what="beat", kind="lift", t=mark[0], decided=cur[0], frame=mark[3], number=self.meter,
                               rise=rise, x=mark[1], y=mark[2]))
        return [beat]
