"""The two glockenspiel parts, as beat positions and solenoid channels.

`songs/arirang.json` and `songs/twinkel.json` are the note lists bundled with
Aria-Solenoid-Studio, copied unchanged: 48 beats each, one [beat, written
pitch] pair per strike. Channel n is serial key n and the nth pitch of NOTES
(Aria-Solenoid-Studio/tools/play_music.py).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

NOTES = ("D4", "E4", "G4", "A4", "B4", "C5", "D5", "E5")     # solenoids 1..8, written pitches
MIN_GAP_S = 0.101          # firmware: 30 ms pulse, then 70 ms recovery on the same solenoid
SONG_FILES = {"arirang": "arirang.json", "twinkle": "twinkel.json"}
SONGS_DIR = Path(__file__).resolve().parent / "songs"


@dataclass(frozen=True)
class Note:
    beat: float
    name: str
    channel: int           # 1..8


@dataclass(frozen=True)
class Score:
    name: str
    title: str
    beats: float
    written_tempo: float
    notes: tuple

    def fastest_tempo(self) -> float:
        """BPM above which two strikes of one solenoid would come closer than its recovery time."""
        last, tightest = {}, float("inf")
        for n in self.notes:
            if n.channel in last:
                tightest = min(tightest, n.beat - last[n.channel])
            last[n.channel] = n.beat
        return float("inf") if tightest == float("inf") else tightest * 60.0 / MIN_GAP_S


def load(name: str) -> Score:
    if name not in SONG_FILES:
        raise ValueError(f"unknown song {name!r}; choose from {sorted(SONG_FILES)}")
    raw = json.loads((SONGS_DIR / SONG_FILES[name]).read_text())
    notes = []
    for beat, pitch in raw["events"]:
        if pitch not in NOTES:
            raise ValueError(f"{name}: pitch {pitch} has no solenoid")
        if not 0 <= beat < raw["beats"]:
            raise ValueError(f"{name}: beat {beat} outside the piece")
        notes.append(Note(float(beat), pitch, NOTES.index(pitch) + 1))
    if any(b.beat < a.beat for a, b in zip(notes, notes[1:])):
        raise ValueError(f"{name}: notes are not in time order")
    return Score(name, raw["title"], float(raw["beats"]), float(raw["tempo"]), tuple(notes))
