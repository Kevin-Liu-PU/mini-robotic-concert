"""Measured-time continuity guard for occasional dropped processing samples."""
from statistics import median
import math


def displacement_limit(base_limit, dt, recent_dts):
    # Preserve the existing guard at normal cadence. Up to one extra frame's
    # elapsed time permits correspondingly more motion, never more than 2x.
    # This validates observed positions; it never creates predicted positions.
    if dt is None or len(recent_dts)<3:
        return base_limit
    typical=median(recent_dts)
    return base_limit*min(2.0,max(1.0,dt/typical)) if typical>0 else base_limit


def supported_tip_limit(base_limit, t, x, y, wrist, history):
    """Bounded allowance from a continuous wrist and three measured tip steps.

    No predicted point is emitted. Half the last measured speed times the
    current dt is allowed in addition to the base guard, capped at 50%.
    This is a continuity heuristic, not proof of physical hand identity.
    """
    if wrist is None or len(history)<4:return base_limit
    last=history[-1];dt=t-last[0]
    if not 0<dt<=.12 or math.dist(wrist,last[3])>.03:return base_limit
    current=(x-last[1],y-last[2]);length=math.hypot(*current)
    if length==0:return base_limit
    for a,b in zip(list(history)[-4:-1],list(history)[-3:]):
        step=(b[1]-a[1],b[2]-a[2]);size=math.hypot(*step)
        previous_dt=b[0]-a[0]
        if not 0<previous_dt<=.12 or size<.005:return base_limit
        if math.dist(a[3],b[3])>max(.03,.75*previous_dt):return base_limit
        if (current[0]*step[0]+current[1]*step[1])/(length*size)<.85:return base_limit
    prior=history[-2]
    speed=math.hypot(last[1]-prior[1],last[2]-prior[2])/(last[0]-prior[0])
    return min(base_limit*1.5,base_limit+.5*speed*dt)
