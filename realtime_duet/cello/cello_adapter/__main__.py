"""python -m cello_adapter --session RUN --listen 8766 (mock only)."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import socket
import sys
import threading
import time
from .core import Journal, MockBackend, Scheduler, hardware_backend
from .transport import LatestInbox


def result_exit_code(reason, backend_kind="mock", shutdown_status=None):
    musical_success = reason in ("score_complete", "explicit_stop", "keyboard_interrupt")
    if backend_kind == "hardware":
        if shutdown_status != "driver_stop_returned_unverified":
            return 4  # failed/pending/incomplete/unknown driver shutdown
        return 3 if musical_success else 1  # API returned, physical standstill unverified
    return 0 if musical_success else 1


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--session", required=True)
    p.add_argument("--clock-domain", required=True)
    p.add_argument("--arm", action="store_true", help="explicitly enable MOCK scheduling; default DISARMED")
    p.add_argument("--clock-timeout-ms", type=float, default=1000)
    p.add_argument("--listen", type=int, help="localhost TCP port; otherwise stdin NDJSON")
    p.add_argument("--backend", choices=("mock", "hardware"), default="mock")
    p.add_argument("--hardware-review")
    p.add_argument("--review-sha256")
    p.add_argument("--execute-hardware", action="store_true")
    p.add_argument("--attended", action="store_true")
    p.add_argument("--journal", default="run-journal")
    p.add_argument("--log", default="bow-events.jsonl")
    args = p.parse_args(argv)
    if args.backend == "hardware":
        try:
            if not args.hardware_review or not args.review_sha256:
                raise ValueError("HARDWARE BLOCKED: reviewed configuration/hash required; see HARDWARE_BRIDGE.md")
            from .hardware import preflight
            review, limits = preflight(args.hardware_review, args.review_sha256,
                                       args.session, args.clock_domain)
            if not args.execute_hardware:
                print("Static hardware preflight passed; no connections made. Execution remains disabled.")
                return 0
            if not args.arm or not args.attended:
                raise ValueError("HARDWARE BLOCKED: explicit --arm and --attended required")
        except (RuntimeError, ValueError, OSError, KeyError, TypeError) as exc:
            p.exit(2, str(exc) + "\n")
    if not 100 <= args.clock_timeout_ms <= 10000:
        p.error("clock timeout must be in [100,10000] ms")
    messages = LatestInbox(args.session, args.clock_domain)
    done = threading.Event()
    listener = None
    stream = sys.stdin
    connection = None
    if args.listen:
        listener = socket.socket()
        listener.bind(("127.0.0.1", args.listen))

    def reader():
        try:
            while not done.is_set():
                line = stream.readline(16385)
                if not line:
                    messages.fail("disconnect")
                    return
                if len(line) > 16384:
                    messages.fail("oversized_message")
                    return
                if line.strip():
                    try:
                        payload = json.loads(line)
                    except (ValueError, TypeError):
                        messages.fail("invalid_json")
                        return
                    messages.put(payload)
        except Exception:
            messages.fail("reader_failure")
            done.set()

    shutdown_status = None
    with Path(args.log).open("x", encoding="utf-8") as log:
        def emit(event):
            line = json.dumps(event, allow_nan=False)
            log.write(line + "\n")
            log.flush()
            print(line, flush=True)
        backend = (hardware_backend(args.hardware_review, args.review_sha256,
                                    args.session, args.clock_domain, execute=True,
                                    attended=args.attended, armed=args.arm, sink=emit)
                   if args.backend == "hardware" else MockBackend(emit))
        # Only advertise the TCP listener after hardware session checks finish.
        # A producer must never spend the score while the robot is initializing.
        if listener:
            try:
                listener.listen(1)
                print(f"{args.backend.upper()} ready on 127.0.0.1:{args.listen}; session={args.session}", file=sys.stderr)
                connection, _ = listener.accept()
                stream = connection.makefile("r", encoding="utf-8")
            except BaseException:
                if args.backend == "hardware":
                    backend.close()
                listener.close()
                raise
        scheduler = Scheduler(args.session, backend, Journal(args.journal),
                              args.clock_timeout_ms / 1000, args.clock_domain, args.arm)
        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        try:
            while not scheduler.terminal:
                kind, payload = messages.take()
                if kind:
                    now = time.perf_counter_ns() / 1e9
                    if kind == "message":
                        scheduler.ingest(payload, now)
                        if scheduler.reason == "explicit_stop":
                            ack = dict(version=1, kind="ACK", session_id=args.session,
                                       ack_sequence=payload["sequence"], status="stopped_latched")
                            line = json.dumps(ack) + "\n"
                            if connection:
                                connection.sendall(line.encode())
                            else:
                                print(line, end="", flush=True)
                    else:
                        scheduler.stop(payload, now)
                if done.is_set() and not scheduler.terminal:
                    scheduler.stop("reader_failure", time.perf_counter_ns() / 1e9)
                scheduler.tick(time.perf_counter_ns() / 1e9)
                time.sleep(0.005)
        except KeyboardInterrupt:
            scheduler.stop("keyboard_interrupt", time.perf_counter_ns() / 1e9)
        except Exception:
            scheduler.stop("adapter_exception", time.perf_counter_ns() / 1e9)
            raise
        finally:
            done.set()
            if args.backend == "hardware":
                shutdown_status = backend.close()
                emit(dict(kind="hardware_shutdown", status=shutdown_status,
                          physical_standstill_verified=False,
                          exit_code=result_exit_code(scheduler.reason, "hardware", shutdown_status)))
            if connection:
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                connection.close()
            if listener:
                listener.close()
    return result_exit_code(scheduler.reason, args.backend, shutdown_status)


if __name__ == "__main__":
    raise SystemExit(main())
