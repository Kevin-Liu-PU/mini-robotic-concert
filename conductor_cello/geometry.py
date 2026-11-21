"""Taught geometry of the bow line on the open A string.

Every pose here is a FLANGE pose. The numbers are only meaningful while the
controller's active TCP offset is zero (the installation every taught point
was verified under). This module never talks to the robot.

Pure standard library on purpose: the planner and its tests must import
without numpy, audio or driver packages.
"""
from __future__ import annotations

import math

# ── taught points (base frame, metres and axis-angle radians) ─────────
FROG = (.328964663774, .703229462539, -.009258082371,
        1.501488908519, 2.294264795278, -1.521353569816)
TIP = (.557923990118, .279876247922, .200314083410,
       -1.446439182207, -2.240158754246, 1.575901583680)
FSAFE = (.328964663671, .703229462391, .045333550563,
         1.501488908520, 2.294264795279, -1.521353569816)
TSAFE = (.557923989897, .279876247854, .255567513319,
         -1.446439179803, -2.240158755503, 1.575901584151)

# ── envelopes of the taught bow line ─────────────────────────────────
U_MIN = 0.04
U_MAX = 0.96
MAX_OUTWARD_DEPTH = 0.0015      # m, lighter than the taught contact
MAX_INWARD_DEPTH = 0.0020       # m, heavier than the taught contact
MOVE_ACCEL = 1.2                # m/s^2, pendant-matched


def rest_torque_reference(u: float) -> float:
    """Reference torque change (Nm) with the bow resting on the string at bow fraction u, from 57
    logged takes: medians 0.23 Nm near u 0.12, 0.42 near 0.30, 0.69 near 0.50, at depths within
    1 mm of the taught line. A straight line through the outer two. Torque is the steadier gauge
    of how hard the bow presses: about 0.05 Nm per mm of depth, and it barely drifts, unlike the
    force reading."""
    return 0.23 + (0.69 - 0.23) / (0.50 - 0.12) * (float(u) - 0.12)
RESET_SPEED = 0.25              # m/s, pendant-matched lift / set-down speed
SPEED_MIN_RECORDED = 0.09       # m/s, slowest stroke recorded on this instrument
SPEED_MAX_RECORDED = 0.25       # m/s, fastest stroke recorded on this instrument

# ── installation every taught point was verified under ───────────────
VERIFIED_TCP = (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
VERIFIED_PAYLOAD_KG = 0.1
VERIFIED_PAYLOAD_COG = (0.0, 0.0, 0.0)
# What earlier runs actually had active. The code asked for CoG zero, but
# ur_rtde's setPayload(mass, [0, 0, 0]) keeps the CURRENT centre of gravity
# (its control script calls set_payload(mass, get_target_payload_cog()) when
# all three are zero), and the controller constructor had just set this one.
AS_RUN_PAYLOAD_COG = (0.128, -0.006, 0.019)

# ── joint envelope of the working configuration, degrees ─────────────
# Union of (a) the joint ranges logged while playing and
# (b) the same arm configuration along the whole taught line, frog to tip,
# from on-string to 80 mm lift (inverse kinematics, nominal UR5e geometry).
JOINT_ENVELOPE_DEG = (
    (-161.0, -124.7), (-53.5, -9.4), (24.7, 73.7),
    (-60.5, -41.6), (-120.7, -91.6), (-168.7, -147.5),
)


def _sub(a, b):
    return tuple(x - y for x, y in zip(a, b))


def _add(a, b):
    return tuple(x + y for x, y in zip(a, b))


def _scale(a, k):
    return tuple(x * k for x in a)


def _dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def _norm(a):
    return math.sqrt(_dot(a, a))


BOW_AXIS_VEC = _sub(TIP[:3], FROG[:3])
BOW_LENGTH = _norm(BOW_AXIS_VEC)                         # ~0.5249 m
BOW_AXIS = _scale(BOW_AXIS_VEC, 1.0 / BOW_LENGTH)

_lift_vec = _sub(FSAFE[:3], FROG[:3])
PRESS_DIR = _scale(_lift_vec, 1.0 / _norm(_lift_vec))    # frog -> fsafe: the lift direction (+z here)
LIFT_M = min(_norm(_sub(FSAFE[:3], FROG[:3])), _norm(_sub(TSAFE[:3], TIP[:3])))   # ~0.0546 m


def canonicalise_rotvec(r, reference):
    """Of the two equivalent axis-angle vectors for r, the one nearer `reference`.

    r and r*(|r|-2pi)/|r| are the same rotation. Near |r| = pi they are far
    apart numerically, and the taught frog and tip rotations are exactly that
    case, so tip is pulled into frog's hemisphere before any interpolation.
    """
    theta = _norm(r)
    if theta < 1e-12:
        return tuple(r)
    alt = _scale(r, (theta - 2.0 * math.pi) / theta)
    return alt if _norm(_sub(alt, reference)) < _norm(_sub(r, reference)) else tuple(r)


_TIP_ROT_CANON = canonicalise_rotvec(TIP[3:], FROG[3:])


def pose_at(u: float) -> tuple:
    """Flange pose at bow fraction u (0 = frog, 1 = tip) on the taught line.

    Same construction as play_midi_pieces.pose_at: straight lerp in position,
    lerp between the canonicalised taught rotations (4.28 deg apart), and the
    taught pose verbatim at the two ends.
    """
    u = min(1.0, max(0.0, float(u)))
    if u <= 1e-9:
        return FROG
    if u >= 1.0 - 1e-9:
        return TIP
    pos = _add(FROG[:3], _scale(BOW_AXIS_VEC, u))
    rot = _add(FROG[3:], _scale(_sub(_TIP_ROT_CANON, FROG[3:]), u))
    return pos + rot


def apply_depth(pose, depth: float) -> tuple:
    """Signed depth from the taught contact: >0 presses in, <0 backs out."""
    depth = min(MAX_INWARD_DEPTH, max(-MAX_OUTWARD_DEPTH, float(depth)))
    return _sub(pose[:3], _scale(PRESS_DIR, depth)) + tuple(pose[3:])


def lifted(pose, lift: float = LIFT_M) -> tuple:
    """Pose raised clear of the string along the taught lift direction."""
    return _add(pose[:3], _scale(PRESS_DIR, float(lift))) + tuple(pose[3:])


def bow_pose(u: float, *, depth: float = 0.0, lift: float = 0.0) -> tuple:
    """pose_at(u) with the depth offset and an optional lift applied."""
    pose = apply_depth(pose_at(u), depth)
    return lifted(pose, lift) if lift else pose


def rotvec_to_matrix(r):
    theta = _norm(r)
    if theta < 1e-12:
        return ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
    kx, ky, kz = _scale(r, 1.0 / theta)
    c, s = math.cos(theta), math.sin(theta)
    v = 1.0 - c
    return ((c + kx * kx * v, kx * ky * v - kz * s, kx * kz * v + ky * s),
            (ky * kx * v + kz * s, c + ky * ky * v, ky * kz * v - kx * s),
            (kz * kx * v - ky * s, kz * ky * v + kx * s, c + kz * kz * v))


def matrix_to_rotvec(m) -> tuple:
    trace = m[0][0] + m[1][1] + m[2][2]
    theta = math.acos(min(1.0, max(-1.0, (trace - 1.0) / 2.0)))
    if theta < 1e-9:
        return (0.0, 0.0, 0.0)
    if abs(theta - math.pi) < 1e-6:
        diag = [(m[i][i] + 1.0) / 2.0 for i in range(3)]
        i = max(range(3), key=lambda k: diag[k])
        axis = [(m[j][i] + (1.0 if j == i else 0.0)) / 2.0 / math.sqrt(diag[i]) for j in range(3)]
        return tuple(a * theta for a in axis)
    k = theta / (2.0 * math.sin(theta))
    return ((m[2][1] - m[1][2]) * k, (m[0][2] - m[2][0]) * k, (m[1][0] - m[0][1]) * k)


def flange_from_tcp(tcp_pose, tcp_offset) -> tuple:
    """Flange pose behind a reported TCP pose: T_flange = T_tcp * inverse(T_offset).

    The controller reports the pose of the ACTIVE TCP. The taught points are
    flange poses, so with a non-zero TCP offset the reported pose has to be
    converted before it is compared with them.
    """
    rt = rotvec_to_matrix(tcp_pose[3:])
    ro = rotvec_to_matrix(tcp_offset[3:])
    rf = tuple(tuple(sum(rt[i][k] * ro[j][k] for k in range(3)) for j in range(3)) for i in range(3))   # rt * ro^T
    shift = tuple(sum(rf[i][k] * tcp_offset[k] for k in range(3)) for i in range(3))
    position = _sub(tuple(tcp_pose[:3]), shift)
    rotation = tuple(tcp_pose[3:]) if _norm(tcp_offset[3:]) < 1e-12 else matrix_to_rotvec(rf)
    return position + rotation


def tcp_from_flange(flange_pose, tcp_offset) -> tuple:
    """Pose the controller reports for a flange pose when `tcp_offset` is active."""
    rf = rotvec_to_matrix(flange_pose[3:])
    shift = tuple(sum(rf[i][k] * tcp_offset[k] for k in range(3)) for i in range(3))
    position = _add(tuple(flange_pose[:3]), shift)
    if _norm(tcp_offset[3:]) < 1e-12:
        return position + tuple(flange_pose[3:])
    ro = rotvec_to_matrix(tcp_offset[3:])
    rt = tuple(tuple(sum(rf[i][k] * ro[k][j] for k in range(3)) for j in range(3)) for i in range(3))
    return position + matrix_to_rotvec(rt)


def rotation_angle(r1, r2) -> float:
    """Angle in radians between two axis-angle orientations."""
    a, b = rotvec_to_matrix(r1), rotvec_to_matrix(r2)
    trace = sum(a[i][j] * b[i][j] for i in range(3) for j in range(3))
    return math.acos(min(1.0, max(-1.0, (trace - 1.0) / 2.0)))


def decompose(position) -> tuple:
    """Split a flange position into (u, lift, lateral) relative to the bow line.

    position = FROG + u*BOW_AXIS_VEC + lift*PRESS_DIR + lateral, with `lateral`
    perpendicular to the plane spanned by the bow axis and the lift direction.
    lift > 0 is above the string line, lift < 0 is pressed past the taught
    contact. `lateral` is returned as a distance in metres.
    """
    d = _sub(tuple(position[:3]), FROG[:3])
    aa = _dot(BOW_AXIS, BOW_AXIS)
    ap = _dot(BOW_AXIS, PRESS_DIR)
    pp = _dot(PRESS_DIR, PRESS_DIR)
    da = _dot(d, BOW_AXIS)
    dp = _dot(d, PRESS_DIR)
    det = aa * pp - ap * ap
    s = (da * pp - dp * ap) / det            # metres along the bow axis
    h = (dp * aa - da * ap) / det            # metres along the lift direction
    residual = _sub(d, _add(_scale(BOW_AXIS, s), _scale(PRESS_DIR, h)))
    return s / BOW_LENGTH, h, _norm(residual)


def distance(p, q) -> float:
    return _norm(_sub(tuple(p[:3]), tuple(q[:3])))


def segment_deviation(position, start, end) -> float:
    """Distance from `position` to the straight segment start -> end (metres)."""
    p = tuple(position[:3])
    a, b = tuple(start[:3]), tuple(end[:3])
    ab = _sub(b, a)
    L2 = _dot(ab, ab)
    if L2 < 1e-18:
        return _norm(_sub(p, a))
    t = min(1.0, max(0.0, _dot(_sub(p, a), ab) / L2))
    return _norm(_sub(p, _add(a, _scale(ab, t))))


def joints_in_envelope(q_rad, margin_deg: float = 15.0) -> list:
    """Indices of joints outside the historical working envelope (+- margin)."""
    bad = []
    for i, (value, (lo, hi)) in enumerate(zip(q_rad, JOINT_ENVELOPE_DEG)):
        deg = math.degrees(value)
        if not (lo - margin_deg <= deg <= hi + margin_deg):
            bad.append(i)
    return bad
