"""Regenerates example_input.jsonl and example_output.json (mock robot, fake time).

    python conductor_cello/examples/make_example.py

The input is a synthetic wire-v1 stream at 20 Hz: READY, then RUNNING at
66 BPM drifting to 70 BPM, one stretch of missed beats (quality "predicted"),
and a STOP in the middle of the third bar. The output is what the executor
commanded for it. Nothing here is a recording of a person or of the robot.
"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))

from conductor_cello.bowplan import SCORES            # noqa: E402
from conductor_cello.clock import ConductorClock      # noqa: E402
from conductor_cello.driver import FakeTimebase, MockPort   # noqa: E402
from conductor_cello.executor import ExecConfig, Executor   # noqa: E402
from conductor_cello.wire import ScriptedSource       # noqa: E402


def stream():
    seq, t, beat = 0, 0.0, 0.0
    out = []
    while True:
        seq += 1
        base = dict(version=1, session_id="example-session", clock_domain="shared-local-run", sequence=seq,
                    source_monotonic_ns=int(round(t * 1e9)), meter=[4, 4])
        if t < 1.0:
            out.append(dict(base, kind="CLOCK", state="ready", beat_position=0.0, tempo_bpm=66.0,
                            estimated_tempo_bpm=66.0, quality="unreliable"))
        else:
            bpm = 66.0 + min(4.0, max(0.0, beat - 4.0))          # drifts 66 -> 70 over bar two
            beat += 0.05 * bpm / 60.0
            if beat >= 10.4:
                out.append(dict(base, kind="STOP", state="stopped", beat_position=round(beat, 4), tempo_bpm=bpm,
                                estimated_tempo_bpm=bpm, quality="predicted"))
                return out
            quality = "predicted" if 5.0 <= beat < 6.5 else "tracked"
            out.append(dict(base, kind="CLOCK", state="running", beat_position=round(beat, 4),
                            tempo_bpm=round(bpm, 3), estimated_tempo_bpm=round(bpm, 3), quality=quality))
        t += 0.05


def main():
    records = stream()
    (HERE / "example_input.jsonl").write_text("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in records))
    tb = FakeTimebase(0.0)
    port = MockPort(tb)
    port.open_receive()
    ex = Executor(port, ExecConfig(mode="contact"), timebase=tb)
    clock = ConductorClock()
    ex.open_control()
    ex.preflight()
    holder = {}

    def armed():
        holder["t0"] = tb.now()
        src = ScriptedSource(clock, [(r["source_monotonic_ns"] / 1e9, r) for r in records], start_s=tb.now())
        ex.feeder = src.step

    summary = ex.run_session(clock, SCORES["twinkle"], nominal_bpm=66.0, on_armed=armed)
    t0 = holder["t0"]
    bows = [dict(index=b["index"], direction="down" if b["direction"] > 0 else "up",
                 dispatched_at_s=round(b["t_dispatch"] - t0, 3), beat_at_dispatch=round(b["phase"], 3),
                 ends_on_beat=b["boundary"], tempo_bpm=round(b["tempo_bpm"], 2), quality=b["quality"],
                 duration_s=round(b["duration_s"], 3), u_start=round(b["u_start"], 4), u_end=round(b["u_end"], 4),
                 length_mm=round(b["length_m"] * 1000, 1), speed_m_s=round(b["speed"], 4),
                 bar_line_error_beats=round(b["boundary_error_beats"], 3), interrupted=b["interrupted"])
            for b in summary["bows"]]
    stop = ex.log.of("stop")[0]
    stop_record = records[-1]
    out = dict(
        note="Mock robot on a fake clock. Times are seconds after the stream's first record.",
        armed_at_u=round(summary["u_start"], 4), contact_force_n_mock=summary["contact_force_n"],
        bows=bows, ended=summary["reason"], completed_beats=summary["completed_beats"],
        stop=dict(requested_at_s=round(stop["t_request"] - t0, 3), standstill_confirmed=stop["standstill_confirmed"],
                  standstill_after_ms=round(stop["standstill_s"] * 1000, 1),
                  travel_after_request_mm=round(stop["stop_distance_m"] * 1000, 2)),
        lifted=summary["lifted"],
        frames_sent_to_conductor=[dict(version=1, kind="ACK", session_id=stop_record["session_id"],
                                       clock_domain=stop_record["clock_domain"],
                                       ack_sequence=stop_record["sequence"], status="stopped_latched")],
    )
    (HERE / "example_output.json").write_text(json.dumps(out, indent=1) + "\n")
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
