"""Bring the arm back to the clear line above the A string from a nearby pose.

For when someone left the arm outside the start region (another string, a
parked pose). The path is fixed in shape and always goes UP first:

    1. nudge  10 mm straight up, very slowly: the first motion proves that the
              command, the completion signal and the telemetry behave
    2. lift   straight up to the travel height, orientation unchanged
    3. cross  at the travel height to above the target point on the taught
              line, turning into the taught orientation on the way
    4. down   straight down to the clear line (54.6 mm above the string)

Straight up is the direction the taught safe points lift the bow off the
string (frog -> fsafe is +z), so steps 1 and 2 move the bow away from the
instrument, and the sideways move happens well above string level.

This is geometry only. Whether the space is actually free is for the person
at the robot to judge before confirming, and to watch during the move.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from . import geometry as g

# Where a homing move may START from (flange, base frame, metres). A box around
# the cello workspace; outside it nobody has checked anything.
START_BOX = ((0.15, 0.75), (0.00, 0.90), (-0.05, 0.60))
MAX_START_ROTATION_DEG = 45.0       # from the taught orientation
JOINT_MARGIN_DEG = 60.0             # around the working joint envelope: same arm configuration
TRAVEL_Z_RANGE = (0.25, 0.45)
TARGET_U_RANGE = (0.10, 0.90)
NUDGE_M = 0.010
NUDGE_SPEED = 0.02
MAX_SEGMENT_JOINT_CHANGE_DEG = 70.0


@dataclass(frozen=True)
class Step:
    name: str
    target: tuple       # flange pose
    speed: float
    what: str           # plain description for the operator


def check_start(flange_pose, q_rad) -> list:
    """Reasons a homing move may not start from here (empty list = fine)."""
    reasons = []
    for axis, value, (lo, hi) in zip("xyz", flange_pose[:3], START_BOX):
        if not lo <= value <= hi:
            reasons.append(f"flange {axis} = {value:.3f} m is outside the homing box {lo}..{hi} m")
    u = g.decompose(flange_pose)[0]
    rot = math.degrees(g.rotation_angle(flange_pose[3:], g.pose_at(min(1.0, max(0.0, u)))[3:]))
    if rot > MAX_START_ROTATION_DEG:
        reasons.append(f"tool is turned {rot:.0f} deg away from the taught orientation (limit {MAX_START_ROTATION_DEG:.0f})")
    bad = g.joints_in_envelope(q_rad, JOINT_MARGIN_DEG)
    if bad:
        reasons.append(f"joints {bad} are more than {JOINT_MARGIN_DEG:.0f} deg outside the working configuration")
    return reasons


FORCE_LIMIT_N = 6.0                 # change from the zeroed reading that stops a homing move


def plan_home(flange_pose, *, u_target: float = 0.5, travel_z: float = 0.40, speed: float = 0.05) -> list:
    """The steps from `flange_pose` to the clear line above bow fraction `u_target`."""
    if not TARGET_U_RANGE[0] <= u_target <= TARGET_U_RANGE[1]:
        raise ValueError(f"target u must be inside {TARGET_U_RANGE}")
    if not TRAVEL_Z_RANGE[0] <= travel_z <= TRAVEL_Z_RANGE[1]:
        raise ValueError(f"travel height must be inside {TRAVEL_Z_RANGE} m")
    if not 0.01 <= speed <= 0.10:
        raise ValueError("homing speed must be inside 0.01..0.10 m/s")
    x, y, z = flange_pose[:3]
    rot = tuple(flange_pose[3:])
    goal = g.bow_pose(u_target, lift=g.LIFT_M)
    if travel_z < goal[2] + 0.05:
        raise ValueError("travel height must be at least 50 mm above the clear line at the target")
    steps = []
    top = max(z, travel_z)
    if z < travel_z - 0.001:
        z_from = z
        if travel_z - z > 2 * NUDGE_M:
            z_from = z + NUDGE_M
            steps.append(Step("nudge", (x, y, z_from) + rot, NUDGE_SPEED,
                              f"10 mm straight up at {NUDGE_SPEED * 1000:.0f} mm/s (first motion, a test)"))
        steps.append(Step("lift", (x, y, travel_z) + rot, speed,
                          f"straight up {((travel_z - z_from) * 1000):.0f} mm to flange height {travel_z:.2f} m, orientation unchanged"))
    across = math.hypot(goal[0] - x, goal[1] - y)
    turn = math.degrees(g.rotation_angle(rot, goal[3:]))
    steps.append(Step("cross", (goal[0], goal[1], top) + tuple(goal[3:]), speed,
                      f"sideways {across * 1000:.0f} mm at that height to above u = {u_target:.2f}, turning {turn:.0f} deg into the taught orientation"))
    steps.append(Step("down", goal, speed,
                      f"straight down {((top - goal[2]) * 1000):.0f} mm to the clear line, {g.LIFT_M * 1000:.0f} mm above the string"))
    return steps


def describe(steps, start_pose) -> list:
    """Plain lines for the operator, with distance and time per step."""
    lines = []
    here = start_pose
    total = 0.0
    for i, step in enumerate(steps, 1):
        dist = g.distance(here, step.target)
        seconds = dist / step.speed + step.speed / g.MOVE_ACCEL
        total += seconds
        lines.append(f"  {i}. {step.what}   [{dist * 1000:.0f} mm, about {seconds:.0f} s]")
        here = step.target
    lines.append(f"  total about {total:.0f} s; it ends at flange {[round(v, 3) for v in steps[-1].target[:3]]}, "
                 f"inside the start region")
    return lines
