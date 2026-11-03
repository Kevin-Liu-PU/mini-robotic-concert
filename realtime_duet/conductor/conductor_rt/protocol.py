"""Shared wire v1. Internal detailed events are logged separately."""
import math


class WireEncoder:
    def __init__(self, clock_domain):
        if not clock_domain:
            raise ValueError("explicit nonempty clock domain required")
        self.domain = clock_domain
        self.sequence = 0

    def encode(self, snapshot):
        self.sequence += 1
        state = snapshot["state"]
        state = "ready" if state in ("idle", "armed") else state
        stopped = state in ("stopped", "ended")
        q = snapshot["quality"]
        quality = "tracked" if q["mode"] == "observed" and q["vision_available"] else "predicted"
        if state == "ready":
            quality = "unreliable"
        tempo = snapshot["phase_rate_bps"] * 60 if state == "running" else snapshot["bpm"]
        assert math.isfinite(tempo)
        return dict(version=1, session_id=snapshot["session_id"], clock_domain=self.domain,
                    sequence=self.sequence, source_monotonic_ns=snapshot["monotonic_ns"],
                    kind="STOP" if stopped else "CLOCK", state=state,
                    beat_position=snapshot["beat_position"], tempo_bpm=tempo,
                    meter=[snapshot["meter"], 4], quality=quality,
                    estimated_tempo_bpm=snapshot["bpm"],
                    reason=snapshot.get("reason"), observation_age_s=q["observation_age_s"])
