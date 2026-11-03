"""Single latest CLOCK slot plus independent priority STOP latch."""
import threading


class LatestInbox:
    def __init__(self, session, domain):
        self.session, self.domain = session, domain
        self.lock = threading.Lock()
        self.latest = None
        self.stop = None
        self.error = None

    def put(self, payload):
        with self.lock:
            if not isinstance(payload, dict):
                self.error = "invalid_message"
                return
            if payload.get("session_id") != self.session or payload.get("clock_domain") != self.domain:
                return
            if payload.get("kind") == "STOP":
                self.stop = payload
                self.latest = None
            elif self.stop is None:
                seq = payload.get("sequence")
                if type(seq) is not int:
                    self.error = "invalid_sequence"
                elif self.latest is None or seq > self.latest.get("sequence", -1):
                    self.latest = payload

    def fail(self, reason):
        with self.lock:
            self.error = reason

    def take(self):
        with self.lock:
            if self.stop is not None:
                return "message", self.stop
            if self.error:
                return "error", self.error
            latest, self.latest = self.latest, None
            return ("message", latest) if latest is not None else (None, None)
