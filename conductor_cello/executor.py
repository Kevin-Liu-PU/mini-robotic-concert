"""Bow executor: guarded moveL primitives and the Conductor-driven session.

Two kinds of trouble are kept strictly apart.

  Rhythm matters (never a fault): missed beats, low-confidence clock, tempo
  or beat-position corrections, a bow that cannot be filled, the Conductor
  stopping or going silent. The run adapts or ends in the normal way:
  stop the bow, confirm standstill, lift clear.

  Safety faults: safety mode not NORMAL, control program gone, telemetry
  stale, force or torque over the limit, flange off the commanded line, speed
  slider changed, a move rejected or ending in the wrong place. The executor
  stops the tool, confirms standstill and then commands NOTHING further. It
  does not lift, retry, unlock, power on or reconnect. A person decides.

Every move is asynchronous and watched every `poll_s`, so a stop request is
acted on within one poll plus the controller's stopL deceleration.
"""
from __future__ import annotations

import json
import math
import threading
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

from . import geometry as g
from .bowplan import BowLimits, ScoreGrid, first_start_u, next_boundary, plan_bow, trapezoid_time
from .clock import ConductorClock
from .driver import ROBOT_MODE_RUNNING, RUNTIME_PLAYING, SAFETY_NORMAL, Timebase


class Refusal(Exception):
    """Preflight said no. Nothing was commanded."""

    def __init__(self, reasons):
        self.reasons = list(reasons)
        super().__init__("; ".join(self.reasons))


class SafetyFault(Exception):
    def __init__(self, kind: str, detail: str = "", controllable: bool = True):
        self.kind, self.detail, self.controllable = kind, detail, controllable
        self.stop_report = None
        super().__init__(f"{kind}: {detail}" if detail else kind)


def move_time_limit(expected_s: float, turn_rad: float, turn_rate_floor: float) -> float:
    """Seconds after which a move that has not reported completion is a fault. `expected_s` is the
    trapezoid time of the translation alone. A move that also turns the tool takes as long as the
    turn needs, so the limit allows for both."""
    return max(expected_s, turn_rad / turn_rate_floor) * 1.25 + 1.0


@dataclass
class ExecConfig:
    mode: str = "air"                   # "air": bow on the lifted line, no contact. "contact": on the string.
    depth: float = 0.0                  # m from the taught contact (contact mode)
    air_lift: float = g.LIFT_M          # m above the taught line for lifts and air bows
    approach_speed: float = 0.10        # m/s for lift / traverse moves
    setdown_speed: float = 0.05         # m/s onto the string
    approach_accel: float = g.MOVE_ACCEL
    stop_decel: float = 2.0             # m/s^2 for stopL
    poll_s: float = 0.002
    state_log_s: float = 0.01
    limits: BowLimits = field(default_factory=BowLimits)
    min_fraction: float = 0.25          # shortest share of a bar worth its own bow
    finish_within_s: float = 0.35       # a STOP with state "ended", or a pause, this close to the bow change lets the
                                        # bow finish; a STOP with state "stopped" is never deferred
    clock_timeout_s: float = 1.0        # silence from the Conductor that ends the run
    pause_hold_s: float = 10.0          # how long to hold the bow still when the Conductor pauses (running -> ready)
    ready_timeout_s: float = 600.0      # how long to wait for the Conductor's first packet, and then armed for the first
                                        # running packet. As long as the Conductor's own session limit (its start files
                                        # give ten minutes): the receiver must not give up while the Conductor is still
                                        # counting in. Ctrl-C, a STOP, a closed connection or 1 s of silence end the wait at once.
    # safety limits
    force_limit_air: float = 7.0        # N change from baseline: unexpected contact in the air. With nothing touching,
                                        # 210 mm bows at 0.12 m/s read up to 2.0 N of change on the robot:
                                        # about 1.1 N that grows with the distance from the baseline point, a 2.4 Hz
                                        # ripple and the bow change. A 400 mm session bow is expected near 3.2 N
    force_limit_contact: float = 10.0   # N change from baseline (earlier runs: p99.9 7.3 N)
    torque_limit: float = 1.8           # Nm change from baseline (earlier runs: max 1.22 Nm)
    contact_band: tuple = (0.7, 6.5)    # N accepted at set-down. Measured on the robot (0.5 s means):
                                        # 0.51 N at u 0.12, 1.80 N at u 0.30, 2.51 N at u 0.50 on the taught line, and
                                        # 1.23 N at u 0.12 pressed 2 mm deeper; with nothing touching the same reading
                                        # stays under about 0.4 N. The frog end is the light end of the taught line
    line_tol: float = 0.005             # m off the commanded straight segment
    end_pos_tol: float = 0.002          # m from the commanded end pose
    end_rot_tol: float = 0.02           # rad
    telemetry_timeout_s: float = 0.25
    slider_tol: float = 0.005
    control_settle_s: float = 0.6       # shortest wait between starting the control program and the first move
    control_settle_timeout_s: float = 3.0   # go on after this long even if speed scaling never reads 1.0 (logged)
    wrench_debounce: int = 3            # consecutive samples over a wrench limit before it is a fault
    wrench_window_s: float = 0.5        # averaging time for the baseline and for the contact reading. In free air the
                                        # force carries a 2.4 Hz ripple of about 0.5 N: one period is 0.41 s
    pre_move_speed_max: float = 0.03    # m/s still allowed when the next move is commanded (bow changes chain)
    end_settle_s: float = 0.15          # time allowed for the measured pose to settle on the commanded end
    standstill_speed: float = 0.002     # m/s
    standstill_rot_speed: float = 0.02  # rad/s
    standstill_samples: int = 5
    standstill_span_s: float = 0.05
    stop_confirm_timeout_s: float = 1.5
    turn_rate_floor: float = 0.12       # rad/s. A moveL that also turns the tool takes as long as the turn needs,
                                        # whatever its length: 30 deg took 2.6 s and 3 deg 0.28 s on the robot
                                        # (0.17 to 0.20 rad/s). The time limit of a move allows for this rate.
    # start region: where a programmed move may begin (taught line, lifted or on the string)
    corridor_lift: tuple = (-0.004, 0.080)
    corridor_lateral: float = 0.020
    corridor_u: tuple = (-0.05, 1.05)
    corridor_rot_deg: float = 10.0
    joint_margin_deg: float = 15.0
    use_zero_ft: bool = False           # tare with zeroFtSensor at the lifted start pose instead of a software baseline

    def validate(self) -> "ExecConfig":
        self.limits.validate()
        if self.mode not in ("air", "contact"):
            raise ValueError("mode must be 'air' or 'contact'")
        if not -g.MAX_OUTWARD_DEPTH - 1e-9 <= self.depth <= g.MAX_INWARD_DEPTH + 1e-9:
            raise ValueError(f"depth must stay inside the taught envelope: "
                             f"{-g.MAX_OUTWARD_DEPTH * 1000:.1f} to +{g.MAX_INWARD_DEPTH * 1000:.1f} mm")
        if not 0.02 <= self.air_lift <= 0.08:
            raise ValueError("air_lift outside 20..80 mm")
        if not (0 < self.approach_speed <= g.RESET_SPEED and 0 < self.setdown_speed <= g.RESET_SPEED):
            raise ValueError("approach / set-down speed above the pendant-matched 0.25 m/s")
        if not 0.5 <= self.stop_decel <= 5.0:
            raise ValueError("stop deceleration outside 0.5..5 m/s^2")
        lo, hi = self.contact_band
        if not (0.5 <= lo < hi <= self.force_limit_contact - 1.0):
            # under 0.5 N a reading cannot be told from free air (two 0.5 s means differ by up to ~0.3 N)
            raise ValueError("contact band must satisfy 0.5 N <= low < high <= contact force limit - 1 N")
        return self


class RunLog:
    """events.jsonl and state.jsonl in a run folder; kept in memory too (tests read it)."""

    def __init__(self, folder=None, timebase: Timebase | None = None, echo=None):
        self.tb = timebase or Timebase()
        self.events = []
        self._lock = threading.Lock()   # the wire thread logs too
        self.folder = Path(folder) if folder else None
        self.echo = echo
        self._ev = self._st = None
        if self.folder:
            self.folder.mkdir(parents=True, exist_ok=False)
            self._ev = open(self.folder / "events.jsonl", "w", buffering=1)
            self._st = open(self.folder / "state.jsonl", "w")

    def event(self, kind: str, **data):
        row = dict(kind=kind, t=self.tb.now(), wall=time.time(), **data)
        with self._lock:
            self.events.append(row)
            if self._ev:
                self._ev.write(json.dumps(row, default=_jsonable) + "\n")
            if self.echo:
                self.echo(row)
        return row

    def state(self, st, extra=None):
        if not self._st:
            return
        row = dict(t=st.t_local, rt=st.robot_time, pose=st.pose, speed=st.speed, wrench=st.wrench, q=st.q,
                   safety=st.safety_mode, mode=st.robot_mode, runtime=st.runtime_state, slider=st.speed_fraction,
                   scaling=st.speed_scaling)
        if extra:
            row.update(extra)
        self._st.write(json.dumps(row) + "\n")

    def of(self, kind: str) -> list:
        return [e for e in self.events if e["kind"] == kind]

    def close(self):
        for f in (self._ev, self._st):
            if f:
                f.close()
        self._ev = self._st = None


def _jsonable(x):
    if hasattr(x, "__dataclass_fields__"):
        return asdict(x)
    if isinstance(x, (set, tuple)):
        return list(x)
    return str(x)


def _vnorm(v) -> float:
    return math.sqrt(sum(x * x for x in v))


class Executor:
    """Owns the robot port. Single-threaded: call it from one thread only."""

    def __init__(self, port, cfg: ExecConfig, *, timebase: Timebase | None = None, log: RunLog | None = None):
        self.port = port
        self.cfg = cfg.validate()
        self.tb = timebase or Timebase()
        self.log = log or RunLog(timebase=self.tb)
        self.feeder = None              # callable(now): in-process clock source, stepped every tick
        self.notify = None              # callable(dict): frames back to the Conductor
        self.operator_stop = None       # callable() -> bool: local request for a normal stop
        self.operator_abort = None      # callable() -> bool: stop and hold, command nothing further
        self._wrench_over = 0
        self.baseline = (0.0,) * 6      # wrench baseline
        self._baseline_set = False      # no force limit is ever judged against an uncaptured baseline
        self.peak_force = 0.0           # largest wrench change since it was last reset (N, Nm)
        self.peak_torque = 0.0
        self._last_rt = None
        self._last_rt_change = None
        self._last_state_log = -1e9
        self.control_open = False
        self.faulted = None

    # ── telemetry and checks ─────────────────────────────────────
    def _read(self):
        st = self.port.read_state()
        now = st.t_local
        if self._last_rt is None or st.robot_time != self._last_rt:
            self._last_rt, self._last_rt_change = st.robot_time, now
        if now - self._last_state_log >= self.cfg.state_log_s:
            self._last_state_log = now
            self.log.state(st)
        return st

    def _tick(self, ctx=None):
        """One monitoring step: feed the clock source, read state, run the safety checks."""
        if self.feeder is not None:
            self.feeder(self.tb.now())
        st = self._read()
        self._check(st, ctx or {})
        return st

    def _check(self, st, ctx):
        c = self.cfg
        if self.operator_abort is not None and self.operator_abort():
            raise SafetyFault("operator_abort", "stop and hold requested at the keyboard")
        if not st.connected:
            raise SafetyFault("rtde_disconnected", controllable=False)
        if st.t_local - self._last_rt_change > c.telemetry_timeout_s:
            raise SafetyFault("telemetry_stale", f"robot time frozen for {st.t_local - self._last_rt_change:.2f} s", controllable=False)
        if st.safety_mode != SAFETY_NORMAL:
            raise SafetyFault("safety_mode", f"safety mode {st.safety_mode}", controllable=False)
        if st.robot_mode != ROBOT_MODE_RUNNING:
            raise SafetyFault("robot_mode", f"robot mode {st.robot_mode}", controllable=False)
        if self.control_open and st.runtime_state != RUNTIME_PLAYING:
            raise SafetyFault("control_program_stopped", f"runtime state {st.runtime_state}", controllable=False)
        if abs(st.speed_fraction - 1.0) > c.slider_tol:
            raise SafetyFault("speed_slider_changed", f"slider {st.speed_fraction:.3f}")
        d = [a - b for a, b in zip(st.wrench, self.baseline)]
        force, torque = _vnorm(d[:3]), _vnorm(d[3:])
        if self._baseline_set:
            self.peak_force, self.peak_torque = max(self.peak_force, force), max(self.peak_torque, torque)
        f_lim = ctx.get("force_limit", c.force_limit_contact)
        t_lim = ctx.get("torque_limit", c.torque_limit)
        if force > f_lim or torque > t_lim:
            self._wrench_over += 1          # a single noisy sample is not a fault
            if self._wrench_over >= c.wrench_debounce:
                if force > f_lim:
                    raise SafetyFault("force_limit", f"{force:.2f} N > {f_lim:.2f} N")
                raise SafetyFault("torque_limit", f"{torque:.2f} Nm > {t_lim:.2f} Nm")
        else:
            self._wrench_over = 0
        seg = ctx.get("segment")
        if seg is not None:
            dev = g.segment_deviation(st.pose, seg[0], seg[1])
            if dev > c.line_tol:
                raise SafetyFault("off_line", f"{dev * 1000:.1f} mm from the commanded segment")

    def _stationary(self, st) -> bool:
        return (_vnorm(st.speed[:3]) <= self.cfg.standstill_speed
                and _vnorm(st.speed[3:]) <= self.cfg.standstill_rot_speed)

    def confirm_standstill(self, timeout_s=None) -> bool:
        """True once the tool has been at rest for N fresh samples over the span."""
        c = self.cfg
        deadline = self.tb.now() + (c.stop_confirm_timeout_s if timeout_s is None else timeout_s)
        count, since, last_rt = 0, None, None
        while self.tb.now() < deadline:
            st = self._read()
            fresh = st.robot_time != last_rt
            last_rt = st.robot_time
            if self._stationary(st) and st.connected:
                if fresh:
                    count += 1
                    since = st.t_local if since is None else since
                if count >= c.standstill_samples and st.t_local - since >= c.standstill_span_s:
                    return True
            else:
                count, since = 0, None
            self.tb.sleep(c.poll_s)
        return False

    def stop_now(self, reason: str) -> dict:
        """stopL, then prove standstill. Falls back to a dashboard program stop."""
        t_req = self.tb.now()
        try:
            pose_req = self._read().pose
        except Exception:
            pose_req = None
        report = dict(reason=reason, t_request=t_req, stop_call="not_sent", dashboard_stop=None)
        try:
            self.port.stop_l(self.cfg.stop_decel)
            report["stop_call"] = "returned"
        except Exception as exc:
            report["stop_call"] = "raised:" + repr(exc)
        report["stop_call_s"] = self.tb.now() - t_req
        ok = False
        try:
            ok = self.confirm_standstill()
        except Exception as exc:
            report["standstill_error"] = repr(exc)
        if not ok:
            try:
                report["dashboard_stop"] = self.port.program_stop()
                self.control_open = False
            except Exception as exc:
                report["dashboard_stop"] = "raised:" + repr(exc)
            try:
                ok = self.confirm_standstill()
            except Exception as exc:
                report["standstill_error"] = repr(exc)
        report["standstill_confirmed"] = bool(ok)
        report["standstill_s"] = self.tb.now() - t_req
        try:
            pose_end = self._read().pose
            report["stop_distance_m"] = g.distance(pose_req, pose_end) if pose_req else None
            report["u_after_stop"] = g.decompose(pose_end)[0]
        except Exception:
            pass
        self.log.event("stop", **report)
        return report

    # ── preflight ────────────────────────────────────────────────
    def describe(self, st=None, tcp_offset=None) -> dict:
        """Where the arm is relative to the taught line. `tcp_offset` (when known and
        non-zero) converts the reported TCP pose into the flange pose first."""
        st = st or self._read()
        if tcp_offset is None and self.control_open:
            tcp_offset = self.port.tcp_offset()
        flange = st.pose
        if tcp_offset is not None and any(abs(x) > 1e-9 for x in tcp_offset):
            flange = g.flange_from_tcp(st.pose, tcp_offset)
        u, lift, lateral = g.decompose(flange)
        rot = math.degrees(g.rotation_angle(flange[3:], g.pose_at(min(1.0, max(0.0, u)))[3:]))
        mass, cog = self.port.payload()
        d = [a - b for a, b in zip(st.wrench, self.baseline)]
        return dict(pose=st.pose, flange_pose=flange, tcp_offset_known=tcp_offset is not None,
                    q_deg=[round(math.degrees(x), 2) for x in st.q], u=u, lift_m=lift,
                    lateral_m=lateral, rot_from_taught_deg=rot, safety_mode=st.safety_mode,
                    robot_mode=st.robot_mode, runtime_state=st.runtime_state, speed_fraction=st.speed_fraction,
                    payload_kg=mass, payload_cog=cog, force_n=_vnorm(d[:3]), torque_nm=_vnorm(d[3:]),
                    joints_outside_envelope=g.joints_in_envelope(st.q, self.cfg.joint_margin_deg),
                    stationary=self._stationary(st))

    def preflight(self, *, need_control: bool = True) -> dict:
        """Read-only checks. Raises Refusal listing every reason; commands nothing."""
        c = self.cfg
        reasons = []
        st0 = self._read()
        self.tb.sleep(0.05)
        st = self._read()
        if st.robot_time == st0.robot_time:
            reasons.append("telemetry is not advancing")
        info = self.describe(st)
        if not st.connected:
            reasons.append("RTDE receive is not connected")
        if st.safety_mode != SAFETY_NORMAL:
            reasons.append(f"safety mode is {st.safety_mode}, need 1 (NORMAL)")
        if st.robot_mode != ROBOT_MODE_RUNNING:
            reasons.append(f"robot mode is {st.robot_mode}, need 7 (RUNNING)")
        if abs(st.speed_fraction - 1.0) > c.slider_tol:
            reasons.append(f"speed slider is {st.speed_fraction:.3f}, need 1.000 (timing depends on it)")
        cog = tuple(info["payload_cog"])
        if not self._installation_ok(g.VERIFIED_TCP, info["payload_kg"], cog):
            reasons.append(f"payload {info['payload_kg']} kg CoG {list(cog)} is not the reference payload "
                           f"(0.1 kg, CoG zero or {list(g.AS_RUN_PAYLOAD_COG)})")
        if not info["stationary"]:
            reasons.append("robot is moving")
        if need_control:
            if not self.control_open:
                reasons.append("control interface is not open")
            else:
                offset = self.port.tcp_offset()
                info["tcp_offset"] = offset
                if any(abs(x) > 1e-9 for x in offset):
                    reasons.append(f"TCP offset {offset} is not zero: every taught point would be a different place")
                if not self.port.program_running():
                    reasons.append("control program is not running")
        lo, hi = c.corridor_lift
        if not (c.corridor_u[0] <= info["u"] <= c.corridor_u[1] and lo <= info["lift_m"] <= hi
                and info["lateral_m"] <= c.corridor_lateral and info["rot_from_taught_deg"] <= c.corridor_rot_deg):
            reasons.append(
                f"flange is outside the start region (u {info['u']:.3f}, lift {info['lift_m'] * 1000:.0f} mm, "
                f"lateral {info['lateral_m'] * 1000:.0f} mm, rotation {info['rot_from_taught_deg']:.1f} deg): "
                f"move it by hand to above the string line first")
        if info["joints_outside_envelope"]:
            reasons.append(f"joints {info['joints_outside_envelope']} are outside the historical working configuration")
        self.log.event("preflight", ok=not reasons, reasons=reasons, **info)
        if reasons:
            raise Refusal(reasons)
        return info

    def open_control(self):
        """Start the control program and wait until the controller is at full speed scaling.

        Measured on the robot: a move commanded 0.13 s after the
        control program started ramped up slowly and ended 61 ms late, while
        moves commanded 0.47 s or more after the start were within 6 ms. The
        controller ramps its speed scaling up when a program starts, so the
        first move waits for it (and for at least `control_settle_s`).
        """
        self.port.open_control()
        self.control_open = True
        t0 = self.tb.now()
        self.tb.sleep(0.05)
        running = self.port.program_running()
        steady, scaling = 0, None
        while True:
            scaling = self._read().speed_scaling
            steady = steady + 1 if scaling >= 0.999 else 0
            waited = self.tb.now() - t0
            if (steady >= 5 and waited >= self.cfg.control_settle_s) or waited >= self.cfg.control_settle_timeout_s:
                break
            self.tb.sleep(self.cfg.poll_s)
        self.log.event("control_opened", program_running=running, waited_s=self.tb.now() - t0,
                       speed_scaling=scaling, settled=steady >= 5)

    @staticmethod
    def _installation_ok(offset, mass, cog) -> bool:
        cog_known = any(all(abs(a - b) <= 1e-4 for a, b in zip(cog, ref))
                        for ref in (g.VERIFIED_PAYLOAD_COG, g.AS_RUN_PAYLOAD_COG))
        return all(abs(x) <= 1e-9 for x in offset) and abs(mass - g.VERIFIED_PAYLOAD_KG) <= 1e-6 and cog_known

    def ensure_installation(self) -> dict:
        """Make the reference tool settings active and prove it by read-back. No motion.

        TCP offset 0 and payload 0.1 kg, set at the start of every session. The pendant's saved
        installation is not touched; a pendant program re-applies its own
        settings when it starts. If the values are already right nothing is
        written. A write that the read-back does not confirm refuses to go on.
        """
        if not self.control_open:
            raise Refusal(["the control interface is not open"])
        st = self._read()
        if not self._stationary(st):
            raise Refusal(["the robot is moving: tool settings are only changed at rest"])
        offset = tuple(self.port.tcp_offset())
        mass, cog = self.port.payload()
        before = dict(tcp_offset=offset, payload_kg=mass, payload_cog=tuple(cog))
        if self._installation_ok(offset, mass, cog):
            self.log.event("installation_ok", **before)
            return dict(changed=False, before=before, after=before, flange_moved_m=0.0)
        flange_before = g.flange_from_tcp(st.pose, offset)
        calls = tuple(self.port.apply_installation(g.VERIFIED_TCP, g.VERIFIED_PAYLOAD_KG, g.AS_RUN_PAYLOAD_COG))
        self.tb.sleep(0.3)
        st2 = self._read()
        offset2 = tuple(self.port.tcp_offset())
        mass2, cog2 = self.port.payload()
        after = dict(tcp_offset=offset2, payload_kg=mass2, payload_cog=tuple(cog2))
        moved = g.distance(flange_before, g.flange_from_tcp(st2.pose, offset2))
        self.log.event("installation_set", calls_ok=calls, before=before, after=after, flange_moved_m=moved)
        if calls != (True, True) or not self._installation_ok(offset2, mass2, cog2) or moved > 0.001:
            raise Refusal([f"tool settings were written but the read-back does not confirm them: TCP offset {offset2}, "
                           f"payload {mass2} kg CoG {list(cog2)}, flange moved {moved * 1000:.2f} mm. No motion was commanded."])
        return dict(changed=True, before=before, after=after, flange_moved_m=moved)

    def capture_baseline(self, window_s: float | None = None):
        """Wrench baseline at rest (software tare; nothing is written to the controller)."""
        if self.cfg.use_zero_ft:
            ok = self.port.zero_ft()
            self.tb.sleep(0.3)
            self.log.event("zero_ft_sensor", ok=ok)
        mean, sd, n = self._mean_wrench(self.cfg.wrench_window_s if window_s is None else window_s)
        self.baseline = mean
        self._baseline_set = True
        self.log.event("wrench_baseline", baseline=self.baseline, sd=sd, samples=n)
        return self.baseline

    def _mean_wrench(self, window_s: float, *, ctx=None, checked: bool = False):
        """Mean and per-axis spread of the wrench over `window_s`, arm at rest.

        `checked` runs the safety checks on every sample (use it once a baseline exists).
        """
        t0 = self.tb.now()
        rows = []
        while True:
            st = self._tick(ctx) if checked else self._read()
            rows.append(st.wrench)
            if self.tb.now() - t0 >= window_s and len(rows) >= 5:
                break
            self.tb.sleep(self.cfg.poll_s)
        n = len(rows)
        mean = tuple(sum(r[i] for r in rows) / n for i in range(6))
        sd = tuple(math.sqrt(sum((r[i] - mean[i]) ** 2 for r in rows) / n) for i in range(6))
        return mean, sd, n

    # ── guarded motion ───────────────────────────────────────────
    def move(self, target, speed: float, accel: float, what: str, *, force_limit=None, torque_limit=None,
             interrupt=None) -> dict:
        """One asynchronous moveL, watched until it ends. Returns a timing report.

        `interrupt()` may return a reason string to end the move early with a
        normal stop (rhythm matters and operator requests). A SafetyFault
        stops the tool and propagates; nothing else is commanded afterwards.
        """
        c = self.cfg
        if self.faulted:
            raise SafetyFault("latched", "a previous safety fault is latched")
        if not self._baseline_set:
            # Force and torque limits are CHANGES from a reading taken at rest. Without one the
            # raw sensor value would be judged, and the sensor's own zero can be off by newtons
            # (a false force fault could otherwise occur at the first approach move).
            self.capture_baseline()
        limits = {}
        if force_limit is not None:
            limits["force_limit"] = force_limit
        if torque_limit is not None:
            limits["torque_limit"] = torque_limit
        try:
            st = self._tick(limits)
        except SafetyFault as fault:
            self._fault(fault)             # found before the move was commanded: same stop, latch and report
        if _vnorm(st.speed[:3]) > c.pre_move_speed_max:
            self._fault(SafetyFault("moving_before_move", f"{what}: {_vnorm(st.speed[:3]):.3f} m/s"))
        start = st.pose
        length = g.distance(start, target)
        expected = trapezoid_time(length, speed, accel)
        turn = g.rotation_angle(start[3:6], target[3:6])
        ctx = dict(limits, segment=(start, target))
        before = self.port.async_progress()
        t_cmd = self.tb.now()
        self.log.event("move_command", what=what, target=tuple(target), speed=speed, accel=accel,
                       length_m=length, expected_s=expected, turn_rad=turn)
        try:
            accepted = self.port.move_l(target, speed, accel, True)
        except Exception as exc:
            self._fault(SafetyFault("move_raised", repr(exc), controllable=False))
        if accepted is not True:
            self._fault(SafetyFault("move_rejected", what))
        deadline = t_cmd + move_time_limit(expected, turn, c.turn_rate_floor)
        seen_running = False
        interrupted = None
        min_scaling = 1.0
        self.peak_force = self.peak_torque = 0.0
        try:
            while True:
                min_scaling = min(min_scaling, self._tick(ctx).speed_scaling)
                now = self.tb.now()
                progress = self.port.async_progress()
                if progress >= 0:
                    seen_running = True
                elif progress != before or seen_running:
                    break
                if interrupt is not None and interrupted is None:
                    why = interrupt(expected - (now - t_cmd))
                    if why:
                        interrupted = why
                        report = self.stop_now(why)
                        if not report["standstill_confirmed"]:
                            raise SafetyFault("stop_unconfirmed", why, controllable=False)
                        break
                if now > deadline:
                    raise SafetyFault("motion_timeout", f"{what}: no completion after {now - t_cmd:.2f} s")
                self.tb.sleep(c.poll_s)
        except SafetyFault as fault:
            self._fault(fault)
        t_done = self.tb.now()
        st = self._read()
        result = dict(what=what, elapsed_s=t_done - t_cmd, expected_s=expected, late_s=t_done - t_cmd - expected,
                      length_m=length, interrupted=interrupted, u_end=g.decompose(st.pose)[0],
                      min_speed_scaling=min_scaling, peak_force_n=self.peak_force, peak_torque_nm=self.peak_torque,
                      force_limit_n=ctx.get("force_limit", c.force_limit_contact),
                      torque_limit_nm=ctx.get("torque_limit", c.torque_limit))
        if interrupted is None:
            # The measured pose trails the commanded one by a few milliseconds at the
            # end of a move; give it a short window before calling it a mismatch.
            settle_until = t_done + c.end_settle_s
            while True:
                pos_err = g.distance(st.pose, target)
                rot_err = g.rotation_angle(st.pose[3:], target[3:])
                if (pos_err <= c.end_pos_tol and rot_err <= c.end_rot_tol) or self.tb.now() >= settle_until:
                    break
                self.tb.sleep(c.poll_s)
                st = self._read()
            result.update(end_pos_err_m=pos_err, end_rot_err_rad=rot_err)
            if pos_err > c.end_pos_tol or rot_err > c.end_rot_tol:
                self._fault(SafetyFault("end_pose_mismatch", f"{what}: {pos_err * 1000:.2f} mm, {rot_err:.4f} rad"))
        self.log.event("move_done", **result)
        return result

    def _fault(self, fault: SafetyFault):
        """Stop the tool, latch, and re-raise. No further motion after this."""
        if self.faulted is None:
            self.faulted = fault
            self.log.event("safety_fault", fault=fault.kind, detail=fault.detail, controllable=fault.controllable)
            fault.stop_report = self.stop_now("safety_fault:" + fault.kind)
        raise fault

    def wait(self, seconds: float, *, force_limit=None, interrupt=None):
        """Stay in place for `seconds`, still watching. Returns an interrupt reason or None."""
        end = self.tb.now() + seconds
        ctx = {"force_limit": force_limit} if force_limit else None
        try:
            while self.tb.now() < end:
                self._tick(ctx)
                if interrupt is not None:
                    why = interrupt(end - self.tb.now())
                    if why:
                        return why
                self.tb.sleep(self.cfg.poll_s)
        except SafetyFault as fault:
            self._fault(fault)
        return None

    # ── landing and lifting ──────────────────────────────────────
    def _force_limit(self) -> float:
        return self.cfg.force_limit_contact if self.cfg.mode == "contact" else self.cfg.force_limit_air

    def goto_lifted(self, u: float) -> dict:
        """From anywhere in the start region: straight up to the lifted line, then along it to u."""
        c = self.cfg
        st = self._read()
        u_now, lift_now, lateral = g.decompose(st.pose)
        u_here = min(1.0, max(0.0, u_now))
        above = g.bow_pose(u_here, lift=c.air_lift)
        report = {}
        if g.distance(st.pose, above) > 0.0005:
            self.capture_baseline()             # at rest where the arm is, on the string or not
            report["lift"] = self.move(above, c.approach_speed, c.approach_accel, "lift to the clear line",
                                       force_limit=c.force_limit_contact)
        # Now certainly in free air: the reference for the tighter in-air limit is taken here.
        self.capture_baseline()
        target = g.bow_pose(u, lift=c.air_lift)
        if g.distance(above, target) > 0.0005:
            report["traverse"] = self.move(target, c.approach_speed, c.approach_accel,
                                           f"traverse above u={u:.3f}", force_limit=c.force_limit_air)
        return report

    def set_down(self, u: float) -> dict:
        """Lower the bow onto the string at u (contact mode) and check the contact force."""
        c = self.cfg
        if c.mode != "contact":
            raise ValueError("set_down is only for contact mode")
        target = g.bow_pose(u, depth=c.depth)
        report = self.move(target, c.setdown_speed, c.approach_accel, f"set down at u={u:.3f}",
                           force_limit=c.force_limit_contact)
        self.wait(0.3, force_limit=c.force_limit_contact)
        try:
            mean, sd, n = self._mean_wrench(c.wrench_window_s, ctx={"force_limit": c.force_limit_contact}, checked=True)
        except SafetyFault as fault:
            self._fault(fault)
        change = [a - b for a, b in zip(mean, self.baseline)]
        force = _vnorm(change[:3])
        report["contact_force_n"] = force
        report["contact_force_xyz_n"] = tuple(change[:3])
        report["contact_torque_nm"] = _vnorm(change[3:])
        report["contact_force_sd_n"] = _vnorm(sd[:3])
        report["in_band"] = c.contact_band[0] <= force <= c.contact_band[1]
        self.log.event("set_down", u=u, depth_mm=c.depth * 1000.0, contact_force_n=force,
                       contact_force_xyz_n=report["contact_force_xyz_n"],
                       contact_torque_nm=report["contact_torque_nm"], contact_force_sd_n=report["contact_force_sd_n"],
                       samples=n, in_band=report["in_band"], band=c.contact_band)
        return report

    def lift_here(self, *, unknown_preload: bool = False) -> dict | None:
        """Raise the bow to the clear height from wherever it is, keeping its orientation.

        `unknown_preload`: the baseline was taken with the bow resting on the string under a load
        nobody measured (the stand-alone lift, for example after a stop-and-hold at the force limit).
        Letting go of that load is itself a wrench change as large as the load, so the limits for
        this one move away from the string are doubled.
        """
        c = self.cfg
        st = self._read()
        _, lift_now, _ = g.decompose(st.pose)
        if lift_now >= c.air_lift - 0.001:
            return None
        target = g.lifted(st.pose, c.air_lift - lift_now)
        k = 2.0 if unknown_preload else 1.0
        if unknown_preload:
            self.log.event("lift_release_limits", force_limit=k * c.force_limit_contact, torque_limit=k * c.torque_limit)
        return self.move(target, c.approach_speed, c.approach_accel, "lift clear",
                         force_limit=k * c.force_limit_contact, torque_limit=k * c.torque_limit)

    def bow_target(self, u: float) -> tuple:
        c = self.cfg
        return g.bow_pose(u, depth=c.depth) if c.mode == "contact" else g.bow_pose(u, lift=c.air_lift)

    # ── Conductor-driven session ─────────────────────────────────
    def run_session(self, clock: ConductorClock, grid: ScoreGrid, *, nominal_bpm: float | None = None,
                    on_armed=None) -> dict:
        """Arm at the start point, follow the clock bow by bow, end, lift.

        Assumes preflight passed and the control interface is open.
        `nominal_bpm` fixes the first bow's tempo; without it the first READY
        packet's tempo is used. `on_armed()` runs once the bow is in place
        (local clock sources start there; a live Conductor is told to begin).
        """
        c, lim = self.cfg, self.cfg.limits
        summary = dict(score=grid.name, mode=c.mode, bows=[], reason=None, completed_beats=0.0,
                       safety_fault=None, lifted=False)

        def interrupt(remaining_s):
            if self.operator_stop is not None and self.operator_stop():
                return "operator_stop"
            snap, stop, last, closed = clock.view()
            if stop is not None:
                # "ended": the piece is over, so a bow that is about to reach its change may do so.
                # "stopped" (or anything else): somebody is stopping the run. Act now, whatever is left.
                if stop.get("state") == "ended" and remaining_s is not None and remaining_s <= c.finish_within_s:
                    return None
                return "conductor_stop:" + str(stop.get("state"))
            if closed is not None:
                return "conductor_gone:" + closed
            if last is not None and self.tb.now() - last > c.clock_timeout_s:
                return "conductor_silent"
            if snap is not None and snap.state != "running":
                if remaining_s is not None and remaining_s <= c.finish_within_s:
                    return None
                return "conductor_paused"
            return None

        def ended():
            """Reason to end at a bow change, or None."""
            if self.operator_stop is not None and self.operator_stop():
                return "operator_stop"
            snap, stop, last, closed = clock.view()
            if stop is not None:
                return "conductor_stop:" + str(stop.get("state"))
            if closed is not None:
                return "conductor_gone:" + closed
            if last is not None and self.tb.now() - last > c.clock_timeout_s:
                return "conductor_silent"
            return None

        try:
            # 1. tempo for the first bow: from READY packets, else the given nominal
            if nominal_bpm is None and not self._baseline_set:
                # This wait watches the wrench too, and a limit only means something as a change from
                # a reading taken at rest. Judged raw, the sensor's own zero ended a session 0.1 s
                # after preflight, before the Conductor had connected: "15.04 N > 10.00 N" with the
                # bow at rest in the air, because the raw reading at rest had drifted during the day.
                self.capture_baseline()
            t_wait = self.tb.now()
            while nominal_bpm is None:
                self._tick({"force_limit": c.force_limit_contact})
                snap = clock.view()[0]
                if snap is not None:
                    nominal_bpm = snap.estimated_tempo_bpm
                    break
                why = ended()
                if why or self.tb.now() - t_wait > c.ready_timeout_s:
                    summary["reason"] = why or "no_conductor_packets"
                    return summary
                self.tb.sleep(c.poll_s)
            first_T = grid.beats_per_bow * 60.0 / nominal_bpm
            u0 = first_start_u(first_T, lim, +1)
            summary.update(nominal_bpm=nominal_bpm, u_start=u0)

            # 2. approach, baseline, set-down
            self.goto_lifted(u0)
            self.capture_baseline()
            if c.mode == "contact":
                landing = self.set_down(u0)
                summary["contact_force_n"] = landing["contact_force_n"]
                if not landing["in_band"]:
                    summary["reason"] = "contact_force_out_of_band"
                    return summary
            self.log.event("armed", u=u0, mode=c.mode, nominal_bpm=nominal_bpm)
            if on_armed is not None:
                on_armed()      # no unsolicited frames go to the Conductor: its receiver rejects unknown kinds

            # 3. wait for the start
            t_armed = self.tb.now()
            while True:
                snap = clock.view()[0]
                if snap is not None and snap.state == "running":
                    break
                why = self.wait(c.poll_s, force_limit=self._force_limit(), interrupt=lambda r: ended())
                if why or self.tb.now() - t_armed > c.ready_timeout_s:
                    summary["reason"] = why or "start_timeout"
                    return summary

            # 4. bows
            u, direction = u0, +1
            while True:
                why = ended()
                if why:
                    summary["reason"] = why
                    break
                snap = clock.view()[0]
                if snap.state != "running":
                    # The Conductor paused: the wire state went back to READY with the
                    # beat position frozen. Hold the bow still; continue if it resumes.
                    self.log.event("paused", beat_position=snap.beat_position, u=u)
                    summary["pauses"] = summary.get("pauses", 0) + 1
                    t_pause = self.tb.now()
                    why = None
                    while True:
                        snap = clock.view()[0]
                        if snap.state == "running":
                            break
                        why = self.wait(c.poll_s, force_limit=self._force_limit(), interrupt=lambda r: ended())
                        if why is None and self.tb.now() - t_pause > c.pause_hold_s:
                            why = "conductor_paused"
                        if why:
                            break
                    if why:
                        summary["reason"] = why
                        break
                    self.log.event("resumed", beat_position=snap.beat_position, held_s=self.tb.now() - t_pause)
                    direction = +1 if (lim.u_hi - u) >= (u - lim.u_lo) else -1     # the side with more bow left
                now = self.tb.now()
                phase = clock.phase_of(snap, now)
                boundary = next_boundary(phase, grid, c.min_fraction)
                tempo = snap.estimated_tempo_bpm
                while boundary is not None and (boundary - phase) * 60.0 / tempo < lim.t_min:
                    boundary = boundary + grid.beats_per_bow if boundary < grid.total_beats else None
                if boundary is None:
                    # Too little of the score is left for another bow.
                    summary["reason"] = "score_complete"
                    summary["completed_beats"] = max(summary["completed_beats"], min(phase, grid.total_beats))
                    break
                duration = (boundary - phase) * 60.0 / tempo
                plan = plan_bow(u, direction, duration, lim)
                bow = dict(index=len(summary["bows"]), t_dispatch=now, phase=phase, boundary=boundary,
                           tempo_bpm=tempo, quality=snap.quality, direction=direction, u_start=plan.u_start,
                           u_end=plan.u_end, length_m=plan.length, speed=plan.speed, duration_s=duration,
                           move_time_s=plan.move_time, wait_after_s=plan.wait_after, flags=list(plan.flags))
                self.log.event("bow_dispatch", **bow)
                stopped = None
                if not plan.rest_only:
                    result = self.move(self.bow_target(plan.u_end), plan.speed, plan.accel,
                                       f"bow {bow['index']} {'down' if direction > 0 else 'up'}",
                                       force_limit=self._force_limit(), interrupt=interrupt)
                    bow.update(elapsed_s=result["elapsed_s"], late_s=result["late_s"], u_reached=result["u_end"],
                               peak_force_n=result["peak_force_n"], peak_torque_nm=result["peak_torque_nm"])
                    stopped = result["interrupted"]
                    u = result["u_end"] if stopped else plan.u_end
                if stopped is None and plan.wait_after > 0:
                    stopped = self.wait(plan.wait_after, force_limit=self._force_limit(), interrupt=interrupt)
                t_end = self.tb.now()
                snap_end = clock.view()[0]
                bow.update(t_end=t_end, phase_at_end=clock.phase_of(snap_end, t_end),
                           boundary_error_beats=clock.phase_of(snap_end, t_end) - boundary, interrupted=stopped)
                summary["bows"].append(bow)
                self.log.event("bow_done", **bow)
                if stopped == "conductor_paused":
                    continue                        # the pause branch above holds, then resumes or ends
                if stopped:
                    summary["reason"] = stopped
                    break
                summary["completed_beats"] = boundary
                if boundary >= grid.total_beats - 1e-9:
                    # The last bar line was reached. A STOP(ended) that the
                    # Conductor sends at the same instant does not change that.
                    summary["reason"] = "score_complete"
                    break
                direction = -direction

            if summary["reason"] == "score_complete" and self.notify:
                self.notify(self._completion_frame(clock, grid, summary))
            return summary
        except SafetyFault as fault:
            summary["reason"] = "safety_fault:" + fault.kind
            summary["safety_fault"] = dict(kind=fault.kind, detail=fault.detail, controllable=fault.controllable,
                                           stop=fault.stop_report)
            if self.notify:
                self.notify(self._failure_frame(clock, "safety_fault:" + fault.kind))
            return summary
        finally:
            # Normal endings lift clear. After a safety fault nothing more is commanded.
            if self.faulted is None and self.control_open:
                try:
                    if not self.confirm_standstill():
                        self.stop_now("end_of_session")
                    self.lift_here()
                    summary["lifted"] = True
                except SafetyFault as fault:
                    summary["safety_fault"] = dict(kind=fault.kind, detail=fault.detail,
                                                   controllable=fault.controllable, stop=fault.stop_report)
                    summary["reason"] = (summary["reason"] or "") + "|lift_fault:" + fault.kind
            summary["standstill_confirmed"] = self._safe_standstill()
            summary["clock"] = dict(session_id=clock.session_id, clock_domain=clock.clock_domain,
                                    accepted=clock.accepted, ignored=dict(clock.ignored))
            self.log.event("session_end", **{k: v for k, v in summary.items() if k != "bows"},
                           bows=len(summary["bows"]))

    def _safe_standstill(self) -> bool:
        try:
            return self.confirm_standstill(0.5)
        except Exception:
            return False

    def _echo_fields(self, clock: ConductorClock) -> dict:
        snap = clock.view()[0]
        raw = snap.raw if snap else {}
        out = dict(session_id=clock.session_id, clock_domain=clock.clock_domain)
        for key in ("playback_id", "contract", "experimental", "score_id", "manifest_sha256", "score_total_beats"):
            if raw.get(key) is not None:
                out[key] = raw[key]
        return out

    def _completion_frame(self, clock, grid, summary) -> dict:
        frame = dict(version=1, kind="score_playback_complete", **self._echo_fields(clock))
        frame.setdefault("score_id", grid.name)
        frame.setdefault("score_total_beats", int(grid.total_beats))
        frame.update(completed_bows=len(summary["bows"]), completed_score_beats=int(summary["completed_beats"]),
                     origin="robot_executor_async_moveL_completion")
        return frame

    def _failure_frame(self, clock, reason) -> dict:
        return dict(version=1, kind="executor_failure", reason=reason, **self._echo_fields(clock))
