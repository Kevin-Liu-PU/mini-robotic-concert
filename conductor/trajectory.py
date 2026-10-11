"""Display-only segmented point history. Never supplies observations to beats."""
from collections import deque
from dataclasses import dataclass
import math

from .continuity import displacement_limit


@dataclass(frozen=True)
class TrailPoint:
    t: float
    x: float
    y: float


class Trajectory:
    def __init__(self,retention_s=8.0,smoothing_tau_s=.055,max_gap_s=.25,max_jump=.30):
        self.retention_s=retention_s
        self.smoothing_tau_s=smoothing_tau_s
        self.max_gap_s=max_gap_s
        self.max_jump=max_jump
        self.segments=deque()
        self.last_input=None
        self.break_segment()

    def break_segment(self):
        """Stop this polyline and smoothing; retain historical segments to age out."""
        self.active=None
        self.raw=None
        self.smoothed=None
        self.sample_dts=deque(maxlen=7)

    def clear(self):
        """Explicit user/camera reset removes all display history."""
        self.segments.clear()
        self.break_segment()

    def prune(self,now):
        for segment in self.segments:
            while segment and now-segment[0].t>self.retention_s:
                segment.popleft()
        while self.segments and not self.segments[0]:
            self.segments.popleft()

    def update(self,t,x,y):
        if not math.isfinite(t) or (self.last_input is not None and t<=self.last_input):
            return None
        self.last_input=t
        self.prune(t)
        if x is None or y is None or not all(math.isfinite(v) and 0<=v<=1 for v in (x,y)):
            self.break_segment()
            return None
        if self.raw:
            dt=t-self.raw.t
            limit=displacement_limit(self.max_jump,dt,self.sample_dts)
            if dt>self.max_gap_s or math.hypot(x-self.raw.x,y-self.raw.y)>limit:
                self.break_segment()
            else:
                self.sample_dts.append(dt)
        if self.active is None:
            self.active=deque()
            self.segments.append(self.active)
            point=TrailPoint(t,x,y)
        else:
            alpha=-math.expm1(-(t-self.smoothed.t)/self.smoothing_tau_s)
            point=TrailPoint(t,self.smoothed.x+alpha*(x-self.smoothed.x),
                             self.smoothed.y+alpha*(y-self.smoothed.y))
        self.active.append(point)
        self.raw=TrailPoint(t,x,y)
        self.smoothed=point
        return point

    def edges(self,now,age_s):
        """Yield only observed-segment edges whose endpoints are still visible."""
        for segment in self.segments:
            previous=None
            for point in segment:
                if not 0<=now-point.t<=age_s:
                    continue
                if previous is not None:
                    yield previous,point
                previous=point

    def current(self,now):
        return self.smoothed if self.smoothed and 0<=now-self.smoothed.t<=self.max_gap_s else None


def trail_opacity(now,point_time,age_s):
    return max(0.0,min(1.0,1-(now-point_time)/age_s))
