"""Transport for wire v1: loopback TCP JSON Lines, plus in-process sources.

Roles follow the existing contract: the CELLO side listens (documented port
8766), the Conductor's Publisher connects and sends one JSON object per line.
The cello answers a STOP with the v1 ACK and may send the two frames the
Conductor's completion authority already understands:
    score_playback_complete   and   executor_failure
Nothing else is sent unless asked for, because an unknown frame is treated as
a contract failure by the Conductor's current receiver.
"""
from __future__ import annotations

import json
import socket
import threading
import time

from .clock import ConductorClock

DEFAULT_PORT = 8766
MAX_LINE = 65536


class WireServer:
    """Accepts ONE Conductor connection and feeds a ConductorClock. Reconnection never resumes."""

    def __init__(self, clock: ConductorClock, *, host="127.0.0.1", port=DEFAULT_PORT, log=None,
                 now=time.perf_counter):
        self.clock, self.host, self.port, self.log, self.now = clock, host, port, log, now
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind((host, port))
        self._listener.listen(1)
        self.port = self._listener.getsockname()[1]
        self._client = None
        self._send_lock = threading.Lock()
        self._closing = threading.Event()
        self.acked = set()
        self.thread = threading.Thread(target=self._run, name="conductor-wire", daemon=True)

    def start(self):
        self.thread.start()
        return self

    def send(self, obj: dict) -> bool:
        raw = (json.dumps(obj, allow_nan=False, separators=(",", ":")) + "\n").encode()
        with self._send_lock:
            if self._client is None:
                return False
            try:
                self._client.sendall(raw)
                return True
            except OSError:
                return False

    def _ack(self, msg: dict):
        seq = msg.get("sequence")
        if seq in self.acked:
            return
        self.acked.add(seq)
        ack = dict(version=1, kind="ACK", session_id=msg.get("session_id"),
                   clock_domain=msg.get("clock_domain"), ack_sequence=seq, status="stopped_latched")
        if msg.get("playback_id") is not None:
            ack["playback_id"] = msg["playback_id"]
        self.send(ack)      # software latch only: says nothing about the arm

    def _run(self):
        try:
            self._listener.settimeout(0.2)
            while not self._closing.is_set() and self._client is None:
                try:
                    client, _ = self._listener.accept()
                except socket.timeout:
                    continue
                client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                client.settimeout(0.2)
                with self._send_lock:
                    self._client = client
                if self.log:
                    self.log("wire_connected", port=self.port)
            buf = b""
            while not self._closing.is_set() and self._client is not None:
                try:
                    chunk = self._client.recv(4096)
                except socket.timeout:
                    continue
                if not chunk:
                    self.clock.close_transport("conductor_closed_connection")
                    break
                buf += chunk
                if len(buf) > MAX_LINE and b"\n" not in buf:
                    self.clock.close_transport("oversized_frame")
                    break
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if not line.strip():
                        continue
                    try:
                        msg = json.loads(line)
                    except ValueError:
                        self.clock.close_transport("invalid_json_frame")
                        return
                    verdict = self.clock.ingest(msg, self.now())
                    if verdict == "stop":
                        self._ack(msg)
        except OSError as exc:
            self.clock.close_transport("socket_error:" + repr(exc))
        finally:
            if self.log:
                self.log("wire_reader_exit", closed=self.clock.transport_closed)

    def close(self):
        self._closing.set()
        with self._send_lock:
            client, self._client = self._client, None
        for s in (client, self._listener):
            try:
                if s is not None:
                    s.close()
            except OSError:
                pass
        if self.thread.is_alive():
            self.thread.join(1.0)


# ── in-process sources (no socket): fixed tempo and recorded streams ──────

def metronome_packets(*, bpm: float, meter: int, total_beats: float, ready_s: float = 2.0,
                      rate_hz: float = 50.0, session_id="local-metronome", clock_domain="local-perf-counter",
                      tail_s: float = 0.5, end_state: str = "ended"):
    """Yield (t_rel_s, packet_without_timestamp) for a perfectly steady clock.

    READY for `ready_s`, RUNNING from beat 0 to `total_beats`, then STOP
    (state `end_state`). The caller stamps source_monotonic_ns at send time.
    """
    period = 1.0 / rate_hz
    seq = 0
    run_s = total_beats * 60.0 / bpm
    t = 0.0
    while t < ready_s + run_s + tail_s:
        seq += 1
        if t < ready_s:
            state, kind, beat, quality = "ready", "CLOCK", 0.0, "unreliable"
        elif t < ready_s + run_s:
            state, kind, beat, quality = "running", "CLOCK", (t - ready_s) * bpm / 60.0, "tracked"
        else:
            state, kind, beat, quality = end_state, "STOP", total_beats, "predicted"
        yield t, dict(version=1, session_id=session_id, clock_domain=clock_domain, sequence=seq,
                      kind=kind, state=state, beat_position=beat, tempo_bpm=float(bpm),
                      estimated_tempo_bpm=float(bpm), meter=[meter, 4], quality=quality)
        t += period


def load_recorded_stream(path) -> list:
    """Read a recorded Conductor stream: JSON Lines (stream.jsonl) or a JSON list (wire.json).

    Returns [(t_rel_s, packet)], t_rel measured from the first record's
    source_monotonic_ns. Records keep every original field.
    """
    text = open(path, encoding="utf-8").read().strip()
    if text.startswith("["):
        records = json.loads(text)
    else:
        records = [json.loads(line) for line in text.splitlines() if line.strip()]
    records = [r for r in records if isinstance(r, dict) and type(r.get("source_monotonic_ns")) is int]
    if not records:
        raise ValueError("no usable records in " + str(path))
    t0 = records[0]["source_monotonic_ns"]
    return [((r["source_monotonic_ns"] - t0) / 1e9, r) for r in records]


class ScriptedSource:
    """Feeds (t_rel, packet) pairs into a clock on the executor's own time base.

    step(now) delivers everything due; packets are re-stamped so that
    source_monotonic_ns lies on the local clock (original spacing preserved).
    Used with real time (a feeder thread) and with the fake time base in tests.
    """

    def __init__(self, clock: ConductorClock, packets, *, start_s: float, on_stop=None):
        self.clock, self.packets, self.start_s, self.on_stop = clock, list(packets), start_s, on_stop
        self.i = 0
        self.stops = []

    def step(self, now_s: float) -> None:
        while self.i < len(self.packets) and self.start_s + self.packets[self.i][0] <= now_s:
            t_rel, pkt = self.packets[self.i]
            self.i += 1
            msg = dict(pkt)
            msg["source_monotonic_ns"] = int(round((self.start_s + t_rel) * 1e9))
            if self.clock.ingest(msg, now_s) == "stop":
                self.stops.append(msg)
                if self.on_stop:
                    self.on_stop(msg)
        if self.i >= len(self.packets) and not self.stops:
            self.clock.close_transport("recorded_stream_ended_without_stop")

    @property
    def exhausted(self) -> bool:
        return self.i >= len(self.packets)
