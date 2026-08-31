"""Short-lived results, keyed by a client-supplied job id.

Phones background aggressively. iOS Safari suspends a tab when you switch
apps, and under memory pressure discards it outright — so a parse that takes
12-30s routinely loses its connection halfway through even though the server
finished the work.

The client retries with the same job id when it comes back. Without this, that
retry would run the model a second time: slower for the user, and a second
charge against the quota for a receipt already parsed. So:

  * a completed result is served straight from here, no model call
  * a retry that arrives while the first is *still running* waits for it,
    rather than starting a competing parse

The cache is per-process and deliberately small. Losing it costs one re-parse,
which is the same as not having it, so there is no need for anything durable.
"""

import logging
import threading
import time
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

TTL_S = 900  # 15 minutes: long enough to answer a phone call mid-dinner
MAX_ENTRIES = 64


@dataclass
class _Entry:
    created: float
    done: threading.Event = field(default_factory=threading.Event)
    result: dict | None = None
    error: Exception | None = None


class JobCache:
    def __init__(self, ttl_s: int = TTL_S, max_entries: int = MAX_ENTRIES):
        self._ttl = ttl_s
        self._max = max_entries
        self._lock = threading.Lock()
        self._entries: dict[str, _Entry] = {}

    def _evict(self) -> None:
        """Called with the lock held."""
        now = time.monotonic()
        stale = [k for k, e in self._entries.items() if now - e.created > self._ttl]
        for key in stale:
            self._entries.pop(key, None)
        while len(self._entries) > self._max:
            oldest = min(self._entries, key=lambda k: self._entries[k].created)
            self._entries.pop(oldest, None)

    def claim(self, job_id: str):
        """Take ownership of a job, or get the entry someone else owns.

        @returns (is_owner, entry). The owner runs the parse and calls
        finish(); everyone else waits on the entry.
        """
        with self._lock:
            self._evict()
            existing = self._entries.get(job_id)
            if existing is not None:
                return False, existing
            entry = _Entry(created=time.monotonic())
            self._entries[job_id] = entry
            return True, entry

    def finish(self, job_id: str, result: dict | None, error: Exception | None) -> None:
        with self._lock:
            entry = self._entries.get(job_id)
        if entry is None:
            return
        entry.result = result
        entry.error = error
        # A failure is not worth caching: the client should be free to retry
        # and actually get another attempt.
        if error is not None:
            with self._lock:
                self._entries.pop(job_id, None)
        entry.done.set()

    def wait(self, entry: _Entry, timeout_s: float):
        """Wait for whoever owns this job. Returns (result, error, timed_out)."""
        if entry.done.wait(timeout=timeout_s):
            return entry.result, entry.error, False
        return None, None, True

    def drop(self, job_id: str) -> None:
        with self._lock:
            self._entries.pop(job_id, None)


cache = JobCache()
