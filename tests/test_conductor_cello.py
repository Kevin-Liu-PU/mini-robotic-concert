"""Offline tests for the Conductor-driven bow executor (mock robot, fake time).

Nothing here touches the network, the robot, audio or the camera. The mock
robot follows the same zero-to-zero trapezoid moveL timing that was measured
on the real arm, on a fake clock, so a 48-beat piece runs in milliseconds.
"""
import math

import pytest

from conductor_cello import geometry as g
from conductor_cello.bowplan import (SCORES, BowLimits, first_start_u, length_for, next_boundary, nominal,
                                     plan_bow, speed_for, trapezoid_time)
from conductor_cello.clock import ConductorClock
from conductor_cello.driver import FakeTimebase, MockPort
from conductor_cello.executor import ExecConfig, Executor, Refusal, SafetyFault
from conductor_cello.wire import ScriptedSource, metronome_packets


# ── geometry ─────────────────────────────────────────────────────────

def test_pose_and_lift_follow_the_taught_points():
    import numpy as np
    assert (g.U_MIN, g.U_MAX) == (0.04, 0.96)
    frog, tip = np.array(g.FROG), np.array(g.TIP)
    theta = np.linalg.norm(tip[3:])
    alt = tip[3:] * (theta - 2 * math.pi) / theta
    canon = alt if np.linalg.norm(alt - frog[3:]) < np.linalg.norm(tip[3:] - frog[3:]) else tip[3:]
    for u in (0.0, 0.04, 0.25, 0.5, 0.731, 0.96, 1.0):
        want = np.concatenate([frog[:3] + u * (tip[:3] - frog[:3]), frog[3:] + u * (canon - frog[3:])])
        if u == 0.0:
            want = frog
        if u == 1.0:
            want = tip
        assert np.allclose(g.pose_at(u), want, rtol=0, atol=1e-12)
    assert abs(g.LIFT_M - 0.054591632934) < 1e-9
    assert abs(g.BOW_LENGTH - float(np.linalg.norm(tip[:3] - frog[:3]))) < 1e-15
    lifted = g.lifted(g.pose_at(0.3))
    u, lift, lateral = g.decompose(lifted)
    assert abs(u - 0.3) < 1e-12 and abs(lift - g.LIFT_M) < 1e-12 and lateral < 1e-12
    assert math.degrees(g.rotation_angle(g.FROG[3:], g.TIP[3:])) == pytest.approx(4.28, abs=0.01)


def test_depth_is_clamped_to_the_taught_envelope():
    deep = g.apply_depth(g.pose_at(0.5), 0.010)
    assert g.decompose(deep)[1] == pytest.approx(-g.MAX_INWARD_DEPTH, abs=1e-12)
    light = g.apply_depth(g.pose_at(0.5), -0.010)
    assert g.decompose(light)[1] == pytest.approx(g.MAX_OUTWARD_DEPTH, abs=1e-12)


# ── planner ──────────────────────────────────────────────────────────

LIM = BowLimits().validate()


def test_trapezoid_matches_the_measured_formula():
    # full bow at 0.15 m/s, a = 1.2  ->  3.452 s ideal
    assert trapezoid_time(g.BOW_LENGTH, 0.15, 1.2) == pytest.approx(0.15 / 1.2 + g.BOW_LENGTH / 0.15)
    for v, T in ((0.09, 4.0), (0.14, 2.0), (0.25, 1.5)):
        L = length_for(v, T, 1.2)
        assert speed_for(L, T, 1.2) == pytest.approx(v)
        assert trapezoid_time(L, v, 1.2) == pytest.approx(T)
    assert speed_for(0.5, 1.0, 1.2) is None


@pytest.mark.parametrize("bpm,beats", [(50, 4), (60, 4), (66, 4), (72, 4), (90, 4), (120, 4), (168, 4),
                                       (50, 3), (60, 3), (72, 3), (120, 3), (168, 3)])
def test_steady_tempo_stays_in_recorded_speed_range_and_on_the_stick(bpm, beats):
    T = beats * 60 / bpm
    u = first_start_u(T, LIM)
    direction = 1
    for _ in range(40):
        p = plan_bow(u, direction, T, LIM)
        assert LIM.v_min - 1e-12 <= p.speed <= LIM.v_max + 1e-12
        assert LIM.u_lo - 1e-9 <= min(p.u_start, p.u_end) and max(p.u_start, p.u_end) <= LIM.u_hi + 1e-9
        assert p.move_time + p.wait_after == pytest.approx(T, abs=1e-9)
        u, direction = p.u_end, -direction
    v, L = nominal(T, LIM)
    assert abs(p.length - L) < 1e-6                   # steady state is the centred nominal stroke


def test_stroke_never_exceeds_remaining_travel_whatever_the_request():
    for u in (0.07, 0.3, 0.5, 0.8, 0.93):
        for direction in (1, -1):
            for T in (0.6, 1.0, 2.5, 4.0, 7.0, 12.0):
                p = plan_bow(u, direction, T, LIM)
                avail = ((LIM.u_hi - u) if direction > 0 else (u - LIM.u_lo)) * g.BOW_LENGTH
                assert p.length <= avail + 1e-12
                assert LIM.u_lo - 1e-9 <= p.u_end <= LIM.u_hi + 1e-9
                if not p.rest_only:
                    assert LIM.v_min - 1e-12 <= p.speed <= LIM.v_max + 1e-12
                assert p.move_time <= T + 1e-9


def test_running_out_of_bow_waits_instead_of_leaving_the_range():
    p = plan_bow(0.90, 1, 4.0, LIM)                   # 21 mm left, four seconds asked
    assert p.rest_only or "bow_exhausted" in p.flags or "fit_remaining_travel" in p.flags
    assert p.u_end <= LIM.u_hi + 1e-9 and p.move_time + p.wait_after == pytest.approx(4.0)
    q = plan_bow(0.94, 1, 2.0, LIM)                   # nothing left in this direction
    assert q.rest_only and q.flags == ("no_travel_left",) and q.wait_after == 2.0


def test_unequal_bows_recentre_instead_of_drifting():
    u, direction = first_start_u(3.6, LIM), 1
    lo, hi = 1.0, 0.0
    durations = [3.6, 3.0, 4.2, 3.3, 3.9, 2.8, 4.4, 3.6] * 12
    for T in durations:
        p = plan_bow(u, direction, T, LIM)
        u, direction = p.u_end, -direction
        lo, hi = min(lo, u), max(hi, u)
    assert 0.08 < lo < 0.25 and 0.75 < hi < 0.92


def test_bar_grid_boundaries():
    tw = SCORES["twinkle"]
    assert next_boundary(0.0, tw, 0.25) == 4.0
    assert next_boundary(2.9, tw, 0.25) == 4.0        # more than a quarter bar left: its own bow
    assert next_boundary(3.2, tw, 0.25) == 8.0        # less: merge into the next bar
    assert next_boundary(9.5, tw, 0.25) == 12.0       # a jump of the beat position just re-locks
    assert next_boundary(45.0, tw, 0.25) == 48.0
    assert next_boundary(47.5, tw, 0.25) is None
    ar = SCORES["arirang"]
    assert next_boundary(0.0, ar, 0.25) == 3.0 and ar.planned_bows == 16 and tw.planned_bows == 12


# ── sessions on the mock robot ───────────────────────────────────────

def session(mode="contact", song="twinkle", bpm=66.0, *, packets=None, port_kwargs=None, cfg=None,
            faults=None, ready_s=6.0, operator_stop_at=None):
    tb = FakeTimebase()
    port = MockPort(tb, **(port_kwargs or {}))
    port.open_receive()
    ex = Executor(port, cfg or ExecConfig(mode=mode), timebase=tb)
    clock = ConductorClock()
    grid = SCORES[song]
    if packets is None:
        packets = metronome_packets(bpm=bpm, meter=int(grid.beats_per_bow), total_beats=grid.total_beats,
                                    ready_s=ready_s)
    src = ScriptedSource(clock, packets, start_s=tb.now())
    ex.feeder = src.step
    frames = []
    ex.notify = frames.append
    for name, at in (faults or {}).items():
        port.faults[name] = tb.now() + at
    if operator_stop_at is not None:
        t_stop = tb.now() + operator_stop_at
        ex.operator_stop = lambda: tb.now() >= t_stop
    ex.open_control()
    ex.preflight()
    summary = ex.run_session(clock, grid)
    return summary, ex, port, frames, clock


@pytest.mark.parametrize("mode,song,bpm,bows", [("air", "twinkle", 66, 12), ("contact", "twinkle", 66, 12),
                                                ("contact", "arirang", 72, 16), ("contact", "twinkle", 120, 12),
                                                ("contact", "arirang", 60, 16)])
def test_steady_clock_plays_every_bar_and_lifts(mode, song, bpm, bows):
    s, ex, port, frames, clock = session(mode, song, bpm)
    assert s["reason"] == "score_complete" and len(s["bows"]) == bows and s["completed_beats"] == 48.0
    assert s["lifted"] and s["standstill_confirmed"] and s["safety_fault"] is None
    assert max(abs(b["boundary_error_beats"]) for b in s["bows"]) < 0.02
    assert all(LIM.v_min <= b["speed"] <= LIM.v_max for b in s["bows"])
    assert [f["kind"] for f in frames] == ["score_playback_complete"]
    assert frames[0]["completed_bows"] == bows and frames[0]["completed_score_beats"] == 48
    u, lift, lateral = g.decompose(port.pose)
    assert lift == pytest.approx(g.LIFT_M, abs=1e-6)
    # one moveL per bow, nothing else between the set-down and the final lift
    bow_moves = [c for c in port.calls if c[0] == "move_l"]
    assert sum(1 for e in ex.log.of("move_done") if e["what"].startswith("bow ")) == bows
    assert not any(c[0] in ("zero_ft", "program_stop") for c in port.calls)
    if mode == "air":
        assert all(g.decompose(c[2])[1] > 0.05 for c in bow_moves)      # never commanded onto the string


def test_stop_in_the_middle_of_a_bow_stops_then_lifts():
    grid = SCORES["twinkle"]
    pk = [(t, p) for t, p in metronome_packets(bpm=66, meter=4, total_beats=48, ready_s=6.0)]
    cut = 6.0 + 5.2                                     # 1.6 s into the second bow
    pk = [(t, p) for t, p in pk if t < cut]
    stop = dict(pk[-1][1], kind="STOP", state="stopped", sequence=pk[-1][1]["sequence"] + 1)
    pk.append((cut, stop))
    s, ex, port, frames, clock = session(packets=pk)
    assert s["reason"] == "conductor_stop:stopped" and len(s["bows"]) == 2
    assert s["bows"][-1]["interrupted"] == "conductor_stop:stopped"
    stops = ex.log.of("stop")
    assert len(stops) == 1 and stops[0]["standstill_confirmed"] and stops[0]["dashboard_stop"] is None
    assert stops[0]["stop_distance_m"] < 0.01 and stops[0]["standstill_s"] < 0.3
    assert s["lifted"] and g.decompose(port.pose)[1] == pytest.approx(g.LIFT_M, abs=1e-6)
    assert frames == []                                 # an explicit stop is answered by the ACK only
    calls = [c[0] for c in port.calls]
    assert calls.index("stop_l") < len(calls) - 1 and calls.count("stop_l") == 1


def test_stopped_just_before_the_bow_change_is_not_deferred():
    # Only "ended" may let a nearly finished bow complete. "stopped" is somebody stopping the run.
    pk = [(t, p) for t, p in metronome_packets(bpm=66, meter=4, total_beats=48, ready_s=6.0)]
    cut = 6.0 + 2 * 4 * 60 / 66 - 0.15                  # 150 ms before the second bar line
    pk = [(t, p) for t, p in pk if t < cut]
    pk.append((cut, dict(pk[-1][1], kind="STOP", state="stopped", sequence=pk[-1][1]["sequence"] + 1)))
    s, ex, port, frames, clock = session(packets=pk)
    assert s["reason"] == "conductor_stop:stopped" and len(s["bows"]) == 2
    assert s["bows"][-1]["interrupted"] == "conductor_stop:stopped"
    stops = ex.log.of("stop")
    assert len(stops) == 1 and stops[0]["standstill_confirmed"]
    assert stops[0]["t_request"] - (ex.log.of("armed")[0]["t"] + cut) < 0.02     # acted on within 20 ms of arrival
    assert s["lifted"] and frames == []


def test_stop_just_before_the_bow_change_lets_the_bow_finish():
    pk = [(t, p) for t, p in metronome_packets(bpm=66, meter=4, total_beats=48, ready_s=6.0)]
    cut = 6.0 + 2 * 4 * 60 / 66 - 0.15                  # 150 ms before the second bar line
    pk = [(t, p) for t, p in pk if t < cut]
    pk.append((cut, dict(pk[-1][1], kind="STOP", state="ended", sequence=pk[-1][1]["sequence"] + 1)))
    s, ex, port, frames, clock = session(packets=pk)
    assert s["reason"] == "conductor_stop:ended" and len(s["bows"]) == 2
    assert s["bows"][-1]["interrupted"] is None and ex.log.of("stop") == []
    assert s["completed_beats"] == 8.0 and s["lifted"]


def test_conductor_going_silent_ends_the_run_normally():
    pk = [(t, p) for t, p in metronome_packets(bpm=66, meter=4, total_beats=48, ready_s=6.0) if t < 6.0 + 9.0]
    tb_packets = pk                                     # stream simply ends: no STOP
    s, ex, port, frames, clock = session(packets=tb_packets)
    assert s["reason"].startswith("conductor_gone") or s["reason"] == "conductor_silent"
    assert s["safety_fault"] is None and s["lifted"] and s["standstill_confirmed"]
    assert len(ex.log.of("stop")) == 1 and ex.log.of("safety_fault") == []


def test_operator_stop_is_a_normal_stop():
    s, ex, port, frames, clock = session(operator_stop_at=6.0 + 4.5)
    assert s["reason"] == "operator_stop" and s["lifted"] and s["safety_fault"] is None


def test_missed_beats_and_low_confidence_do_not_stop_anything():
    pk = []
    for t, p in metronome_packets(bpm=66, meter=4, total_beats=48, ready_s=6.0):
        if p["state"] == "running" and 10.0 < t < 22.0:
            p = dict(p, quality="predicted" if t < 16.0 else "unreliable")
        pk.append((t, p))
    s, ex, port, frames, clock = session(packets=pk)
    assert s["reason"] == "score_complete" and len(s["bows"]) == 12
    assert {b["quality"] for b in s["bows"]} >= {"tracked", "predicted"}


def test_beat_position_jump_relocks_on_the_next_bar_line():
    pk = []
    for t, p in metronome_packets(bpm=66, meter=4, total_beats=48, ready_s=6.0):
        if p["state"] == "running" and t >= 6.0 + 12.0:                 # recovery: the Conductor is 1.5 beats ahead
            p = dict(p, beat_position=min(48.0, p["beat_position"] + 1.5))
        pk.append((t, p))
    s, ex, port, frames, clock = session(packets=pk)
    assert s["reason"] == "score_complete" and s["safety_fault"] is None
    assert all(abs(b["boundary_error_beats"]) < 0.05 for b in s["bows"][5:])
    assert all(float(b["boundary"]) % 4.0 == 0.0 for b in s["bows"])
    assert len(s["bows"]) in (11, 12)                                   # one bar may be merged, never a fault


def test_tempo_change_is_absorbed_at_the_bow_change():
    pk, beat, last_t, seq = [], 0.0, None, 0
    for t, p in metronome_packets(bpm=66, meter=4, total_beats=400, ready_s=6.0, tail_s=0.0):
        if p["state"] != "running":
            pk.append((t, p))
            continue
        bpm = 66.0 if beat < 16 else 84.0
        if last_t is not None:
            beat += (t - last_t) * bpm / 60.0
        last_t = t
        if beat >= 48.0:
            pk.append((t, dict(p, kind="STOP", state="ended", beat_position=48.0)))
            break
        pk.append((t, dict(p, beat_position=beat, tempo_bpm=bpm, estimated_tempo_bpm=bpm)))
    s, ex, port, frames, clock = session(packets=pk)
    assert s["reason"] == "score_complete" and s["safety_fault"] is None
    errors = [b["boundary_error_beats"] for b in s["bows"]]
    late = [i for i, e in enumerate(errors) if abs(e) > 0.05]
    # Speed is fixed inside a bow, so the one bow that was already under way
    # when the tempo jumped by 27 % ends about a beat off. The very next bow
    # is planned from the new tempo and lands on its bar line again.
    assert late == [4] and errors[4] == pytest.approx(4 * (84 / 66 - 1), abs=0.05)
    assert all(float(b["boundary"]) % 4.0 == 0.0 for b in s["bows"])
    assert s["bows"][2]["tempo_bpm"] == 66.0 and s["bows"][-1]["tempo_bpm"] == 84.0
    assert s["bows"][-1]["speed"] > s["bows"][2]["speed"]              # faster bars, same stick region


# ── safety faults: stop, hold, command nothing more ──────────────────

@pytest.mark.parametrize("fault,kind,controllable", [("force_spike", "force_limit", True),
                                                     ("slider_changed", "speed_slider_changed", True),
                                                     ("protective_stop", "safety_mode", False),
                                                     ("telemetry_freeze", "telemetry_stale", False),
                                                     ("program_dies", "control_program_stopped", False)])
def test_safety_fault_stops_and_never_moves_again(fault, kind, controllable):
    s, ex, port, frames, clock = session(faults={fault: 6.0 + 5.0})
    assert s["reason"] == "safety_fault:" + kind
    assert s["safety_fault"]["kind"] == kind and s["safety_fault"]["controllable"] is controllable
    assert not s["lifted"]
    t_fault = ex.log.of("safety_fault")[0]["t"]
    assert [c for c in port.calls if c[0] == "move_l" and c[1] > t_fault] == []      # no lift, no retry
    assert [f["kind"] for f in frames] == ["executor_failure"]
    assert not any(c[0] in ("zero_ft",) for c in port.calls)
    with pytest.raises(SafetyFault):
        ex.move(g.lifted(port.pose), 0.05, 1.2, "anything after a fault")


def test_rejected_move_is_a_fault_not_a_retry():
    s, ex, port, frames, clock = session(faults={"move_rejected": 6.0 + 5.0})
    assert s["reason"] == "safety_fault:move_rejected"
    t_fault = ex.log.of("safety_fault")[0]["t"]
    assert [c for c in port.calls if c[0] == "move_l" and c[1] > t_fault] == []


def test_ignored_stop_escalates_to_the_dashboard_program_stop():
    s, ex, port, frames, clock = session(faults={"stop_ignored": 0.0}, operator_stop_at=6.0 + 4.5)
    stop = ex.log.of("stop")[0]
    assert stop["dashboard_stop"] == "mock_program_stop" and stop["standstill_confirmed"]
    assert any(c[0] == "program_stop" for c in port.calls)


# ── preflight refuses without commanding anything ────────────────────

def _preflight(port_kwargs):
    tb = FakeTimebase()
    port = MockPort(tb, **port_kwargs)
    port.open_receive()
    ex = Executor(port, ExecConfig(mode="air"), timebase=tb)
    ex.open_control()
    return ex, port


def test_preflight_refuses_the_pose_the_arm_was_parked_in_on_2026_11_10():
    parked = (0.516487, 0.557252, 0.638974, 1.223604, 1.498544, 2.100512)
    q = (-2.502891604100, -0.890259460812, 0.795774284993, -3.147715707819, -1.517830197011, -2.640615288411)
    ex, port = _preflight(dict(start_pose=parked, q=q))
    with pytest.raises(Refusal) as info:
        ex.preflight()
    text = str(info.value)
    assert "outside the start region" in text and "historical working configuration" in text
    assert [c for c in port.calls if c[0] == "move_l"] == []


@pytest.mark.parametrize("kwargs,needle", [(dict(tcp_offset=(0.0282, -0.0961, -0.0997, 0, 0, 0)), "TCP offset"),
                                           (dict(payload_kg=0.19, payload_cog=(0.128, -0.006, 0.019)), "payload")])
def test_preflight_refuses_a_different_installation(kwargs, needle):
    ex, port = _preflight(kwargs)
    with pytest.raises(Refusal) as info:
        ex.preflight()
    assert needle in str(info.value) and [c for c in port.calls if c[0] == "move_l"] == []


def test_preflight_accepts_fsafe_and_the_string_line():
    for pose in (g.FSAFE, g.pose_at(0.5), g.lifted(g.pose_at(0.2))):
        ex, port = _preflight(dict(start_pose=pose))
        info = ex.preflight()
        assert info["joints_outside_envelope"] == []


def test_contact_force_out_of_band_ends_before_any_bow():
    cfg = ExecConfig(mode="contact", contact_band=(5.0, 6.5))            # mock contact is 3.6 N
    s, ex, port, frames, clock = session(cfg=cfg)
    assert s["reason"] == "contact_force_out_of_band" and s["bows"] == [] and s["lifted"]


# ── pause / resume, transport and command line ───────────────────────

def test_pause_holds_the_bow_and_resume_continues():
    pk, shift = [], 0.0
    for t, p in metronome_packets(bpm=66, meter=4, total_beats=48, ready_s=6.0):
        if p["state"] == "running" and 6.0 + 9.0 <= t < 6.0 + 13.0:      # paused for four seconds
            pk.append((t, dict(p, state="ready", quality="unreliable", beat_position=9.0 * 66 / 60)))
            shift = 4.0 * 66 / 60
            continue
        if p["state"] == "running" and t >= 6.0 + 13.0:
            p = dict(p, beat_position=p["beat_position"] - shift)
        if p["kind"] == "STOP":
            continue
        pk.append((t, p))
    last_t, last = pk[-1]
    beat = last["beat_position"]
    seq = last["sequence"]
    t = last_t
    while beat < 48.0:                                                    # run on to the end of the score
        t += 0.02
        beat += 0.02 * 66 / 60
        seq += 1
        pk.append((t, dict(last, sequence=seq, beat_position=min(beat, 48.0))))
    pk.append((t + 0.02, dict(last, sequence=seq + 1, kind="STOP", state="ended", beat_position=48.0)))
    s, ex, port, frames, clock = session(packets=pk)
    assert s["reason"] == "score_complete" and s.get("pauses") == 1 and s["safety_fault"] is None
    assert len(ex.log.of("paused")) == 1 and len(ex.log.of("resumed")) == 1
    assert ex.log.of("resumed")[0]["held_s"] == pytest.approx(4.0, abs=0.3)
    assert any(b["interrupted"] == "conductor_paused" for b in s["bows"])
    assert s["completed_beats"] == 48.0 and s["lifted"]


def test_pause_without_resume_ends_normally_after_the_hold():
    pk = []
    for t, p in metronome_packets(bpm=66, meter=4, total_beats=48, ready_s=6.0, tail_s=0.0):
        if p["kind"] == "STOP" or t > 6.0 + 30.0:
            continue
        if p["state"] == "running" and t >= 6.0 + 9.0:
            p = dict(p, state="ready", quality="unreliable", beat_position=9.0 * 66 / 60)
        pk.append((t, p))
    s, ex, port, frames, clock = session(packets=pk)
    assert s["reason"] == "conductor_paused" and s["lifted"] and s["safety_fault"] is None


def test_wire_server_follows_a_live_socket_and_acks_stop():
    """Real loopback TCP and real time (about four seconds), mock robot."""
    import json
    import socket
    import threading
    import time
    from conductor_cello.bowplan import ScoreGrid
    from conductor_cello.driver import Timebase
    from conductor_cello.wire import WireServer

    tb = Timebase()
    port = MockPort(tb)
    port.open_receive()
    ex = Executor(port, ExecConfig(mode="air", approach_speed=0.25), timebase=tb)
    clock = ConductorClock()
    server = WireServer(clock, port=0).start()
    ex.notify = server.send
    grid = ScoreGrid("two-bars", 4.0, 8.0)
    received = []

    def conductor():
        sock = socket.create_connection(("127.0.0.1", server.port), 2)
        sock.settimeout(0.01)
        t0 = time.perf_counter()
        stop_sent = None
        for t_rel, pkt in metronome_packets(bpm=240, meter=4, total_beats=8, ready_s=1.5, rate_hz=50,
                                            session_id="live-test", end_state="stopped", tail_s=0.4):
            while time.perf_counter() - t0 < t_rel:
                time.sleep(0.001)
            pkt["source_monotonic_ns"] = time.perf_counter_ns()
            sock.sendall((json.dumps(pkt) + "\n").encode())
            if pkt["kind"] == "STOP" and stop_sent is None:
                stop_sent = pkt["sequence"]
            try:
                data = sock.recv(4096)
                received.extend(json.loads(line) for line in data.split(b"\n") if line.strip())
            except socket.timeout:
                pass
        time.sleep(0.2)
        try:
            data = sock.recv(4096)
            received.extend(json.loads(line) for line in data.split(b"\n") if line.strip())
        except (socket.timeout, OSError):
            pass
        sock.close()

    thread = threading.Thread(target=conductor)
    ex.open_control()
    ex.preflight()
    try:
        summary = ex.run_session(clock, grid, nominal_bpm=240.0, on_armed=thread.start)
        thread.join(5)
    finally:
        server.close()
    assert summary["reason"] in ("score_complete", "conductor_stop:stopped") and len(summary["bows"]) == 2
    assert summary["lifted"] and summary["safety_fault"] is None
    kinds = [m["kind"] for m in received]
    assert "ACK" in kinds
    ack = next(m for m in received if m["kind"] == "ACK")
    assert ack["status"] == "stopped_latched" and ack["session_id"] == "live-test" and ack["version"] == 1
    assert type(ack["ack_sequence"]) is int and ack["clock_domain"] == "local-perf-counter"
    assert clock.session_id == "live-test"


def test_command_line_refuses_the_real_robot_without_go(monkeypatch, capsys, tmp_path):
    import conductor_cello.__main__ as cli

    def boom(self):
        raise AssertionError("the robot must not be contacted without --go")

    monkeypatch.setattr(cli.RealRtdePort, "open_receive", boom)
    monkeypatch.setattr(cli.RealRtdePort, "open_control", boom)
    monkeypatch.setattr(cli, "RUNS", tmp_path)
    for argv in (["diag"], ["goto", "--u", "0.5"], ["lift"], ["bow"],
                 ["play", "--source", "metronome", "--song", "twinkle", "--bpm", "66"]):
        assert cli.main(argv) == 2
    assert "REFUSED" in capsys.readouterr().out


def test_command_line_mock_session_runs_end_to_end(monkeypatch, capsys, tmp_path):
    import json
    import conductor_cello.__main__ as cli
    monkeypatch.setattr(cli, "RUNS", tmp_path)
    code = cli.main(["play", "--mock", "--source", "metronome", "--song", "twinkle", "--bpm", "240", "--bars", "2",
                     "--mode", "contact", "--approach-speed", "0.25", "--setdown-speed", "0.25"])
    assert code == 0
    folder = next(tmp_path.iterdir())
    summary = json.loads((folder / "summary.json").read_text())
    # Two of twelve bars were requested: the shortened score ends at its last bar line, like the whole piece would.
    assert summary["reason"] == "score_complete" and summary["completed_beats"] == 8.0
    assert len(summary["bows"]) == 2 and summary["lifted"]
    events = [json.loads(line)["kind"] for line in (folder / "events.jsonl").read_text().splitlines()]
    assert events.count("bow_done") == 2 and "set_down" in events and "armed" in events
    assert (folder / "state.jsonl").stat().st_size > 1000


@pytest.mark.parametrize("after_s,phase", [(0.3, "before the first move, while the baseline is taken"),
                                           (0.8, "during the lift"), (2.5, "during the traverse")])
def test_keyboard_abort_stops_and_holds_without_lifting(after_s, phase):
    tb = FakeTimebase()
    port = MockPort(tb, start_pose=g.pose_at(0.3))
    port.open_receive()
    ex = Executor(port, ExecConfig(mode="contact"), timebase=tb)
    ex.open_control()
    ex.preflight()
    t_abort = tb.now() + after_s
    ex.operator_abort = lambda: tb.now() >= t_abort
    with pytest.raises(SafetyFault) as info:
        ex.goto_lifted(0.8)
    assert info.value.kind == "operator_abort" and info.value.stop_report["standstill_confirmed"], phase
    moves_after = [c for c in port.calls if c[0] == "move_l" and c[1] > t_abort]
    assert moves_after == [] and ex.faulted is not None, phase
    moved = any(c[0] == "move_l" for c in port.calls)
    assert moved == (after_s > 0.5), phase                 # the 0.3 s case is caught before anything is commanded


def test_contact_force_is_a_windowed_mean_not_one_sample():
    # free-air force ripple as measured on the robot (2.44 Hz); a single sample would be off by up to the amplitude
    tb = FakeTimebase()
    port = MockPort(tb, ripple_n=1.0)
    port.open_receive()
    ex = Executor(port, ExecConfig(mode="contact"), timebase=tb)
    ex.open_control()
    ex.preflight()
    ex.goto_lifted(0.5)
    ex.capture_baseline()
    landing = ex.set_down(0.5)
    assert landing["in_band"] and landing["contact_force_n"] == pytest.approx(MockPort.CONTACT_FORCE_N, abs=0.35)
    assert 0.5 < landing["contact_force_sd_n"] < 0.9                      # the ripple is reported, not hidden
    row = ex.log.of("set_down")[0]
    assert row["samples"] > 100 and row["contact_torque_nm"] == pytest.approx(MockPort.CONTACT_TORQUE_NM, abs=0.05)
    base = ex.log.of("wrench_baseline")[-1]
    assert base["samples"] > 100 and abs(base["baseline"][0]) < 0.2        # 1 N ripple averaged to under 0.2 N


def test_single_noisy_wrench_sample_is_not_a_fault():
    tb = FakeTimebase()
    port = MockPort(tb)
    port.open_receive()
    ex = Executor(port, ExecConfig(mode="air"), timebase=tb)
    ex.open_control()
    ex.preflight()
    real_read = port.read_state
    spikes = {"n": 0}

    def noisy():
        st = real_read()
        spikes["n"] += 1
        if spikes["n"] == 40:                       # exactly one sample far over the limit
            return type(st)(**{**st.__dict__, "wrench": (0.0, 30.0, 0.0, 0.0, 0.0, 0.0)})
        return st

    port.read_state = noisy
    report = ex.move(g.lifted(g.pose_at(0.6)), 0.12, 1.2, "air bow with one noisy sample", force_limit=3.0)
    assert report["interrupted"] is None and ex.faulted is None and spikes["n"] > 40


def _diag_with(monkeypatch, tmp_path, **mock_kwargs):
    import conductor_cello.__main__ as cli

    class Port(MockPort):
        def __init__(self, tb, start_pose=None, **kw):
            kw.update(mock_kwargs)
            super().__init__(tb, start_pose=kw.pop("pose", start_pose), **kw)

    monkeypatch.setattr(cli, "MockPort", Port)
    monkeypatch.setattr(cli, "RUNS", tmp_path)
    return cli.main(["diag", "--mock"])


def test_diag_separates_the_controller_check_from_positioning(monkeypatch, capsys, tmp_path):
    parked = (0.516487, 0.557252, 0.638974, 1.223604, 1.498544, 2.100512)
    q = (-2.502891604100, -0.890259460812, 0.795774284993, -3.147715707819, -1.517830197011, -2.640615288411)
    assert _diag_with(monkeypatch, tmp_path, pose=parked, q=q) == 0
    out = capsys.readouterr().out
    assert "Controller runs the control program: YES" in out and "start region" in out
    assert "move the arm by hand" in out


def test_diag_sets_the_tool_settings_and_then_judges_the_real_flange(monkeypatch, capsys, tmp_path):
    # Pendant installation active, arm physically above the middle of the bow.
    reported = g.tcp_from_flange(g.lifted(g.pose_at(0.5)), LIVE_OFFSET)
    assert _diag_with(monkeypatch, tmp_path, pose=reported, tcp_offset=LIVE_OFFSET, payload_kg=0.26,
                      payload_cog=(0.05, -0.008, 0.024)) == 0
    out = capsys.readouterr().out
    assert "Controller runs the control program: YES" in out and "tool settings set for this session" in out
    assert "flange moved 0.00 mm" in out and "Ready for a first move" in out


def test_diag_passes_when_everything_is_in_place(monkeypatch, capsys, tmp_path):
    assert _diag_with(monkeypatch, tmp_path) == 0
    assert "Ready for a first move" in capsys.readouterr().out


# ── a state read on the robot ────────────────────────────────────────

LIVE_TCP = (0.4693, 0.1793, 0.192, -1.4958, -2.0068, 1.2088)
LIVE_OFFSET = (0.02821, -0.09611, -0.09969, 0.0, 0.0, 0.0)
LIVE_Q = tuple(math.radians(x) for x in (-177.13, -54.92, 107.31, -90.33, -112.12, -182.62))


def test_flange_is_recovered_from_a_reported_tool_pose():
    flange = g.flange_from_tcp(LIVE_TCP, LIVE_OFFSET)
    # forward kinematics of the logged joints gives [0.4668, 0.1193, 0.0642]
    assert flange[:3] == pytest.approx((0.4668, 0.1193, 0.0642), abs=0.0006)
    u, lift, lateral = g.decompose(flange)
    assert u == pytest.approx(1.203, abs=0.003) and lift == pytest.approx(-0.179, abs=0.002)
    assert g.flange_from_tcp(g.FSAFE, g.VERIFIED_TCP) == g.FSAFE
    turned = g.flange_from_tcp(g.FSAFE, (0.0, 0.0, 0.1, 0.0, 0.0, 0.3))
    assert g.distance(turned, g.FSAFE) == pytest.approx(0.1, abs=1e-9)
    assert math.isclose(g.rotation_angle(turned[3:], g.FSAFE[3:]), 0.3, abs_tol=1e-9)


def test_preflight_refuses_the_installation_found_on_2026_11_10():
    ex, port = _preflight(dict(start_pose=LIVE_TCP, q=LIVE_Q, tcp_offset=LIVE_OFFSET, payload_kg=0.26,
                               payload_cog=(0.05, -0.008, 0.024)))
    with pytest.raises(Refusal) as info:
        ex.preflight()
    text = str(info.value)
    assert "TCP offset" in text and "payload 0.26 kg" in text and "outside the start region" in text
    assert "u 1.203" in text                              # judged on the flange, not on the tool point
    assert [c for c in port.calls if c[0] == "move_l"] == []


def test_preflight_accepts_the_payload_the_paper_runs_really_had():
    for cog in (g.VERIFIED_PAYLOAD_COG, g.AS_RUN_PAYLOAD_COG):
        ex, port = _preflight(dict(payload_cog=cog))
        ex.preflight()
    ex, port = _preflight(dict(payload_cog=(0.05, -0.008, 0.024)))
    with pytest.raises(Refusal):
        ex.preflight()


def _pendant_state_port(cli, monkeypatch, tmp_path, flange=None, **extra):
    made = {}
    reported = LIVE_TCP if flange is None else g.tcp_from_flange(flange, LIVE_OFFSET)

    working = tuple(math.radians(x) for x in (-141.4, -40.7, 63.8, -54.7, -104.9, -157.0))

    def joints(flange_pose):
        # the mock has no kinematics: working joints near the taught line, the logged ones elsewhere
        u, lift, lateral = g.decompose(flange_pose)
        return working if (lateral < 0.02 and -0.01 < lift < 0.09) else LIVE_Q

    class Port(MockPort):
        def __init__(self, tb, start_pose=None, **kw):
            super().__init__(tb, start_pose=reported, q=None if flange is not None else LIVE_Q, tcp_offset=LIVE_OFFSET,
                             payload_kg=0.26, payload_cog=(0.05, -0.008, 0.024),
                             q_fn=None if flange is not None else joints)
            for name, at in extra.items():
                self.faults[name] = tb.now() + at
            made["port"] = self

    monkeypatch.setattr(cli, "MockPort", Port)
    monkeypatch.setattr(cli, "RUNS", tmp_path)
    return made


def test_install_command_sets_the_two_values_and_moves_nothing(monkeypatch, capsys, tmp_path):
    import conductor_cello.__main__ as cli
    made = _pendant_state_port(cli, monkeypatch, tmp_path)
    assert cli.main(["install", "--mock"]) == 0
    port = made["port"]
    assert port.tcp_offset() == g.VERIFIED_TCP and port.payload() == (0.1, g.AS_RUN_PAYLOAD_COG)
    assert [c[0] for c in port.calls if c[0] in ("move_l", "stop_l", "zero_ft")] == []
    out = capsys.readouterr().out
    assert "INSTALL OK" in out and "flange moved 0.00 mm" in out
    # the physical flange is where it was; only the reported pose changed
    assert g.distance(port.pose, g.flange_from_tcp(LIVE_TCP, LIVE_OFFSET)) < 1e-9


def test_every_session_sets_the_tool_settings_itself(monkeypatch, capsys, tmp_path):
    import conductor_cello.__main__ as cli
    made = _pendant_state_port(cli, monkeypatch, tmp_path, flange=g.lifted(g.pose_at(0.5)))
    code = cli.main(["play", "--mock", "--source", "metronome", "--song", "twinkle", "--bpm", "240", "--bars", "2",
                     "--mode", "contact", "--approach-speed", "0.25", "--setdown-speed", "0.25"])
    assert code == 0
    port = made["port"]
    installs = [c for c in port.calls if c[0] == "apply_installation"]
    first_move = next(c for c in port.calls if c[0] == "move_l")
    assert len(installs) == 1 and installs[0][1] < first_move[1]            # once, before any motion
    assert port.tcp_offset() == g.VERIFIED_TCP and port.payload()[0] == 0.1
    out = capsys.readouterr().out
    assert "tool settings set for this session" in out and "preflight passed" in out


def test_session_does_not_write_when_the_settings_are_already_right(monkeypatch, capsys, tmp_path):
    import conductor_cello.__main__ as cli
    monkeypatch.setattr(cli, "RUNS", tmp_path)
    assert cli.main(["bow", "--mock", "--mode", "air", "--u-from", "0.5", "--u-to", "0.55", "--speed", "0.12"]) == 0
    out = capsys.readouterr().out
    assert "tool settings already right" in out and "tool settings set for this session" not in out


def test_unconfirmed_tool_settings_refuse_before_any_motion(monkeypatch, capsys, tmp_path):
    import conductor_cello.__main__ as cli
    made = _pendant_state_port(cli, monkeypatch, tmp_path, flange=g.lifted(g.pose_at(0.5)), install_ignored=0.0)
    assert cli.main(["bow", "--mock", "--mode", "air"]) == 2
    assert [c for c in made["port"].calls if c[0] == "move_l"] == []
    assert "read-back does not confirm" in capsys.readouterr().out


def test_tool_point_conversion_round_trip():
    for offset in (LIVE_OFFSET, (0.01, 0.02, -0.03, 0.1, -0.2, 0.3)):
        back = g.flange_from_tcp(g.tcp_from_flange(g.FSAFE, offset), offset)
        assert g.distance(back, g.FSAFE) < 1e-12 and g.rotation_angle(back[3:], g.FSAFE[3:]) < 1e-6


def test_install_refuses_the_real_robot_without_go(monkeypatch, capsys, tmp_path):
    import conductor_cello.__main__ as cli

    def boom(self):
        raise AssertionError("the robot must not be contacted without --go")

    monkeypatch.setattr(cli.RealRtdePort, "open_receive", boom)
    monkeypatch.setattr(cli, "RUNS", tmp_path)
    assert cli.main(["install"]) == 2


# ── homing from a pose outside the start region ──────────────────────

LIVE_FLANGE = g.flange_from_tcp(LIVE_TCP, LIVE_OFFSET)


def test_home_plan_goes_up_first_and_ends_on_the_clear_line():
    from conductor_cello import homing
    assert homing.check_start(LIVE_FLANGE, LIVE_Q) == []
    steps = homing.plan_home(LIVE_FLANGE)
    assert [s.name for s in steps] == ["nudge", "lift", "cross", "down"]
    nudge, lift, cross, down = steps
    # the first two steps are pure +z with the orientation unchanged
    for step in (nudge, lift):
        assert step.target[:2] == pytest.approx(LIVE_FLANGE[:2], abs=1e-12) and step.target[3:] == LIVE_FLANGE[3:]
    assert nudge.target[2] == pytest.approx(LIVE_FLANGE[2] + 0.010) and nudge.speed == 0.02
    assert lift.target[2] == 0.40 and cross.target[2] == 0.40
    assert down.target == g.bow_pose(0.5, lift=g.LIFT_M)
    assert cross.target[3:] == down.target[3:]                       # the turn happens at height, not on the way down
    u, lift_m, lateral = g.decompose(down.target)
    assert u == pytest.approx(0.5) and lift_m == pytest.approx(g.LIFT_M) and lateral < 1e-12
    text = " ".join(homing.describe(steps, LIVE_FLANGE))
    assert "straight up" in text and "turning 22 deg" in text and "inside the start region" in text


def test_home_refuses_far_or_twisted_starts():
    from conductor_cello import homing
    parked_0109 = (0.516487, 0.557252, 0.638974, 1.223604, 1.498544, 2.100512)       # 0.64 m up, 153 deg turned
    q_0109 = (-2.502891604100, -0.890259460812, 0.795774284993, -3.147715707819, -1.517830197011, -2.640615288411)
    reasons = " ".join(homing.check_start(parked_0109, q_0109))
    assert "outside the homing box" in reasons and "turned" in reasons and "working configuration" in reasons
    with pytest.raises(ValueError):
        homing.plan_home(LIVE_FLANGE, travel_z=0.15)
    with pytest.raises(ValueError):
        homing.plan_home(LIVE_FLANGE, speed=0.5)


def test_home_command_from_the_live_pose(monkeypatch, capsys, tmp_path):
    import conductor_cello.__main__ as cli
    made = _pendant_state_port(cli, monkeypatch, tmp_path)
    assert cli.main(["home", "--mock", "--speed", "0.10"]) == 0
    port = made["port"]
    names = [c[0] for c in port.calls]
    moves = [c for c in port.calls if c[0] == "move_l"]
    assert names.index("apply_installation") < names.index("zero_ft") < names.index("move_l")
    assert len(moves) == 4 and moves[0][3] == 0.02                    # nudge first, slowly
    assert all(b[2][2] >= a[2][2] - 1e-9 for a, b in zip(moves[:2], moves[1:3]))      # never down before the crossing
    u, lift_m, lateral = g.decompose(port.pose)
    assert u == pytest.approx(0.5, abs=1e-6) and lift_m == pytest.approx(g.LIFT_M, abs=1e-6)
    out = capsys.readouterr().out
    assert "It goes UP first" in out and "HOME OK" in out and "controller confirms" in out


@pytest.mark.parametrize("fault,needle", [("pose_unreachable", "not reachable"), ("ik_flips", "not the same arm configuration")])
def test_home_refuses_when_the_controller_disagrees(monkeypatch, capsys, tmp_path, fault, needle):
    import conductor_cello.__main__ as cli
    made = _pendant_state_port(cli, monkeypatch, tmp_path, **{fault: 0.0})
    assert cli.main(["home", "--mock"]) == 2
    assert [c for c in made["port"].calls if c[0] == "move_l"] == []
    assert needle in capsys.readouterr().out


def test_home_stops_and_holds_on_unexpected_contact(monkeypatch, capsys, tmp_path):
    import conductor_cello.__main__ as cli
    made = _pendant_state_port(cli, monkeypatch, tmp_path, force_spike=4.0)
    assert cli.main(["home", "--mock"]) == 3
    port = made["port"]
    calls = [c[0] for c in port.calls]
    assert "stop_l" in calls
    t_stop = next(c[1] for c in port.calls if c[0] == "stop_l")
    assert [c for c in port.calls if c[0] == "move_l" and c[1] > t_stop] == []
    assert "SAFETY FAULT force_limit" in capsys.readouterr().out


def test_home_refuses_the_real_robot_without_go(monkeypatch, capsys, tmp_path):
    import conductor_cello.__main__ as cli

    def boom(self):
        raise AssertionError("the robot must not be contacted without --go")

    monkeypatch.setattr(cli.RealRtdePort, "open_receive", boom)
    monkeypatch.setattr(cli, "RUNS", tmp_path)
    assert cli.main(["home"]) == 2


def test_first_move_waits_for_full_speed_scaling_after_the_program_starts():
    tb = FakeTimebase()
    port = MockPort(tb)
    port.open_receive()
    ex = Executor(port, ExecConfig(mode="air"), timebase=tb)
    t0 = tb.now()
    ex.open_control()
    opened = ex.log.of("control_opened")[0]
    assert opened["settled"] and opened["speed_scaling"] == 1.0
    assert tb.now() - t0 >= max(ex.cfg.control_settle_s, port.SCALING_RAMP_S)
    ex.preflight()
    report = ex.move(g.lifted(g.pose_at(0.6)), 0.12, 1.2, "first move of a session", force_limit=3.0)
    assert report["min_speed_scaling"] == 1.0 and abs(report["late_s"]) < 0.01


def test_control_start_goes_on_after_the_timeout_if_scaling_never_settles():
    tb = FakeTimebase()
    port = MockPort(tb)
    port.SCALING_RAMP_S = 1e9                       # scaling stays near zero forever
    port.open_receive()
    ex = Executor(port, ExecConfig(mode="air"), timebase=tb)
    t0 = tb.now()
    ex.open_control()
    opened = ex.log.of("control_opened")[0]
    assert not opened["settled"] and tb.now() - t0 == pytest.approx(ex.cfg.control_settle_timeout_s, abs=0.01)


# ── force limits are changes from a reading taken at rest ────────────

OFF_ZERO = (0.9, -2.2, -0.1, -0.59, -0.03, 0.52)            # raw free-air reading after the homing run


def test_approach_move_is_not_judged_against_the_raw_sensor_value(monkeypatch, capsys, tmp_path):
    import conductor_cello.__main__ as cli

    class Port(MockPort):
        def __init__(self, tb, start_pose=None, **kw):
            super().__init__(tb, start_pose=start_pose, wrench_offset=OFF_ZERO, **kw)

    monkeypatch.setattr(cli, "MockPort", Port)
    monkeypatch.setattr(cli, "RUNS", tmp_path)
    code = cli.main(["bow", "--mock", "--mode", "air", "--u-from", "0.3", "--u-to", "0.7", "--speed", "0.12",
                     "--repeat", "4", "--approach-speed", "0.25"])
    out = capsys.readouterr().out
    assert code == 0 and "SAFETY FAULT" not in out and out.count("done  bow") == 4


def test_no_move_ever_runs_without_a_force_reference():
    tb = FakeTimebase()
    port = MockPort(tb, wrench_offset=OFF_ZERO)
    port.open_receive()
    ex = Executor(port, ExecConfig(mode="air"), timebase=tb)
    ex.open_control()
    ex.preflight()
    assert ex.log.of("wrench_baseline") == []
    ex.move(g.lifted(g.pose_at(0.55)), 0.10, 1.2, "first move, nothing prepared", force_limit=3.0)
    assert len(ex.log.of("wrench_baseline")) == 1 and ex.faulted is None


def test_session_starting_on_the_string_lifts_then_references_free_air():
    # bow resting on the string at the start, and a sensor whose zero is off
    s, ex, port, frames, clock = session(port_kwargs=dict(start_pose=g.pose_at(0.5), wrench_offset=OFF_ZERO))
    assert s["reason"] == "score_complete" and s["safety_fault"] is None and len(s["bows"]) == 12
    assert s["contact_force_n"] == pytest.approx(MockPort.CONTACT_FORCE_N, abs=0.05)     # measured as a change, not raw
    kinds = [e["kind"] for e in ex.log.events]
    first_move = kinds.index("move_command")
    assert "wrench_baseline" in kinds[:first_move]


RAW_AT_REST_DRIFTED = (-7.63, -7.20, -10.88, -0.82, -0.23, 0.47)   # raw reading at rest in the air: 15.1 N


def _conductor_connects_late(delay_s=5.0):
    grid = SCORES["arirang"]
    return [(t + delay_s, p) for t, p in metronome_packets(bpm=72, meter=3, total_beats=grid.total_beats, ready_s=6.0)]


def test_waiting_for_the_conductor_is_not_judged_against_the_raw_sensor_value():
    # Live source without --bpm: the session waits for the Conductor's first packet before anything
    # moves. By the evening the raw reading at rest was above the 10 N limit, and the first live
    # session on the robot ended 0.1 s after preflight with "15.04 N > 10.00 N".
    s, ex, port, frames, clock = session(mode="air", song="arirang", packets=_conductor_connects_late(),
                                         port_kwargs=dict(wrench_offset=RAW_AT_REST_DRIFTED))
    assert s["reason"] == "score_complete" and s["safety_fault"] is None and len(s["bows"]) == 16
    reference, first_packet = ex.log.of("wrench_baseline")[0]["t"], ex.log.of("preflight")[0]["t"] + 5.0
    assert reference < first_packet - 4.0                           # taken at rest, before the wait began


def test_waiting_for_the_conductor_still_faults_on_a_real_force_change():
    s, ex, port, frames, clock = session(mode="air", song="arirang", packets=_conductor_connects_late(),
                                         faults={"force_spike": 2.0},
                                         port_kwargs=dict(wrench_offset=RAW_AT_REST_DRIFTED))
    assert s["reason"] == "safety_fault:force_limit" and s["bows"] == []
    assert s["safety_fault"]["detail"] == "20.00 N > 10.00 N"       # the change, not the 30 N raw reading
    assert [c for c in port.calls if c[0] == "move_l"] == []


@pytest.mark.parametrize("unknown_preload", [True, False])
def test_lift_from_a_hold_at_the_force_limit(monkeypatch, unknown_preload):
    # bow held on the string after a set-down fault: 12 N and 3 Nm of load nobody measured
    monkeypatch.setattr(MockPort, "CONTACT_FORCE_N", 12.0)
    monkeypatch.setattr(MockPort, "CONTACT_TORQUE_NM", 3.0)
    tb = FakeTimebase()
    port = MockPort(tb, start_pose=g.pose_at(0.5), wrench_offset=OFF_ZERO)
    port.open_receive()
    ex = Executor(port, ExecConfig(mode="contact"), timebase=tb)
    ex.open_control()
    ex.preflight()
    ex.capture_baseline()                       # taken under load: letting go will read as a 12 N change
    if unknown_preload:
        report = ex.lift_here(unknown_preload=True)
        _, lift, _ = g.decompose(port.pose)
        assert report is not None and ex.faulted is None and lift == pytest.approx(ex.cfg.air_lift, abs=1e-4)
        assert ex.log.of("lift_release_limits")[0]["force_limit"] == 2 * ex.cfg.force_limit_contact
    else:
        with pytest.raises(SafetyFault) as info:
            ex.lift_here()
        assert info.value.kind == "force_limit"  # the ordinary limits read the release itself as a fault


def test_shortened_metronome_run_never_starts_an_extra_bow(monkeypatch, capsys, tmp_path):
    # at 66 BPM the end packet used to arrive just after bow 5 had been commanded: a start and a stop within 70 ms
    import json
    import conductor_cello.__main__ as cli
    monkeypatch.setattr(cli, "RUNS", tmp_path)
    code = cli.main(["play", "--mock", "--source", "metronome", "--song", "twinkle", "--bpm", "66", "--bars", "4",
                     "--mode", "contact", "--contact-band", "0.8", "6.5"])
    assert code == 0
    folder = next(tmp_path.iterdir())
    summary = json.loads((folder / "summary.json").read_text())
    assert summary["reason"] == "score_complete" and len(summary["bows"]) == 4 and summary["lifted"]
    rows = [json.loads(line) for line in (folder / "events.jsonl").read_text().splitlines()]
    assert [r for r in rows if r["kind"] == "stop"] == []
    assert rows[0]["config"]["contact_band"] == [0.8, 6.5]                       # the option reached the session
    assert [r["band"] for r in rows if r["kind"] == "set_down"] == [[0.8, 6.5]]
    bows = [r for r in rows if r["kind"] == "move_done" and r["what"].startswith("bow ")]
    assert len(bows) == 4 and all(r["peak_force_n"] == pytest.approx(MockPort.CONTACT_FORCE_N, abs=0.05) for r in bows)
    assert all(r["force_limit_n"] == 10.0 and r["torque_limit_nm"] == 1.8 for r in bows)
    out = capsys.readouterr().out
    assert "largest force change 3.60 N of the 10 N limit" in out and "contact force at set-down: 3.60 N" in out
    assert cli.main(["play", "--mock", "--source", "metronome", "--song", "twinkle", "--bpm", "66", "--bars", "0"]) == 2


def test_contact_band_must_be_distinguishable_from_free_air():
    with pytest.raises(ValueError):
        ExecConfig(mode="contact", contact_band=(0.1, 6.5)).validate()
    with pytest.raises(ValueError):
        ExecConfig(mode="contact", contact_band=(1.5, 9.5)).validate()
    assert ExecConfig(mode="contact", contact_band=(0.8, 6.5)).validate().contact_band == (0.8, 6.5)


def test_depth_option_presses_past_the_taught_line_within_the_paper_envelope(monkeypatch, capsys, tmp_path):
    import json
    import conductor_cello.__main__ as cli
    monkeypatch.setattr(cli, "RUNS", tmp_path)
    code = cli.main(["bow", "--mock", "--mode", "contact", "--u-from", "0.3", "--u-to", "0.4", "--depth-mm", "2",
                     "--contact-band", "0.7", "6.5"])
    assert code == 0
    folder = next(tmp_path.iterdir())
    rows = [json.loads(line) for line in (folder / "events.jsonl").read_text().splitlines()]
    assert rows[0]["config"]["depth"] == pytest.approx(0.002)
    landing = [r for r in rows if r["kind"] == "move_command" and r["what"].startswith("set down")][0]
    _, lift, lateral = g.decompose(tuple(landing["target"]))
    assert lift == pytest.approx(-0.002, abs=1e-6) and lateral < 1e-6            # straight down, 2 mm past the line
    bow = [r for r in rows if r["kind"] == "move_command" and r["what"].startswith("bow 0")][0]
    assert g.decompose(tuple(bow["target"]))[1] == pytest.approx(-0.002, abs=1e-6)   # the bow stays at that depth
    assert [r["depth_mm"] for r in rows if r["kind"] == "set_down"] == [pytest.approx(2.0)]
    out = capsys.readouterr().out
    assert "+2.0 mm past the taught line" in out and "reference about 0.45 Nm at this place" in out
    # outside the envelope: refused before anything is opened, no run folder, no traceback
    before = len(list(tmp_path.iterdir()))
    assert cli.main(["bow", "--mock", "--mode", "contact", "--depth-mm", "3"]) == 2
    assert cli.main(["bow", "--mock", "--mode", "contact", "--contact-band", "0.1", "6.5"]) == 2
    assert len(list(tmp_path.iterdir())) == before
    assert "REFUSED" in capsys.readouterr().out


def test_after_the_score_the_receiver_stays_for_the_conductors_stop():
    # if the receiver closed when the score ended at beat 48, a STOP from the Conductor at beat 51
    # would never be acknowledged
    import conductor_cello.__main__ as cli
    tb = FakeTimebase()
    clock = ConductorClock()
    assert cli._await_conductor_stop(clock, timeout_s=1.0, tb=tb) == "timeout"
    t0 = tb.now()
    polls = {"n": 0}

    def later():                                   # the STOP arrives while we wait
        polls["n"] += 1
        if polls["n"] == 5:
            now = tb.now()
            clock.ingest(dict(version=1, session_id="s", clock_domain="d", sequence=9, kind="STOP", state="stopped",
                              source_monotonic_ns=int(now * 1e9), beat_position=51.3, tempo_bpm=66.0, meter=[4, 4],
                              quality="predicted"), now)
        return False
    assert cli._await_conductor_stop(clock, timeout_s=5.0, tb=tb, give_up=later) == "stop_acknowledged"
    assert tb.now() - t0 < 0.5
    gone = ConductorClock()
    gone.close_transport("conductor_closed_connection")
    assert cli._await_conductor_stop(gone, timeout_s=5.0, tb=tb) == "conductor_gone"
    assert cli._await_conductor_stop(ConductorClock(), timeout_s=5.0, tb=tb, give_up=lambda: True) == "operator"


def test_the_time_limit_of_a_move_allows_for_the_turn_it_makes():
    # a homing step of 63 mm with a 33 degree turn needs more time than its translation alone
    import math
    from conductor_cello.executor import ExecConfig, move_time_limit
    floor = ExecConfig().turn_rate_floor
    assert move_time_limit(1.306, 0.0, floor) == pytest.approx(2.63, abs=0.01)             # no turn: as before
    assert move_time_limit(1.306, math.radians(33), floor) > 6.0                           # that step now has time
    assert move_time_limit(2.5, math.radians(1), floor) == pytest.approx(2.5 * 1.25 + 1.0)  # a bow is not affected
