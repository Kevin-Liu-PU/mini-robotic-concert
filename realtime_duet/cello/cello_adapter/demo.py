"""Real-time synthetic producer for integration testing, never opens camera."""
import argparse
import json
import socket
import time


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--session", required=True)
    p.add_argument("--clock-domain", required=True)
    p.add_argument("--port", type=int, default=8766)
    p.add_argument("--bpm", required=True, type=float)
    p.add_argument("--tempo-change", action="store_true", help="1.25x at beat 12; 0.8x at beat 28")
    args = p.parse_args()
    if not 38 <= args.bpm <= 190:
        p.error("demo bpm must be between 38 and 190")
    with socket.create_connection(("127.0.0.1", args.port)) as conn:
        origin = time.perf_counter_ns() / 1e9
        last = origin
        phase = 0.0
        seq = 0
        while phase < 48.1:
            now_ns = time.perf_counter_ns()
            now = now_ns / 1e9
            bpm = args.bpm
            if args.tempo_change:
                bpm *= 1.25 if 12 <= phase < 28 else (0.8 if phase >= 28 else 1)
            phase += (now - last) * bpm / 60
            last = now
            payload = dict(version=1, session_id=args.session, clock_domain=args.clock_domain,
                           sequence=seq, source_monotonic_ns=now_ns, kind="CLOCK",
                           beat_position=phase, tempo_bpm=bpm, state="running",
                           meter=[4, 4], quality="tracked")
            try:
                conn.sendall((json.dumps(payload) + "\n").encode())
            except (ConnectionError, OSError):
                break
            seq += 1
            time.sleep(0.02)


if __name__ == "__main__":
    main()
