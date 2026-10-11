"""Camera-free freshness checks shared by capture and deterministic tests."""
class FrameFreshness:
    def __init__(self):
        self.last_digest=None
        self.last_unique_time=None

    def observe(self,digest,t):
        if digest==self.last_digest:
            return False
        self.last_digest=digest
        self.last_unique_time=t
        return True

    def stale(self,t,timeout):
        return self.last_unique_time is None or t-self.last_unique_time>timeout
