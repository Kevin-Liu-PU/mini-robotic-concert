"""Command line for the Conductor-driven cello executor.

    python -m conductor_cello state                      read-only snapshot, no control program
    python -m conductor_cello diag  --go                 open the control interface, set tool settings, check, no motion
    python -m conductor_cello install --go               only the tool-settings step (TCP 0, payload 0.1 kg), no motion

Every command that opens the control interface first makes the reference
tool settings active (TCP offset 0, payload 0.1 kg) and confirms them by
read-back. The pendant's saved installation is never changed.
    python -m conductor_cello home  --go                 from a nearby pose outside the start region: up, across, down to the clear line
    python -m conductor_cello goto  --u 0.5 --go         to the clear line above bow fraction u
    python -m conductor_cello bow   --mode air ...       ONE guarded bow (timing and stop commissioning)
    python -m conductor_cello lift  --go                 raise the bow clear from where it is
    python -m conductor_cello play  --source ...         a whole session: metronome, recorded stream or live Conductor

Add --mock to any command to run against the built-in kinematic robot instead
of the UR5e (no network, real-time pace). Without --mock every command that
opens the control interface or moves needs --go, and asks for a typed
confirmation unless --operator-present is given by the person at the robot.

Exit codes: 0 normal, 2 refused before anything was commanded, 3 safety fault,
4 other error.
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime
import json
import math
import signal
import sys
from pathlib import Path

from . import __version__, geometry as g, homing
from .bowplan import SCORES, BowLimits, trapezoid_time
from .clock import ConductorClock
from .driver import MockPort, RealRtdePort, Timebase, read_tcp_offset_readonly
from .executor import ExecConfig, Executor, Refusal, RunLog, SafetyFault
from .wire import DEFAULT_PORT, ScriptedSource, WireServer, load_recorded_stream, metronome_packets

ROBOT_IP = "192.168.1.100"
RUNS = Path(__file__).resolve().parent / "runs"


def _say(*a):
    print(*a, flush=True)


def _echo(row):
    kind = row["kind"]
    if kind in ("move_command",):
        _say(f"  move  {row['what']}: {row['length_m'] * 1000:.1f} mm at {row['speed']:.3f} m/s, expect {row['expected_s']:.3f} s")
    elif kind == "move_done":
        tail = f"interrupted ({row['interrupted']})" if row.get("interrupted") else f"end error {row.get('end_pos_err_m', 0) * 1000:.2f} mm"
        slow = "" if row.get("min_speed_scaling", 1.0) >= 0.99 else f", speed scaling dipped to {row['min_speed_scaling']:.2f}"
        _say(f"  done  {row['what']}: {row['elapsed_s']:.3f} s ({row['late_s'] * 1000:+.1f} ms vs trapezoid), {tail}{slow}")
    elif kind == "bow_dispatch":
        _say(f"bow {row['index']:2d} {'down' if row['direction'] > 0 else 'up  '} beat {row['phase']:6.2f} -> {row['boundary']:5.1f}  "
             f"{row['tempo_bpm']:5.1f} BPM  {row['duration_s']:.2f} s  u {row['u_start']:.3f}->{row['u_end']:.3f}  "
             f"{row['speed']:.3f} m/s  {row['quality']} {' '.join(row['flags'])}")
    elif kind == "bow_done":
        _say(f"       ended {row['boundary_error_beats']:+.3f} beat from the bar line")
    elif kind == "set_down":
        verdict = "inside" if row["in_band"] else "OUTSIDE"
        _say(f"[set_down] bow on the string at u = {row['u']:.3f}"
             + (f", {row['depth_mm']:+.1f} mm past the taught line" if row.get("depth_mm") else "")
             + f": contact force {row['contact_force_n']:.2f} N "
             f"(mean of {row.get('samples', 1)} readings, spread {row.get('contact_force_sd_n', 0.0):.2f} N), "
             f"{verdict} the accepted {row['band'][0]}..{row['band'][1]} N; "
             f"torque {row.get('contact_torque_nm', 0.0):.2f} Nm, reference about "
             f"{g.rest_torque_reference(row['u']):.2f} Nm at this place")
    elif kind == "stop":
        _say(f"[stop] {row['reason']}: stop call {row['stop_call']} after {row.get('stop_call_s', 0) * 1000:.0f} ms, "
             f"standstill {'confirmed' if row['standstill_confirmed'] else 'NOT CONFIRMED'} after {row['standstill_s'] * 1000:.0f} ms, "
             f"travelled {(row.get('stop_distance_m') or 0) * 1000:.1f} mm, dashboard fallback {row['dashboard_stop']}")
    elif kind == "control_opened":
        note = "" if row.get("settled") else "   (speed scaling never read 1.0: going on after the timeout)"
        _say(f"[control_opened] program running {row['program_running']}; waited {row['waited_s']:.2f} s for full speed "
             f"scaling, now {row['speed_scaling']:.2f}{note}")
    elif kind == "installation_ok":
        _say(f"tool settings already right: TCP offset zero, payload {row['payload_kg']} kg (nothing written)")
    elif kind == "installation_set":
        b, a = row["before"], row["after"]
        _say("tool settings set for this session (the pendant's saved installation is not touched):")
        _say(f"  before: TCP offset {tuple(round(x, 5) for x in b['tcp_offset'])}   payload {b['payload_kg']} kg, CoG {list(b['payload_cog'])}")
        _say(f"  after:  TCP offset {tuple(round(x, 5) for x in a['tcp_offset'])}   payload {a['payload_kg']} kg, CoG {list(a['payload_cog'])}")
        _say(f"  flange moved {row['flange_moved_m'] * 1000:.2f} mm (expected 0: only the bookkeeping changes)")
    elif kind == "preflight" and row.get("ok"):
        _say(f"[preflight] ok: flange u {row['u']:.3f}, {row['lift_m'] * 1000:.1f} mm above the line, payload {row['payload_kg']} kg, slider {row['speed_fraction']:.2f}")
    elif kind in ("safety_fault", "armed", "paused", "resumed", "preflight", "session_end",
                  "zero_ft_sensor", "wire_connected"):
        shown = {k: v for k, v in row.items() if k not in ("kind", "t", "wall", "pose", "q_deg", "baseline")}
        _say(f"[{kind}] " + json.dumps(shown, default=str))


def _build(args, stage):
    tb = Timebase()
    limits = BowLimits(v_pref=args.v_pref) if getattr(args, "v_pref", None) else BowLimits()
    extra = {}
    if getattr(args, "contact_band", None):
        extra["contact_band"] = tuple(args.contact_band)
    if getattr(args, "depth_mm", None):
        extra["depth"] = args.depth_mm / 1000.0
    try:
        cfg = ExecConfig(mode=getattr(args, "mode", "air"), limits=limits,
                         approach_speed=getattr(args, "approach_speed", 0.10),
                         setdown_speed=getattr(args, "setdown_speed", 0.05),
                         use_zero_ft=getattr(args, "zero_ft", False), **extra).validate()
    except ValueError as exc:               # a setting outside its allowed range: say so before anything is opened
        raise Refusal([str(exc)])
    if args.mock:
        start = g.lifted(g.pose_at(0.5)) if stage != "state" else g.FSAFE
        port = MockPort(tb, start_pose=start)
    else:
        port = RealRtdePort(args.ip, tb)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    folder = RUNS / f"{stamp}-{stage}{'-mock' if args.mock else ''}"
    log = RunLog(folder, tb, echo=_echo)
    ex = Executor(port, cfg, timebase=tb, log=log)
    log.event("start", stage=stage, version=__version__, mock=bool(args.mock), argv=sys.argv[1:],
              config={k: (v if not hasattr(v, "__dataclass_fields__") else vars(v)) for k, v in vars(cfg).items()})
    _say(f"run folder: {folder}")
    return ex, port, log, folder


def _authorise(args, what: str) -> None:
    """Nothing is sent to the real controller without --go and a person at the robot."""
    if args.mock:
        return
    if not args.go:
        raise Refusal([f"{what}: add --go to run this on the real robot (it was not contacted)"])
    if args.operator_present:
        return
    if not sys.stdin.isatty():
        raise Refusal(["no terminal to confirm at: pass --operator-present when a person is at the robot with the E-stop"])
    answer = input(f"\n{what}\nA person is at the robot with the E-stop in hand? Type YES to continue: ")
    if answer.strip().upper() != "YES":
        raise Refusal([f"not confirmed by the operator (typed {answer.strip()!r}; type YES to go ahead)"])


def _print_state(info: dict, extra=None):
    same = all(abs(a - b) < 1e-9 for a, b in zip(info["pose"], info["flange_pose"]))
    if same and info["tcp_offset_known"]:
        _say(f"  flange pose      {[round(x, 4) for x in info['pose']]}   (TCP offset is zero)")
    elif same:
        _say(f"  reported pose    {[round(x, 4) for x in info['pose']]}   (TCP offset not read yet: this is the flange only if it is zero)")
    else:
        _say(f"  reported TCP     {[round(x, 4) for x in info['pose']]}   (pose of the ACTIVE tool point, not the flange)")
        _say(f"  flange pose      {[round(x, 4) for x in info['flange_pose']]}   (TCP offset removed)")
    _say(f"  joints (deg)     {info['q_deg']}")
    _say(f"  on the bow line  u {info['u']:.3f}   lift {info['lift_m'] * 1000:.1f} mm   lateral {info['lateral_m'] * 1000:.1f} mm   "
         f"rotation from taught {info['rot_from_taught_deg']:.1f} deg")
    _say(f"  modes            safety {info['safety_mode']} (1 = NORMAL)   robot {info['robot_mode']} (7 = RUNNING)   "
         f"runtime {info['runtime_state']}   speed slider {info['speed_fraction']:.3f}")
    _say(f"  payload          {info['payload_kg']} kg, CoG {list(info['payload_cog'])}   (reference: 0.1 kg)")
    _say(f"  wrench           |F| {info['force_n']:.2f} N   |T| {info['torque_nm']:.2f} Nm   (raw, no baseline)")
    if info.get("joints_outside_envelope"):
        _say(f"  joints outside the historical working configuration: {info['joints_outside_envelope']}")
    for k, v in (extra or {}).items():
        _say(f"  {k:16s} {v}")


def cmd_state(args):
    ex, port, log, folder = _build(args, "state")
    try:
        port.open_receive()                 # receive only: no program is started
        extra = {}
        offset = None
        if not args.mock:
            try:
                offset = read_tcp_offset_readonly(args.ip)
                extra["tcp offset"] = offset
            except Exception as exc:
                extra["tcp offset"] = "unavailable: " + repr(exc)
        else:
            offset = port.tcp_offset()
            extra["tcp offset"] = offset
        info = ex.describe(tcp_offset=offset)
        if offset is not None and any(abs(x) > 1e-9 for x in offset):
            extra["tcp offset"] = f"{tuple(offset)}   NOT ZERO: the taught points assume zero"
        log.event("state", **info, **{k.replace(" ", "_"): v for k, v in extra.items()})
        _say("\nRead-only state (nothing was sent to the controller):")
        _print_state(info, extra)
        lo, hi = ex.cfg.corridor_lift
        inside = (ex.cfg.corridor_u[0] <= info["u"] <= ex.cfg.corridor_u[1] and lo <= info["lift_m"] <= hi
                  and info["lateral_m"] <= ex.cfg.corridor_lateral and info["rot_from_taught_deg"] <= ex.cfg.corridor_rot_deg
                  and not info["joints_outside_envelope"])
        _say(f"\n  start region: {'INSIDE' if inside else 'OUTSIDE - move the arm by hand to above the string line before any programmed move'}")
        if not Executor._installation_ok(offset or (1,) * 6, info["payload_kg"], tuple(info["payload_cog"])):
            _say("  tool settings: not the reference ones. Nothing to do by hand: every command that opens the")
            _say("                 control interface sets TCP offset 0 and payload 0.1 kg itself and checks them.")
        return 0
    finally:
        port.close()
        log.close()


SETS_TOOL = ("Tool settings for this session are set first if needed: TCP offset 0, payload 0.1 kg "
             "(the reference setup). The pendant's saved installation is not touched.")


def _open(args, ex, port, what, *, preflight: bool = True, install: bool = True):
    """Read-only look, confirmation, control interface, tool settings, preflight. No motion."""
    if not args.mock and not args.go:
        raise Refusal([f"{what} Add --go to run this on the real robot (it was not contacted)."])
    port.open_receive()                     # read-only so far
    offset = None
    try:
        offset = port.tcp_offset() if args.mock else read_tcp_offset_readonly(args.ip)
    except Exception:
        pass
    _say("\nState before anything is sent:")
    _print_state(ex.describe(tcp_offset=offset),
                 {"tcp offset": offset if offset is not None else "not readable before the control interface is open"})
    _authorise(args, what + ("\n" + SETS_TOOL if install else ""))
    ex.open_control()                       # uploads ur_rtde's control script: no motion
    if install:
        ex.ensure_installation()            # TCP 0 / 0.1 kg for this session, checked by read-back
    if not preflight:
        return None
    info = ex.preflight()
    _say("preflight passed")
    return info


def _abort_hooks(ex, *, hold_on_first: bool = False):
    """Ctrl-C handling.

    Sessions and bows: the first Ctrl-C is a normal stop (stop, confirm
    standstill, lift clear); a second one is stop-and-hold with nothing
    further commanded. Plain positioning moves hold on the first Ctrl-C.
    """
    hits = {"n": 0}
    limit = 1 if hold_on_first else 2

    def handler(signum, frame):
        hits["n"] += 1
        _say(f"\n[operator] Ctrl-C #{hits['n']}: " + ("stopping" if hits["n"] < limit else "STOP AND HOLD"))

    signal.signal(signal.SIGINT, handler)
    ex.operator_stop = lambda: hits["n"] >= 1
    ex.operator_abort = lambda: hits["n"] >= limit
    return hits


def _await_conductor_stop(clock, *, timeout_s: float, tb, give_up=None) -> str:
    """After the score is complete and the arm is lifted: stay connected until the Conductor's
    own STOP has arrived, so it gets its acknowledgement (the receiver thread sends it on arrival).

    The Conductor's sender never reconnects, and without the acknowledgement it reports the stop as
    unconfirmed (or waits for it). Returns why the wait ended.
    """
    end = tb.now() + timeout_s
    while tb.now() < end:
        _, stop, _, closed = clock.view()
        if stop is not None:
            return "stop_acknowledged"
        if closed is not None:
            return "conductor_gone"
        if give_up is not None and give_up():
            return "operator"
        tb.sleep(0.02)
    return "timeout"


def _wrench_margin(log) -> None:
    """Largest force and torque change of the run against the limits that applied."""
    rows = [e for e in log.of("move_done") if "peak_force_n" in e]
    if not rows:
        return
    f = max(rows, key=lambda e: e["peak_force_n"] / e["force_limit_n"])
    t = max(rows, key=lambda e: e["peak_torque_nm"] / e["torque_limit_nm"])
    _say(f"largest force change {f['peak_force_n']:.2f} N of the {f['force_limit_n']:.0f} N limit ({f['what']}); "
         f"largest torque change {t['peak_torque_nm']:.2f} Nm of the {t['torque_limit_nm']:.1f} Nm limit ({t['what']})")


def cmd_diag(args):
    """No motion. The dry run of everything a session does before it moves:
    does the controller run a control program, do the tool settings take, is the arm in place?
    """
    ex, port, log, folder = _build(args, "diag")
    try:
        _open(args, ex, port, "DIAG: open the control interface and check it. No motion.",
              preflight=False, install=False)
        running = port.program_running()
        progress = port.async_progress() if running else None
        _say(f"\n1. Controller runs the control program: {'YES' if running else 'NO'}"
             + (f"   (async progress register {progress})" if running else ""))
        if not running:
            log.event("diag", program_running=False)
            _say("DIAG FAILED: the control script did not start. Check the pendant log; nothing was moved.")
            return 2
        _say("2. Tool settings:")
        ex.ensure_installation()            # raises Refusal if the read-back does not confirm the write
        try:
            ex.preflight()
            reasons = []
        except Refusal as refusal:
            reasons = refusal.reasons
        positioning = [r for r in reasons if "start region" in r or "working configuration" in r]
        blocking = [r for r in reasons if r not in positioning]
        log.event("diag", program_running=True, async_progress=progress, positioning=positioning, blocking=blocking)
        if blocking:
            _say("3. Not ready for motion. Fix these first:")
            for reason in blocking + positioning:
                _say("   - " + reason)
            return 2
        if positioning:
            _say("3. The arm is not where a move may start:")
            for reason in positioning:
                _say("   - " + reason)
            _say("DIAG OK for the controller and the tool settings. Next: move the arm by hand, then run `state`. "
                 "No motion was commanded.")
            return 0
        _say("3. Ready for a first move: speed slider and start region check out.")
        _say("DIAG OK. No motion was commanded.")
        return 0
    finally:
        port.close()
        log.close()


def cmd_install(args):
    """Only the tool-settings step, on its own. No motion.

    Every motion command runs this step itself at its start; this command is
    for doing or checking it separately.
    """
    ex, port, log, folder = _build(args, "install")
    try:
        _open(args, ex, port, "INSTALL: set TCP offset 0 and payload 0.1 kg (the reference setup). No motion.",
              preflight=False, install=False)
        result = ex.ensure_installation()
        _say("INSTALL OK" + ("" if result["changed"] else " (nothing had to be written)")
             + ". These values last until a pendant program or installation is loaded, or the controller restarts.")
        return 0
    finally:
        port.close()
        log.close()


def cmd_home(args):
    """From a nearby pose outside the start region back to the clear line above the A string."""
    ex, port, log, folder = _build(args, "home")
    try:
        what = "HOME: bring the arm back to the clear line above the A string."
        if not args.mock and not args.go:
            raise Refusal([f"{what} Add --go to run this on the real robot (it was not contacted)."])
        port.open_receive()                                     # read-only so far
        offset = port.tcp_offset() if args.mock else read_tcp_offset_readonly(args.ip)
        if offset is None:
            raise Refusal(["could not read the TCP offset read-only, so the flange position is not known"])
        st = ex._read()
        info = ex.describe(st, tcp_offset=offset)
        flange = tuple(info["flange_pose"])
        _say("\nState before anything is sent:")
        _print_state(info, {"tcp offset": offset})
        reasons = homing.check_start(flange, st.q)
        if st.safety_mode != 1 or st.robot_mode != 7:
            reasons.append(f"safety mode {st.safety_mode} / robot mode {st.robot_mode}: need 1 (NORMAL) and 7 (RUNNING)")
        if abs(st.speed_fraction - 1.0) > ex.cfg.slider_tol:
            reasons.append(f"speed slider is {st.speed_fraction:.3f}, need 1.000")
        if not info["stationary"]:
            reasons.append("robot is moving")
        if reasons:
            raise Refusal(reasons)
        steps = homing.plan_home(flange, u_target=args.u, travel_z=args.travel_z, speed=args.speed)
        lines = homing.describe(steps, flange)
        log.event("home_plan", start=flange, steps=[dict(name=s.name, target=s.target, speed=s.speed) for s in steps])
        _say("\nPlanned path (flange). It goes UP first:")
        for line in lines:
            _say(line)
        _say("  The force sensor is zeroed before the first motion (the bow must be touching nothing).")
        _say(f"  Any force change over {homing.FORCE_LIMIT_N:.0f} N, or Ctrl-C, stops the arm and holds it there.")
        _authorise(args, what + " Watch the whole bow, hand on the E-stop.\n" + SETS_TOOL)

        ex.open_control()
        ex.ensure_installation()
        st = ex._read()                                         # with TCP offset zero this is the flange
        if g.distance(st.pose, flange) > 0.001:
            raise Refusal([f"flange is {g.distance(st.pose, flange) * 1000:.1f} mm from where the plan started: plan again"])
        # the controller's own kinematics: every target reachable, no change of arm configuration
        q_near = tuple(st.q)
        for step in steps:
            if not port.pose_reachable(step.target):
                raise Refusal([f"the controller says step '{step.name}' is not reachable inside its safety limits"])
            q_next = port.inverse_kinematics(step.target, q_near)
            if q_next is None:
                raise Refusal([f"no joint solution for step '{step.name}'"])
            change = max(abs(math.degrees(a - b)) for a, b in zip(q_next, q_near))
            if change > homing.MAX_SEGMENT_JOINT_CHANGE_DEG:
                raise Refusal([f"step '{step.name}' would turn a joint by {change:.0f} deg: not the same arm configuration"])
            q_near = q_next
        _say("controller confirms: every step is reachable in the same arm configuration")
        _abort_hooks(ex, hold_on_first=True)
        ok = port.zero_ft()
        ex.tb.sleep(0.3)
        log.event("zero_ft_sensor", ok=ok)
        ex.capture_baseline()
        for step in steps:
            ex.move(step.target, step.speed, g.MOVE_ACCEL, "home: " + step.name, force_limit=homing.FORCE_LIMIT_N)
        still = ex.confirm_standstill()
        try:
            final = ex.preflight()
        except Refusal as refusal:
            _say("\nHOME moved the arm, but the check at the end is not clean. Do not continue until this is understood:")
            for reason in refusal.reasons:
                _say("  - " + reason)
            return 2
        _say(f"\nHOME OK: standstill {still}; flange at u {final['u']:.3f}, {final['lift_m'] * 1000:.1f} mm above the string line, "
             f"inside the start region.")
        return 0
    finally:
        port.close()
        log.close()


def cmd_goto(args):
    ex, port, log, folder = _build(args, "goto")
    try:
        _open(args, ex, port, f"GOTO: lift to the clear line and move above u = {args.u:.3f} at {args.approach_speed} m/s.")
        _abort_hooks(ex, hold_on_first=True)
        ex.capture_baseline()
        ex.goto_lifted(args.u)
        _say("GOTO done; standstill confirmed: " + str(ex.confirm_standstill()))
        return 0
    finally:
        port.close()
        log.close()


def cmd_lift(args):
    ex, port, log, folder = _build(args, "lift")
    try:
        _open(args, ex, port, "LIFT: raise the bow straight up to the clear height from where it is.")
        _abort_hooks(ex, hold_on_first=True)
        ex.capture_baseline()
        report = ex.lift_here(unknown_preload=True)
        _say("already clear" if report is None else "LIFT done")
        return 0
    finally:
        port.close()
        log.close()


def cmd_bow(args):
    """One guarded bow between two bow fractions, for timing and stop commissioning."""
    ex, port, log, folder = _build(args, "bow-" + args.mode)
    try:
        length = abs(args.u_to - args.u_from) * g.BOW_LENGTH
        expect = trapezoid_time(length, args.speed, g.MOVE_ACCEL)
        lim = ex.cfg.limits
        if not (lim.u_lo <= min(args.u_from, args.u_to) and max(args.u_from, args.u_to) <= lim.u_hi):
            raise Refusal([f"u must stay inside {lim.u_lo}..{lim.u_hi}"])
        if not (lim.v_min <= args.speed <= lim.v_max):
            raise Refusal([f"speed must stay inside the recorded {lim.v_min}..{lim.v_max} m/s"])
        _open(args, ex, port,
              f"BOW ({args.mode}): u {args.u_from:.3f} -> {args.u_to:.3f} = {length * 1000:.0f} mm at {args.speed} m/s, "
              f"about {expect:.2f} s" + (f", STOP TEST after {args.stop_at_s} s" if args.stop_at_s else "")
              + (", repeated back and forth x" + str(args.repeat) if args.repeat > 1 else ""))
        hits = _abort_hooks(ex, hold_on_first=True)
        ex.goto_lifted(args.u_from)
        ex.capture_baseline()
        if args.mode == "contact":
            landing = ex.set_down(args.u_from)
            if not landing["in_band"]:
                _say(f"contact force {landing['contact_force_n']:.2f} N is outside {ex.cfg.contact_band}: lifting, no bow.")
                ex.lift_here()
                return 2
        results = []
        a, b = args.u_from, args.u_to
        for i in range(args.repeat):
            t0 = ex.tb.now()

            def interrupt(remaining, t0=t0):
                if args.stop_at_s and i == 0 and ex.tb.now() - t0 >= args.stop_at_s:
                    return "stop_test"
                return None

            r = ex.move(ex.bow_target(b), args.speed, g.MOVE_ACCEL, f"bow {i} u {a:.3f}->{b:.3f}",
                        force_limit=ex._force_limit(), interrupt=interrupt)
            results.append(r)
            if r["interrupted"]:
                break
            a, b = b, a
        if args.mode == "contact":
            ex.lift_here()
        late = [r["late_s"] * 1000 for r in results if not r["interrupted"]]
        log.event("bow_summary", results=results)
        if late:
            _say(f"\nmoveL time minus trapezoid: {['%+.1f ms' % x for x in late]}   (July dataset: +3.2 ms median, +7.1 max)")
        _wrench_margin(log)
        return 0
    finally:
        port.close()
        log.close()


def cmd_play(args):
    ex, port, log, folder = _build(args, f"play-{args.source}-{args.mode}")
    server = None
    try:
        grid = SCORES[args.song]
        clock = ConductorClock(session_id=args.session, clock_domain=args.clock_domain)
        nominal = args.bpm
        packets = None
        if args.source == "metronome":
            if not args.bpm:
                raise Refusal(["--bpm is required for the metronome source"])
            if args.bars is not None:
                # a shortened score, so the run ends at its last bar line like the whole piece would,
                # instead of starting one more bow and being stopped a moment later
                if args.bars < 1:
                    raise Refusal(["--bars must be at least 1"])
                grid = dataclasses.replace(grid, total_beats=min(grid.total_beats, args.bars * grid.beats_per_bow))
            packets = list(metronome_packets(bpm=args.bpm, meter=int(grid.beats_per_bow), total_beats=grid.total_beats,
                                             ready_s=2.0))
        elif args.source == "replay":
            if not args.stream:
                raise Refusal(["--stream PATH is required for the replay source"])
            rec = load_recorded_stream(args.stream)
            first = next((i for i, (_, r) in enumerate(rec) if r.get("state") == "running"), None)
            if first is None:
                raise Refusal(["the recorded stream never reaches state running"])
            ready = [r for _, r in rec[:first] if r.get("state") == "ready"]
            if nominal is None:
                src = ready[-1] if ready else rec[first][1]
                nominal = src.get("estimated_tempo_bpm") or src["tempo_bpm"]
            lead = min(2.0, rec[first][0])
            packets = [(t - (rec[first][0] - lead), r) for t, r in rec if t >= rec[first][0] - lead]
            meter = rec[first][1]["meter"][0]
            if meter != int(grid.beats_per_bow):
                raise Refusal([f"stream meter {meter} does not match --song {args.song} ({int(grid.beats_per_bow)} beats per bow)"])
        _say(f"song {grid.name}: {grid.planned_bows} bows of {grid.beats_per_bow:g} beats, mode {args.mode}, source {args.source}"
             + (f", first-bow tempo {nominal:.1f} BPM" if nominal else ", first-bow tempo from the Conductor's READY packets"))
        _open(args, ex, port, f"PLAY ({args.mode}, {args.source}): approach, "
              + ("set the bow DOWN on the string" + (f" {ex.cfg.depth * 1000:+.1f} mm past the taught line" if ex.cfg.depth else "")
                 + ", " if args.mode == "contact" else "stay on the clear line (no contact), ")
              + "then one moveL per bar.")
        hits = _abort_hooks(ex)
        if args.source == "live":
            server = WireServer(clock, port=args.port, log=log.event).start()
            ex.notify = server.send
            _say(f"listening for the Conductor on 127.0.0.1:{server.port} (wire v1, JSON Lines)")

        def armed():
            if packets is not None:
                source = ScriptedSource(clock, packets, start_s=ex.tb.now())
                ex.feeder = source.step
            _say("\nARMED: bow in place. " + ("The Conductor may start." if args.source == "live" else "Clock source started."))

        summary = ex.run_session(clock, grid, nominal_bpm=nominal, on_armed=armed)
        (folder / "summary.json").write_text(json.dumps(summary, indent=1, default=str) + "\n")
        errs = [abs(b["boundary_error_beats"]) for b in summary["bows"] if not b.get("interrupted")]
        _say(f"\nended: {summary['reason']}   bows {len(summary['bows'])}   beats {summary['completed_beats']:g}   "
             f"lifted {summary['lifted']}   standstill {summary['standstill_confirmed']}")
        if errs:
            errs.sort()
            _say(f"bow change vs bar line: median {errs[len(errs) // 2]:.3f} beat, max {errs[-1]:.3f} beat")
        if "contact_force_n" in summary:
            _say(f"contact force at set-down: {summary['contact_force_n']:.2f} N")
        _wrench_margin(log)
        if server is not None and summary["reason"] == "score_complete":
            _say(f"\nThe robot is finished and lifted. Staying connected up to {args.stop_wait:g} s for the Conductor's STOP "
                 "(Ctrl-C to leave now).")
            n0 = hits["n"]
            why = _await_conductor_stop(clock, timeout_s=args.stop_wait, tb=ex.tb, give_up=lambda: hits["n"] > n0)
            log.event("conductor_stop_wait", result=why)
            _say({"stop_acknowledged": "Conductor's STOP received and acknowledged.",
                  "conductor_gone": "The Conductor closed the connection without a STOP.",
                  "operator": "Left at the keyboard; the Conductor's STOP was not acknowledged.",
                  "timeout": "No STOP from the Conductor in that time; it will report a missing acknowledgement."}[why])
        if summary["safety_fault"]:
            _say("\nSAFETY FAULT: " + json.dumps(summary["safety_fault"], default=str))
            _say("The tool was stopped and nothing further was commanded. Check the robot and the pendant by eye.")
            _say("When it is safe:  python -m conductor_cello lift --go")
            return 3
        return 0
    finally:
        if server is not None:
            server.close()
        port.close()
        log.close()


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m conductor_cello", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--ip", default=ROBOT_IP)
    common.add_argument("--mock", action="store_true", help="built-in kinematic robot, no network")
    common.add_argument("--go", action="store_true", help="really contact the robot")
    common.add_argument("--operator-present", action="store_true",
                        help="a person is at the robot with the E-stop (replaces the typed confirmation)")
    motion = argparse.ArgumentParser(add_help=False)
    motion.add_argument("--approach-speed", type=float, default=0.10, help="m/s for lift and traverse moves")
    motion.add_argument("--setdown-speed", type=float, default=0.05, help="m/s onto the string")
    motion.add_argument("--contact-band", type=float, nargs=2, metavar=("LOW_N", "HIGH_N"), default=None,
                        help="contact force accepted at set-down (default 0.7 6.5)")
    motion.add_argument("--depth-mm", type=float, default=None,
                        help="press this many mm past the taught line in contact mode; "
                             "allowed range -1.5 (lighter) to +2.0 (heavier)")
    motion.add_argument("--zero-ft", action="store_true", help="tare with zeroFtSensor instead of a software baseline")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("state", parents=[common]).set_defaults(fn=cmd_state)
    sub.add_parser("diag", parents=[common]).set_defaults(fn=cmd_diag)
    sub.add_parser("install", parents=[common]).set_defaults(fn=cmd_install)
    s = sub.add_parser("home", parents=[common])
    s.add_argument("--u", type=float, default=0.5, help="bow fraction to end above (default 0.5)")
    s.add_argument("--travel-z", type=float, default=0.40, help="flange height for the sideways move, m (default 0.40)")
    s.add_argument("--speed", type=float, default=0.05, help="m/s for the lift, the sideways move and the descent")
    s.set_defaults(fn=cmd_home)
    s = sub.add_parser("goto", parents=[common, motion])
    s.add_argument("--u", type=float, required=True)
    s.set_defaults(fn=cmd_goto)
    sub.add_parser("lift", parents=[common, motion]).set_defaults(fn=cmd_lift)
    s = sub.add_parser("bow", parents=[common, motion])
    s.add_argument("--mode", choices=["air", "contact"], default="air")
    s.add_argument("--u-from", type=float, default=0.50)
    s.add_argument("--u-to", type=float, default=0.60)
    s.add_argument("--speed", type=float, default=0.12)
    s.add_argument("--repeat", type=int, default=1, help="number of bows, alternating direction")
    s.add_argument("--stop-at-s", type=float, default=None, help="request a stop this long into the first bow")
    s.set_defaults(fn=cmd_bow)
    s = sub.add_parser("play", parents=[common, motion])
    s.add_argument("--source", choices=["metronome", "replay", "live"], required=True)
    s.add_argument("--song", choices=sorted(SCORES), required=True)
    s.add_argument("--mode", choices=["air", "contact"], default="air")
    s.add_argument("--bpm", type=float, default=None, help="metronome tempo, or first-bow tempo override")
    s.add_argument("--bars", type=int, default=None, help="metronome: stop after this many bars")
    s.add_argument("--stream", default=None, help="replay: recorded stream.jsonl or wire.json")
    s.add_argument("--port", type=int, default=DEFAULT_PORT, help="live: loopback port to listen on")
    s.add_argument("--stop-wait", type=float, default=30.0,
                   help="live: seconds to stay connected after the score is complete, for the Conductor's STOP")
    s.add_argument("--session", default=None, help="accept only this session_id (default: bind to the first sender)")
    s.add_argument("--clock-domain", default=None, help="accept only this clock_domain")
    s.add_argument("--v-pref", type=float, default=None, help="preferred bow speed, m/s (default 0.14)")
    s.set_defaults(fn=cmd_play)
    args = p.parse_args(argv)
    try:
        return args.fn(args)
    except Refusal as refusal:
        _say("\nREFUSED - nothing was commanded:")
        for reason in refusal.reasons:
            _say("  - " + reason)
        return 2
    except SafetyFault as fault:
        _say(f"\nSAFETY FAULT {fault.kind}: {fault.detail}")
        _say("The tool was stopped and nothing further was commanded. Check the robot and the pendant by eye.")
        return 3
    except KeyboardInterrupt:
        _say("\ninterrupted")
        return 4
    except (OSError, RuntimeError) as exc:
        _say(f"\nERROR {type(exc).__name__}: {exc}")
        _say("If this is a connection error: is this computer on the robot network and the robot in Remote Control?")
        return 4


if __name__ == "__main__":
    sys.exit(main())
