"""Short-lived, path-scoped artwork grants backed by a live host session."""
import secrets
import threading
import time


class PreviewSessions:
    def __init__(self, ttl=600, capacity=1024):
        self.ttl, self.capacity = ttl, capacity
        self.entries = {}
        self.lock = threading.Lock()

    def issue(self, token, path):
        now = time.time()
        with self.lock:
            self.entries = {key: value for key, value in self.entries.items() if value[2] > now}
            while len(self.entries) >= self.capacity:
                del self.entries[next(iter(self.entries))]
            grant = secrets.token_urlsafe(32)
            self.entries[grant] = (token, path, now + self.ttl)
        return grant

    def token(self, grant, path):
        if not isinstance(grant, str) or len(grant) != 43:
            return ''
        with self.lock:
            value = self.entries.get(grant)
            if value and value[1] == path and value[2] > time.time():
                return value[0]
        return ''
