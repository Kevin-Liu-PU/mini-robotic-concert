"""Which notes are due, decided from the Conductor's clock. No I/O.

A note is struck when the Conductor's beat position reaches the note's beat.
The beat position between two packets is extrapolated at the clock's own rate
(`ConductorClock.phase_of`), so a strike does not wait for the next packet.

What it never does: play a burst. If the beat position moves past several
notes at once (the clock re-locked to the conductor), notes older than
`late_beats` are dropped, not caught up. If it moves backwards, nothing is
replayed. A solenoid is never asked to strike again inside its recovery time.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from conductor_cello.clock import ConductorClock

from .score import MIN_GAP_S, Score


@dataclass
class Tally:
    struck: int = 0
    dropped_late: int = 0          # the clock moved past them
    dropped_recovery: int = 0      # same solenoid too soon after its last strike
    worst_late_s: float = 0.0


@dataclass
class Player:
    score: Score
    lead_s: float = 0.0            # strike this much before the beat (solenoid travel); 0 until measured
    late_beats: float = 0.5        # a note this far behind the clock is dropped instead of played late
    clock_timeout_s: float = 1.0   # silence from the Conductor that ends the performance
    index: int = 0
    started: bool = False
    tally: Tally = field(default_factory=Tally)
    _last: dict = field(default_factory=dict)

    def step(self, view, now_s: float):
        """view = ConductorClock.view(). Returns (notes to strike now, status).

        status: 'waiting' (not started), 'playing', 'holding' (the Conductor paused), 'finished'
        (all notes done and the piece's beats are over), or an end reason: 'stop:<state>',
        'gone:<why>', 'silent'.
        """
        snap, stop, last, closed = view
        if stop is not None:
            return [], "stop:" + str(stop.get("state"))
        if closed is not None:
            return [], "gone:" + closed
        if last is not None and now_s - last > self.clock_timeout_s:
            return [], "silent"
        if snap is None or (snap.state != "running" and not self.started):
            return [], "waiting"
        if snap.state != "running":
            return [], "holding"
        self.started = True
        rate = snap.phase_rate_bpm / 60.0                                   # beats per second
        phase = ConductorClock.phase_of(snap, now_s)
        reach = phase + self.lead_s * rate
        due = []
        notes = self.score.notes
        reached = []
        while self.index < len(notes) and notes[self.index].beat <= reach + 1e-9:
            reached.append(notes[self.index])
            self.index += 1
        newest = reached[-1].beat if reached else None
        for note in reached:
            behind = reach - note.beat
            # several beats reached in one step means the clock jumped: only the newest is played
            if behind > self.late_beats or note.beat != newest:
                self.tally.dropped_late += 1
                continue
            if now_s - self._last.get(note.channel, float("-inf")) < MIN_GAP_S:
                self.tally.dropped_recovery += 1
                continue
            self._last[note.channel] = now_s
            self.tally.struck += 1
            late = behind / rate if rate > 0 else 0.0
            self.tally.worst_late_s = max(self.tally.worst_late_s, late)
            due.append((note, phase, late))
        if self.index >= len(notes) and phase >= self.score.beats:
            return due, "finished"
        return due, "playing"
