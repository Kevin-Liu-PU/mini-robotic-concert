"""Glockenspiel that follows the Conductor.

    python -m conductor_glock play --song arirang --dry-run
    python -m conductor_glock play --song arirang --forward 8767 --dry-run     # duet: pass the stream on to the cello
    python -m conductor_glock play --song arirang --go [--serial /dev/cu.usbmodemXXXX]
    python -m conductor_glock ports                                             # list the controller's serial port
    python -m conductor_glock check --go                                        # open, ask READY, all-off, close. No strike.

It listens where the Conductor sends (127.0.0.1:8766) and strikes each note of the part when the
Conductor's beat position reaches it. With --forward PORT every line is passed on unchanged to the
next receiver (start the cello's receiver first, with `--port PORT`). Without --go the real
controller is never opened; --dry-run only records what would be struck.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from conductor_cello.clock import ConductorClock

from . import __version__
from .player import Player
from .ports import DryPort, ProtonPort
from .relay import Relay
from .score import NOTES, SONG_FILES, load

RUNS = Path(__file__).resolve().parent / "runs"
DEFAULT_PORT = 8766


def _say(text=""):
    print(text, flush=True)


class Log:
    def __init__(self, folder: Path):
        folder.mkdir(parents=True, exist_ok=False)
        self.folder = folder
        self._file = open(folder / "events.jsonl", "w", buffering=1)

    def event(self, kind, **fields):
        self._file.write(json.dumps(dict(kind=kind, t=time.perf_counter(), **fields), default=str) + "\n")

    def close(self):
        self._file.close()


def _open_port(args):
    if args.dry_run:
        return DryPort()
    if not args.go:
        raise SystemExit("REFUSED: add --dry-run to only record the strikes, or --go to open the real controller "
                         "(it was not opened).")
    return ProtonPort(args.serial)


def cmd_ports(args):
    from .ports import candidates
    found = candidates()
    _say("Proton USB serial ports found: " + (", ".join(found) if found else "none"))
    return 0


def cmd_check(args):
    if not args.go:
        raise SystemExit("REFUSED: add --go to open the real controller (it was not opened).")
    port = ProtonPort(args.serial)
    _say(f"{port.name}: answered {port.answer}. All outputs off. Nothing was struck.")
    if port.firmware != "aria8":
        _say("That is the firmware from before the Aria-Solenoid-Studio package. Its own player works with it in "
             "the same way. Not known here: how long it holds a strike, and what it does when USB is lost.")
    port.close()
    return 0


def cmd_play(args):
    score = load(args.song)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + f"-{os.getpid()}"
    folder = RUNS / f"{stamp}-play-{args.song}-{'dry' if args.dry_run else 'live'}"
    log = Log(folder)
    _say(f"run folder: {folder}")
    _say(f"song {score.name}: {len(score.notes)} strikes over {score.beats:g} beats; solenoids 1..8 = {' '.join(NOTES)}")
    port = relay = None
    hits = {"n": 0}

    def on_signal(signum, frame):
        hits["n"] += 1
        _say("\n[operator] Ctrl-C: stopping")
    signal.signal(signal.SIGINT, on_signal)
    try:
        port = _open_port(args)
        _say(f"controller: {port.name}")
        log.event("start", version=__version__, song=score.name, dry_run=args.dry_run, controller=port.name,
                  listen=args.listen, forward=args.forward, lead_ms=args.lead_ms)
        clock = ConductorClock()
        try:
            relay = Relay(clock, listen_port=args.listen, forward_port=args.forward, log=log.event).start()
        except OSError as exc:
            where = f"the next receiver on 127.0.0.1:{args.forward}" if args.forward else f"port {args.listen}"
            raise SystemExit(f"REFUSED: could not set up {where} ({exc}). "
                             + ("Start the cello's receiver first, with --port %d." % args.forward if args.forward else ""))
        if args.forward:
            _say(f"passing every line on to the next receiver on 127.0.0.1:{args.forward}")
        _say(f"listening for the Conductor on 127.0.0.1:{relay.port} (wire v1, JSON Lines)")
        player = Player(score, lead_s=args.lead_ms / 1000.0)
        status, reason, t_wait, finished_at, armed = "waiting", None, time.perf_counter(), None, False
        fault = None
        while True:
            now = time.perf_counter()
            if hits["n"]:
                reason = "operator_stop"
                break
            due, status = player.step(clock.view(), now)
            for note, phase, late in due:
                if fault is None:
                    try:
                        port.strike(note.channel)
                    except Exception as exc:          # USB gone: go silent, keep passing the stream on
                        fault = repr(exc)
                        log.event("controller_fault", error=fault)
                        _say(f"\nCONTROLLER FAULT: {fault}. No more strikes; the stream is still passed on.")
                log.event("strike", beat=note.beat, note=note.name, channel=note.channel, phase=phase, late_s=late,
                          sent=fault is None)
                if not args.quiet:
                    _say(f"  beat {note.beat:5.1f}  {note.name}  solenoid {note.channel}   {late * 1000:+5.0f} ms")
            if status == "waiting" and not armed and clock.view()[0] is not None:
                armed = True
                _say("\nConductor connected. Waiting for the piece to start.")
            if status == "finished":
                if finished_at is None:
                    finished_at = now
                    _say(f"\nAll notes played. Staying connected up to {args.stop_wait:g} s for the Conductor's STOP.")
                elif now - finished_at > args.stop_wait:
                    reason = "finished_no_stop_seen"
                    break
            elif status not in ("waiting", "playing", "holding"):
                reason = status
                break
            elif status == "waiting" and not armed and now - t_wait > args.wait:
                reason = "no_conductor"
                break
            time.sleep(0.002)
        try:
            port.all_off()
        except Exception:
            pass
        tally = player.tally
        summary = dict(song=score.name, reason=reason, notes=len(score.notes), struck=tally.struck,
                       dropped_late=tally.dropped_late, dropped_recovery=tally.dropped_recovery,
                       worst_late_ms=tally.worst_late_s * 1000.0, finished=finished_at is not None,
                       controller_fault=fault, forwarded_lines=relay.forwarded, returned_lines=relay.returned,
                       clock=dict(accepted=clock.accepted, ignored=dict(clock.ignored)))
        log.event("end", **summary)
        (folder / "summary.json").write_text(json.dumps(summary, indent=1) + "\n")
        _say(f"\nended: {reason}   struck {tally.struck} of {len(score.notes)}   dropped late {tally.dropped_late}   "
             f"dropped for recovery {tally.dropped_recovery}   latest strike {tally.worst_late_s * 1000:.0f} ms after its beat")
        return 0 if fault is None else 3
    finally:
        if relay is not None:
            relay.close()
        if port is not None:
            port.close()
        log.close()


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m conductor_glock", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("play")
    s.add_argument("--song", choices=sorted(SONG_FILES), required=True)
    s.add_argument("--listen", type=int, default=DEFAULT_PORT, help="loopback port the Conductor sends to")
    s.add_argument("--forward", type=int, default=None, help="pass every line on to this loopback port (the cello's receiver)")
    s.add_argument("--dry-run", action="store_true", help="record the strikes, open no hardware")
    s.add_argument("--go", action="store_true", help="really open the solenoid controller")
    s.add_argument("--serial", default=None, help="serial device of the Proton target (default: the only one found)")
    s.add_argument("--lead-ms", type=float, default=0.0, help="strike this much before the beat")
    s.add_argument("--stop-wait", type=float, default=30.0, help="after the last note, how long to wait for the Conductor's STOP")
    s.add_argument("--wait", type=float, default=300.0, help="how long to wait for the Conductor to connect")
    s.add_argument("--quiet", action="store_true", help="do not print every strike")
    s.set_defaults(fn=cmd_play)
    sub.add_parser("ports").set_defaults(fn=cmd_ports)
    c = sub.add_parser("check")
    c.add_argument("--go", action="store_true")
    c.add_argument("--serial", default=None)
    c.set_defaults(fn=cmd_check)
    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
