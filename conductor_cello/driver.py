"""Robot ports: the real ur_rtde binding and a time-accurate mock.

Both expose the same small surface, shaped after what ur_rtde 1.6.5 really
returns (checked against the installed library, not assumed):
    move_l(pose, speed, accel, asynchronous) -> bool
    async_progress() -> int          (<0 idle/finished, toggles between two values)
    stop_l(decel) -> None            (blocks until the tool has stopped)
    disconnect() -> None
ONE thread owns every control call. The real port never unlocks a protective
stop, never powers on and never reconnects. Tool settings are written in one
place only, apply_installation(), which the executor calls once at the start
of a session and checks by read-back. The pendant keeps its own installation;
a session sets TCP 0 / 0.1 kg itself.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass

from . import geometry as g

SAFETY_NORMAL = 1
ROBOT_MODE_RUNNING = 7
RUNTIME_PLAYING = 2


@dataclass(frozen=True)
class RobotState:
    t_local: float              # executor clock when the sample was read
    robot_time: float           # controller timestamp of the sample
    pose: tuple                 # flange pose (TCP offset must be zero)
    speed: tuple                # TCP speed, 6 values
    wrench: tuple               # TCP force/torque, 6 values
    q: tuple                    # joint positions, rad
    safety_mode: int
    robot_mode: int
    runtime_state: int
    speed_fraction: float       # pendant speed slider target, 1.0 = 100 %
    connected: bool
    speed_scaling: float = 1.0  # what the trajectory generator applies right now (ramps up after a program starts)


class Timebase:
    """Real time. Tests substitute FakeTimebase so nothing sleeps for real."""

    def now(self) -> float:
        return time.perf_counter()

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)


class FakeTimebase(Timebase):
    def __init__(self, start: float = 1000.0):
        self.t = float(start)
        self.hooks = []         # callables run on every advance (scripted sources, fault triggers)

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += max(0.0, seconds)
        for hook in list(self.hooks):
            hook(self.t)


# ══════════════════════════════════════════════════════════════════
# Real robot
# ══════════════════════════════════════════════════════════════════

class RealRtdePort:
    """ur_rtde binding. Receive-only until open_control() is called."""

    def __init__(self, ip: str, timebase: Timebase | None = None):
        self.ip = ip
        self.tb = timebase or Timebase()
        self.rtde_r = None
        self.rtde_c = None
        self._dash = None

    # ── receive only: no program is started on the controller ────
    def open_receive(self):
        import rtde_receive
        self.rtde_r = rtde_receive.RTDEReceiveInterface(self.ip)

    # ── control: uploads ur_rtde's own control script (no motion, no installation write) ──
    def open_control(self):
        import rtde_control
        self.rtde_c = rtde_control.RTDEControlInterface(self.ip)

    def read_state(self) -> RobotState:
        r = self.rtde_r
        return RobotState(
            t_local=self.tb.now(), robot_time=float(r.getTimestamp()),
            pose=tuple(r.getActualTCPPose()), speed=tuple(r.getActualTCPSpeed()),
            wrench=tuple(r.getActualTCPForce()), q=tuple(r.getActualQ()),
            safety_mode=int(r.getSafetyMode()), robot_mode=int(r.getRobotMode()),
            runtime_state=int(r.getRuntimeState()),
            speed_fraction=float(r.getTargetSpeedFraction()), connected=bool(r.isConnected()),
            speed_scaling=float(r.getSpeedScaling()))

    def payload(self) -> tuple:
        return float(self.rtde_r.getPayload()), tuple(self.rtde_r.getPayloadCog())

    def tcp_offset(self) -> tuple:
        return tuple(self.rtde_c.getTCPOffset())

    def program_running(self) -> bool:
        return bool(self.rtde_c.isProgramRunning())

    def move_l(self, pose, speed: float, accel: float, asynchronous: bool = True) -> bool:
        return self.rtde_c.moveL([float(x) for x in pose], float(speed), float(accel), bool(asynchronous)) is True

    def async_progress(self) -> int:
        return int(self.rtde_c.getAsyncOperationProgress())

    def stop_l(self, decel: float) -> None:
        self.rtde_c.stopL(float(decel))            # returns None in ur_rtde 1.6.5

    def zero_ft(self) -> bool:
        return self.rtde_c.zeroFtSensor() is True

    def pose_reachable(self, pose) -> bool:
        """Controller's own check: reachable and inside its safety limits. No motion."""
        return self.rtde_c.isPoseWithinSafetyLimits([float(x) for x in pose]) is True

    def inverse_kinematics(self, pose, qnear):
        """Joint solution nearest `qnear` from the controller's calibrated kinematics, or None."""
        pose, qnear = [float(x) for x in pose], [float(x) for x in qnear]
        if self.rtde_c.getInverseKinematicsHasSolution(pose, qnear) is not True:
            return None
        return tuple(self.rtde_c.getInverseKinematics(pose, qnear))

    def apply_installation(self, tcp, mass: float, cog) -> tuple:
        """Set the TCP offset and the payload. No motion."""
        ok_tcp = self.rtde_c.setTcp([float(x) for x in tcp]) is True
        ok_payload = self.rtde_c.setPayload(float(mass), [float(x) for x in cog]) is True
        return ok_tcp, ok_payload

    def program_stop(self) -> str:
        """Last-resort software stop on a second channel: stops the running program.

        Dashboard 'stop' only. It does not unlock, power on or release brakes.
        """
        import dashboard_client
        if self._dash is None:
            self._dash = dashboard_client.DashboardClient(self.ip)
        if not self._dash.isConnected():
            self._dash.connect()
        self._dash.stop()
        return "dashboard_stop_sent"

    def close(self) -> None:
        for obj, calls in ((self.rtde_c, ("stopScript", "disconnect")), (self.rtde_r, ("disconnect",)),
                           (self._dash, ("disconnect",))):
            if obj is None:
                continue
            for name in calls:
                try:
                    getattr(obj, name)()
                except Exception:
                    pass
        self.rtde_c = self.rtde_r = self._dash = None


def read_tcp_offset_readonly(ip: str, timeout_s: float = 3.0):
    """TCP offset from the primary read-only port 30011, without any control program.

    Parses the Cartesian sub-package (type 4) of a robot-state message: six
    doubles of TCP pose followed by six doubles of TCP offset. Sends nothing.
    """
    import socket
    import struct
    deadline = time.perf_counter() + timeout_s
    buf = b""
    with socket.create_connection((ip, 30011), timeout_s) as conn:
        conn.settimeout(0.5)
        while time.perf_counter() < deadline:
            try:
                data = conn.recv(65536)
            except socket.timeout:
                continue
            if not data:
                break
            buf += data
            while len(buf) >= 5:
                size = struct.unpack_from("!I", buf)[0]
                if not 5 <= size <= 1 << 20:
                    raise ValueError("invalid primary frame size")
                if len(buf) < size:
                    break
                frame, buf = buf[:size], buf[size:]
                if frame[4] != 16:
                    continue
                pos = 5
                while pos + 5 <= len(frame):
                    sub = struct.unpack_from("!I", frame, pos)[0]
                    if sub < 5 or pos + sub > len(frame):
                        break
                    if frame[pos + 4] == 4 and sub >= 101:
                        values = struct.unpack_from("!12d", frame, pos + 5)
                        return tuple(values[6:])
                    pos += sub
    return None


# ══════════════════════════════════════════════════════════════════
# Mock robot: trapezoid motion on the executor's time base
# ══════════════════════════════════════════════════════════════════

class MockPort:
    """Kinematic stand-in with the same call surface as RealRtdePort.

    Motion is the ideal zero-to-zero trapezoid of moveL. An on-string contact
    force appears when the flange is within 2 mm of the taught line. Faults are
    injected by name with a trigger time on the time base:
        move_rejected, protective_stop, telemetry_freeze, force_spike,
        slider_changed, program_dies, stop_ignored
    """

    CONTACT_FORCE_N = 3.6
    CONTACT_TORQUE_NM = 0.75

    def __init__(self, timebase: Timebase, start_pose=None, *, tcp_offset=g.VERIFIED_TCP,
                 payload_kg=g.VERIFIED_PAYLOAD_KG, payload_cog=g.VERIFIED_PAYLOAD_COG, q=None,
                 latency_s: float = 0.003, q_fn=None, wrench_offset=(0.0,) * 6, ripple_n: float = 0.0,
                 ripple_hz: float = 2.44):
        self.ripple_n, self.ripple_hz = float(ripple_n), float(ripple_hz)   # force ripple seen in free air on the robot
        self.q_fn = q_fn                # optional joints-from-flange-pose model (the mock has no kinematics)
        self.wrench_offset = tuple(wrench_offset)   # a sensor whose zero is off
        self.tb = timebase
        self.pose = tuple(start_pose if start_pose is not None else g.lifted(g.pose_at(0.5)))
        self._tcp_offset, self._payload, self._cog = tuple(tcp_offset), float(payload_kg), tuple(payload_cog)
        self.q = tuple(q) if q is not None else tuple(math.radians(x) for x in (-141.4, -40.7, 63.8, -54.7, -104.9, -157.0))
        self.latency_s = latency_s
        self.seg = None                 # (start, target, v, a, t0, T, L)
        self._progress = -1
        self.safety_mode = SAFETY_NORMAL
        self.robot_mode = ROBOT_MODE_RUNNING
        self.speed_fraction = 1.0
        self.program = True
        self.connected = True
        self.faults = {}                # name -> trigger time
        self.calls = []                 # audit trail of control calls
        self._frozen_at = None
        self._robot_t0 = timebase.now()
        self.control_open = False
        self.receive_open = False

    def open_receive(self):
        self.receive_open = True

    SCALING_RAMP_S = 0.3            # the real controller ramps speed scaling up after a program starts

    def open_control(self):
        self.control_open = True
        self._t_control = self.tb.now()
        self.calls.append(("open_control", self.tb.now()))

    def _active(self, name) -> bool:
        return name in self.faults and self.tb.now() >= self.faults[name]

    def _profile(self, t):
        """(distance, speed) along the segment at time t since start."""
        _, _, v, a, _, T, L = self.seg
        if L <= 0 or t >= T:
            return L, 0.0
        if v * v / a >= L:                       # triangular
            half = T / 2.0
            if t <= half:
                return 0.5 * a * t * t, a * t
            tail = T - t
            return L - 0.5 * a * tail * tail, a * tail
        ramp = v / a
        if t < ramp:
            return 0.5 * a * t * t, a * t
        if t <= T - ramp:
            return 0.5 * a * ramp * ramp + v * (t - ramp), v
        tail = T - t
        return L - 0.5 * a * tail * tail, a * tail

    def _advance(self):
        if self._active("protective_stop") and self.safety_mode == SAFETY_NORMAL:
            self.safety_mode = 3
            if self.seg is not None:
                self._update_pose()
                self.seg = None
                self._toggle()
        if self._active("program_dies") and self.program:
            self.program = False
            if self.seg is not None:
                self._update_pose()
                self.seg = None
        if self.seg is None:
            return (0.0,) * 6
        vel = self._update_pose()
        start, target, v, a, t0, T, L = self.seg
        if self.tb.now() - t0 >= T:
            self.pose = target
            self.seg = None
            self._toggle()
            return (0.0,) * 6
        return vel

    def _update_pose(self):
        start, target, v, a, t0, T, L = self.seg
        d, speed = self._profile(max(0.0, self.tb.now() - t0))
        k = d / L if L > 0 else 1.0
        self.pose = tuple(s + (e - s) * k for s, e in zip(start, target))
        direction = tuple((e - s) / L for s, e in zip(start[:3], target[:3])) if L > 0 else (0.0, 0.0, 0.0)
        return tuple(x * speed for x in direction) + (0.0, 0.0, 0.0)

    def _toggle(self):
        # Two consecutive finished operations never report the same value.
        self._last_done = -2 if getattr(self, "_last_done", -1) != -2 else -1
        self._progress = self._last_done

    def read_state(self) -> RobotState:
        vel = self._advance()
        now = self.tb.now()
        if self._active("telemetry_freeze"):
            if self._frozen_at is None:
                self._frozen_at = now
            robot_time = self._frozen_at - self._robot_t0
        else:
            robot_time = now - self._robot_t0
        _, lift, lateral = g.decompose(self.pose)
        on_string = lift < 0.002 and lateral < 0.005
        force = self.CONTACT_FORCE_N if on_string else 0.0
        torque = self.CONTACT_TORQUE_NM if on_string else 0.0
        if self._active("force_spike"):
            force += 20.0
        ripple = self.ripple_n * math.sin(2.0 * math.pi * self.ripple_hz * now) if self.ripple_n else 0.0
        wrench = tuple(a + b for a, b in zip((ripple, force, 0.0, torque, 0.0, 0.0), self.wrench_offset))
        fraction = 0.5 if self._active("slider_changed") else self.speed_fraction
        if self.q_fn is not None:
            self.q = tuple(self.q_fn(g.flange_from_tcp(self.pose, self._tcp_offset)))
        scaling = 0.0
        if self.control_open and self.program:
            scaling = min(1.0, (now - self._t_control) / self.SCALING_RAMP_S)
        return RobotState(now, robot_time, self.pose, vel, wrench, self.q, self.safety_mode, self.robot_mode,
                          RUNTIME_PLAYING if self.program else 1, fraction, self.connected, scaling)

    def payload(self):
        return self._payload, self._cog

    def tcp_offset(self):
        return self._tcp_offset

    def program_running(self) -> bool:
        self._advance()
        return self.program

    def move_l(self, pose, speed, accel, asynchronous=True) -> bool:
        self._advance()
        self.calls.append(("move_l", self.tb.now(), tuple(pose), speed, accel, asynchronous))
        if self._active("move_rejected") or not self.program or self.safety_mode != SAFETY_NORMAL or self.seg is not None:
            return False
        L = g.distance(self.pose, pose)
        if L <= 0:
            self._toggle()
            return True
        v = float(speed)
        T = 2.0 * math.sqrt(L / accel) if v * v / accel >= L else v / accel + L / v
        self.seg = (self.pose, tuple(pose), v, float(accel), self.tb.now() + self.latency_s, T, L)
        self._progress = 0
        if not asynchronous:
            self.tb.sleep(T + self.latency_s)
            self._advance()
        return True

    def async_progress(self) -> int:
        self._advance()
        return self._progress

    def stop_l(self, decel: float) -> None:
        self.calls.append(("stop_l", self.tb.now(), decel))
        if self._active("stop_ignored") or self.seg is None:
            self._advance()
            return None
        start, target, v, a, t0, T, L = self.seg
        d, speed = self._profile(max(0.0, self.tb.now() - t0))
        stop_time = speed / decel
        d_stop = min(L, d + speed * speed / (2.0 * decel))
        self.tb.sleep(stop_time)                     # blocks until the tool has stopped
        k = d_stop / L
        self.pose = tuple(s + (e - s) * k for s, e in zip(start, target))
        self.seg = None
        self._toggle()
        return None

    def zero_ft(self) -> bool:
        self.calls.append(("zero_ft", self.tb.now()))
        return True

    def pose_reachable(self, pose) -> bool:
        return not self._active("pose_unreachable")

    def inverse_kinematics(self, pose, qnear):
        if self._active("pose_unreachable"):
            return None
        if self._active("ik_flips"):
            return tuple(x + 2.0 for x in qnear)            # a far-away arm configuration
        return tuple(qnear)

    def apply_installation(self, tcp, mass, cog) -> tuple:
        self.calls.append(("apply_installation", self.tb.now(), tuple(tcp), mass, tuple(cog)))
        if self._active("install_ignored"):
            return True, True                           # fault injection: the write has no effect
        flange = g.flange_from_tcp(self.pose, self._tcp_offset)
        self._tcp_offset = tuple(tcp)
        self._payload = float(mass)
        if any(abs(x) > 0 for x in cog):            # ur_rtde keeps the current CoG when all three are zero
            self._cog = tuple(cog)
        self.pose = g.tcp_from_flange(flange, self._tcp_offset)     # the arm does not move, the reported pose does
        return True, True

    def program_stop(self) -> str:
        self.calls.append(("program_stop", self.tb.now()))
        if self.seg is not None:
            self._update_pose()
            self.seg = None
        self.program = False
        return "mock_program_stop"

    def close(self) -> None:
        self.calls.append(("close", self.tb.now()))
