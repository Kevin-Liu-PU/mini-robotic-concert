"""Conductor clock follower for the shared wire contract v1.

A packet is one JSON object:
  {"version":1,"session_id":"...","clock_domain":"...","sequence":42,
   "source_monotonic_ns":123456789000,"kind":"CLOCK","state":"running",
   "beat_position":8.25,"tempo_bpm":76.0,"meter":[4,4],"quality":"tracked"}
Optional fields used when present: estimated_tempo_bpm, phase_rate_bpm,
playback_id, score_id, score_total_beats, planned_bows, contract,
experimental, manifest_sha256, observation_age_s.

Time base: source_monotonic_ns is time.perf_counter_ns() of the Conductor
process on the SAME host. This is not a cross-host protocol.

What the follower does NOT do: it never ends a performance because the beat
position moved, the tempo changed or the quality dropped. Those are rhythm
matters decided by the Conductor and absorbed at the next bow change. Only
STOP, a closed connection or silence longer than the timeout end a run.
"""
from __future__ import annotations

import math
import threading
from dataclasses import dataclass

STATES = ("ready", "running", "stopped", "ended")
QUALITIES = ("tracked", "predicted", "unreliable")
MAX_PACKET_AGE_S = 0.5          # v1: stale CLOCK is ignored
MAX_PACKET_FUTURE_S = 0.05      # v1: future CLOCK is ignored
TEMPO_RANGE = (30.0, 240.0)


def _finite(x) -> bool:
    return type(x) in (int, float) and math.isfinite(x)


@dataclass(frozen=True)
class Snapshot:
    sequence: int
    t_source: float             # seconds on the shared perf_counter clock
    state: str
    quality: str
    beat_position: float
    tempo_bpm: float            # v1 field
    estimated_tempo_bpm: float  # stable estimate (falls back to tempo_bpm)
    phase_rate_bpm: float       # slope for extrapolation (falls back to tempo_bpm)
    meter: tuple
    raw: dict


class ConductorClock:
    """Holds the newest accepted snapshot and a latched STOP. Thread-safe."""

    def __init__(self, *, session_id=None, clock_domain=None):
        self._lock = threading.Lock()
        self.session_id = session_id
        self.clock_domain = clock_domain
        self.latest: Snapshot | None = None
        self.stop: dict | None = None           # latched STOP packet
        self.last_accept_s: float | None = None
        self.transport_closed: str | None = None
        self.accepted = 0
        self.ignored: dict = {}
        self.first_running_s: float | None = None

    # ── ingest ───────────────────────────────────────────────────
    def _ignore(self, why: str) -> str:
        self.ignored[why] = self.ignored.get(why, 0) + 1
        return "ignored:" + why

    def ingest(self, msg, now_s: float) -> str:
        """Returns 'clock', 'stop' or 'ignored:<reason>'. Never raises on bad input."""
        with self._lock:
            if not isinstance(msg, dict):
                return self._ignore("not_an_object")
            if msg.get("version") != 1 or type(msg.get("version")) is not int:
                return self._ignore("version")
            sid, dom = msg.get("session_id"), msg.get("clock_domain")
            if not isinstance(sid, str) or not sid or not isinstance(dom, str) or not dom:
                return self._ignore("identity_missing")
            if self.clock_domain is not None and dom != self.clock_domain:
                return self._ignore("foreign_clock_domain")
            if self.session_id is not None and sid != self.session_id:
                return self._ignore("foreign_session")
            kind, state = msg.get("kind"), msg.get("state")
            if kind not in ("CLOCK", "STOP") or state not in STATES:
                return self._ignore("kind_or_state")
            seq, src = msg.get("sequence"), msg.get("source_monotonic_ns")
            if type(seq) is not int or seq < 0 or type(src) is not int or src < 0:
                return self._ignore("sequence_or_timestamp")

            if kind == "STOP" or state in ("stopped", "ended"):
                # STOP is matched before freshness and ordering, and latches.
                if self.session_id is None:
                    self.session_id, self.clock_domain = sid, dom
                if self.stop is None:
                    self.stop = dict(msg)
                return "stop"
            if self.stop is not None:
                return self._ignore("after_stop")

            t_src = src / 1e9
            age = now_s - t_src
            if age > MAX_PACKET_AGE_S:
                return self._ignore("stale")
            if age < -MAX_PACKET_FUTURE_S:
                return self._ignore("future")
            if self.latest is not None and seq <= self.latest.sequence:
                return self._ignore("old_sequence")
            beat, tempo, quality, meter = (msg.get("beat_position"), msg.get("tempo_bpm"),
                                           msg.get("quality"), msg.get("meter"))
            if not _finite(beat) or beat < 0 or not _finite(tempo) or quality not in QUALITIES:
                return self._ignore("timing_fields")
            if not TEMPO_RANGE[0] <= tempo <= TEMPO_RANGE[1]:
                return self._ignore("tempo_range")
            if (not isinstance(meter, (list, tuple)) or len(meter) != 2
                    or any(type(m) is not int or m <= 0 for m in meter)):
                return self._ignore("meter")
            est = msg.get("estimated_tempo_bpm")
            est = float(est) if _finite(est) and TEMPO_RANGE[0] <= est <= TEMPO_RANGE[1] else float(tempo)
            rate = msg.get("phase_rate_bpm")
            rate = float(rate) if _finite(rate) and 0 < rate <= TEMPO_RANGE[1] else float(tempo)

            if self.session_id is None:
                self.session_id, self.clock_domain = sid, dom       # bind to the first valid sender
            self.latest = Snapshot(seq, t_src, state, quality, float(beat), float(tempo), est, rate,
                                   tuple(meter), dict(msg))
            self.last_accept_s = now_s
            self.accepted += 1
            if state == "running" and self.first_running_s is None:
                self.first_running_s = now_s
            return "clock"

    def close_transport(self, reason: str) -> None:
        with self._lock:
            if self.transport_closed is None:
                self.transport_closed = str(reason)

    # ── queries ──────────────────────────────────────────────────
    def view(self):
        with self._lock:
            return self.latest, self.stop, self.last_accept_s, self.transport_closed

    @staticmethod
    def phase_of(snap: Snapshot, now_s: float) -> float:
        """Beat position extrapolated to `now_s` from one snapshot."""
        if snap.state != "running":
            return snap.beat_position
        return snap.beat_position + max(0.0, now_s - snap.t_source) * snap.phase_rate_bpm / 60.0
