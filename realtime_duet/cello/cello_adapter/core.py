"""Continuous phase-to-bow scheduler. Standard library only, no robot I/O."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path


@dataclass(frozen=True)
class Snapshot:
    version: int
    session_id: str
    clock_domain: str
    sequence: int
    source_monotonic_ns: int
    kind: str
    state: str
    beat_position: float = 0.0
    tempo_bpm: float = 0.0
    meter: tuple = (4, 4)
    quality: str = "unreliable"

    @property
    def seq(self):
        return self.sequence

    @property
    def monotonic_s(self):
        return self.source_monotonic_ns / 1e9

    @property
    def phase_beats(self):
        return self.beat_position

    @property
    def bpm(self):
        return self.tempo_bpm

    @classmethod
    def parse(cls, payload):
        if not isinstance(payload, dict):
            raise ValueError("snapshot must be an object")
        keys = cls.__dataclass_fields__
        s = cls(**{key: payload[key] for key in keys if key in payload})
        if s.version != 1 or type(s.version) is not int or not s.session_id or not s.clock_domain:
            raise ValueError("schema/session invalid")
        if type(s.seq) is not int or s.seq < 0 or type(s.source_monotonic_ns) is not int:
            raise ValueError("sequence/timestamp invalid")
        if s.kind not in ("CLOCK", "STOP") or s.source_monotonic_ns < 0:
            raise ValueError("kind/timestamp invalid")
        if s.kind == "STOP":
            return s
        if not {"beat_position", "tempo_bpm", "meter", "quality"}.issubset(payload):
            raise ValueError("CLOCK missing timing fields")
        for value in (s.phase_beats, s.bpm):
            if type(value) not in (int, float) or not math.isfinite(value):
                raise ValueError("nonfinite/non-numeric timing")
        if s.state not in ("ready", "running", "stopped", "ended"):
            raise ValueError("state invalid")
        if s.quality not in ("tracked", "predicted", "unreliable") or list(s.meter) != [4, 4]:
            raise ValueError("quality/meter invalid")
        if not 30 <= s.bpm <= 240 or not 0 <= s.phase_beats <= 10000:
            raise ValueError("timing outside supported mock envelope")
        return s


class Journal:
    """One exclusive, durable reservation per session; never auto-resume."""
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def reserve(self, session_id):
        import os
        name = hashlib.sha256(session_id.encode()).hexdigest() + ".json"
        with (self.directory / name).open("x", encoding="utf-8") as f:
            json.dump({"session_id": session_id, "status": "reserved_no_auto_resume"}, f)
            f.flush()
            os.fsync(f.fileno())


class MockBackend:
    """Phase-following simulation; no geometry or physical safety claim."""
    def __init__(self, sink=None):
        self.events = []
        self.sink = sink
        self.active = None
        self.progress = 0.0

    def emit(self, kind, now, **values):
        event = {"kind": kind, "monotonic_s": now, **values}
        self.events.append(event)
        if self.sink:
            self.sink(event)

    def start(self, index, phase, bpm, now):
        if self.active is not None:
            raise RuntimeError("overlapping strokes")
        self.active = index
        self.emit("bow_start", now, index=index, measure=index + 1,
                  direction="down" if index % 2 == 0 else "up", string="A",
                  pitch_mode="rhythm_only_unapproved_substitution",
                  onset_beats=index * 4, duration_beats=4, phase_beats=phase, bpm=bpm)

    def update(self, phase, bpm, now):
        if self.active is not None:
            self.progress = min(1.0, max(0.0, (phase - self.active * 4) / 4))

    def end(self, now):
        if self.active is not None:
            self.emit("bow_end", now, index=self.active)
            self.active = None

    def stop(self, reason, now):
        self.end(now)
        self.emit("stop", now, reason=reason)


class Scheduler:
    """Consume heartbeat snapshots; tick from a local monotonic loop."""
    def __init__(self, session_id, backend, journal=None, heartbeat_timeout=1.0,
                 clock_domain="shared-local-run", armed=False):
        if not session_id:
            raise ValueError("explicit session_id required")
        self.session_id, self.backend = session_id, backend
        self.clock_domain, self.armed = clock_domain, armed
        self.journal = journal
        self.heartbeat_timeout = heartbeat_timeout
        self.snapshot = None
        self.last_received = None
        self.last_tick = None
        self.last_phase = None
        self.next_index = 0
        self.active_index = None
        self.terminal = False
        self.reason = None
        self.reserved = False
        self.running_seen = False

    def stop(self, reason, now):
        if not self.terminal:
            self.terminal, self.reason = True, reason
            self.active_index = None
            # No future command queue exists. stop must be nonblocking and cancel
            # the active action in a hardware implementation.
            self.backend.stop(reason, now)
            # Even Stop before the first bow retires this session permanently.
            # Never delay the immediate backend stop for a disk write.
            if self.journal and not self.reserved:
                try:
                    self.journal.reserve(self.session_id)
                    self.reserved = True
                except OSError:
                    pass  # existing reservation is already fail-closed

    def phase(self, now):
        s = self.snapshot
        return s.phase_beats + max(0.0, now - s.monotonic_s) * s.bpm / 60

    def ingest(self, payload, now):
        if self.terminal:
            return False
        if isinstance(payload, dict) and (payload.get("session_id") != self.session_id
                                         or payload.get("clock_domain") != self.clock_domain):
            return False
        try:
            s = Snapshot.parse(payload)
        except (ValueError, TypeError) as exc:
            self.stop("invalid_message:" + str(exc), now)
            return False
        if s.session_id != self.session_id:
            return False
        # A valid explicit Stop is fail-closed, including a duplicate Stop.
        if s.kind == "STOP" or s.state == "stopped":
            self.stop("explicit_stop", now)
            return True
        old = self.snapshot
        if old and s.seq <= old.seq:
            return False
        if self.last_received is not None and now - self.last_received > self.heartbeat_timeout:
            self.stop("heartbeat_timeout", now)
            return False
        if not -0.05 <= now - s.monotonic_s <= 0.5:
            return False
        # READY is not a running anchor. Validate the first RUNNING admission
        # independently of whether READY snapshots have already been received.
        if s.state == "running" and not self.running_seen:
            phase_at_receipt = s.phase_beats + max(0.0, now-s.monotonic_s)*s.bpm/60
            if s.phase_beats > 0.1 or phase_at_receipt > 0.1:
                self.stop("late_join_refused", now)
                return False
        if old:
            if s.monotonic_s < old.monotonic_s or s.phase_beats < old.phase_beats:
                return False
            if old.state == "running" and s.state != "running":
                if s.state == "ended":
                    self.stop("score_complete" if s.phase_beats >= 48 else "premature_end", now)
                    return True
                self.stop("invalid_state_transition", now)
                return False
            expected = self.phase(s.monotonic_s) if old.state == "running" else old.phase_beats
            if abs(s.phase_beats - expected) > 0.5:
                self.stop("phase_discontinuity", now)
                return False
        self.snapshot, self.last_received = s, now
        if s.state == "running":
            self.running_seen = True
        return True

    def tick(self, now):
        if self.terminal or self.snapshot is None:
            return
        fault = getattr(self.backend, "fault", None)
        if fault:
            self.stop("hardware_fault:" + str(fault), now)
            return
        if self.last_tick is not None:
            if now < self.last_tick or now - self.last_tick > 0.25:
                self.stop("local_loop_stall", now)
                return
        self.last_tick = now
        if now - self.last_received > self.heartbeat_timeout:
            self.stop("heartbeat_timeout", now)
            return
        if not self.armed or self.snapshot.state != "running":
            return
        phase = self.phase(now)
        # Small phase corrections may hold future motion but never reverse it.
        if self.last_phase is not None:
            phase = max(phase, self.last_phase)
        self.last_phase = phase
        # Admission may precede the first actual tick; do not let a delayed
        # consumer or a later arm action turn an old beat-zero sample into bow0.
        if self.next_index == 0 and phase > 0.1:
            self.stop("late_join_refused", now)
            return
        if phase >= 48:
            self.stop("score_complete", now)
            return
        if self.active_index is not None and phase >= (self.active_index + 1) * 4:
            self.backend.end(now)
            self.active_index = None
        if self.active_index is None and phase >= self.next_index * 4:
            # Never issue a burst of catch-up bow commands after a jump/delay.
            if phase - self.next_index * 4 > 0.5:
                self.stop("missed_bow_boundary", now)
                return
            if not self.reserved:
                try:
                    if self.journal:
                        self.journal.reserve(self.session_id)
                except (OSError, ValueError):
                    self.stop("session_already_used_or_journal_failed", now)
                    return
                self.reserved = True
            self.backend.start(self.next_index, phase, self.snapshot.bpm, now)
            self.active_index = self.next_index
            self.next_index += 1
        self.backend.update(phase, self.snapshot.bpm, now)


def hardware_backend(*args, **kwargs):
    """Explicit entry; missing review fails before any hardware imports."""
    if not args:
        raise RuntimeError("HARDWARE BLOCKED: explicit reviewed configuration and execution flags required")
    from .hardware import open_hardware
    return open_hardware(*args, **kwargs)
