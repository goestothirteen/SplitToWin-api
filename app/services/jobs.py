"""Receipt parses that outlive the request that started them.

Reading a busy receipt takes 30-60s. Holding an HTTP connection open for that
was the single biggest source of failure in this app: iOS Safari suspends a
backgrounded tab and drops the socket, so a parse the server had *finished*
came back to the person as an error, and a genuinely slow receipt hit a
deadline that existed only to stay under the gunicorn worker timeout.

So the upload starts a job and returns immediately, and the phone asks how it
is going every second or so. Nothing is holding a socket, which is what lets
the time budget be generous, the waiting screen show real progress instead of
a spinner, and a phone that went to sleep for two minutes pick the answer up
intact when it wakes.

Jobs live in memory, in one process (see `gunicorn.conf.py` — a second worker
would answer half the polls with "never heard of it"). Losing them on restart
costs one re-upload, which is why nothing here is written to disk: the receipt
and its contents stay in RAM for fifteen minutes and are never persisted.
"""

import logging
import threading
import time
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

TTL_S = 900  # 15 minutes: long enough to answer a phone call mid-dinner
MAX_ENTRIES = 64
# The box has 1 vCPU and shares it with two other apps. The Claude Code
# provider already caps itself at one Node process; this stops a queue of
# uploads from stacking image work on top of that.
MAX_ACTIVE = 3

QUEUED = "queued"
RUNNING = "running"
DONE = "done"
FAILED = "failed"


@dataclass
class Job:
    id: str
    created: float = field(default_factory=time.monotonic)
    status: str = QUEUED
    stage: str = "Waiting for a free reader"
    detail: str = ""
    items: int = 0
    result: dict | None = None
    error: Exception | None = None
    done: threading.Event = field(default_factory=threading.Event)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def report(self, stage: str = None, detail: str = None, items: int = None) -> None:
        """Called from the worker thread as the parse moves along.

        Must never raise: a progress update failing would otherwise take down
        a parse that was going perfectly well.
        """
        try:
            with self._lock:
                if stage is not None:
                    self.stage = stage
                    # A new stage invalidates the old detail; leaving it would
                    # caption the new step with the last one's text.
                    if detail is None:
                        self.detail = ""
                if detail is not None:
                    self.detail = detail
                if items is not None:
                    self.items = items
        except Exception:  # pragma: no cover - defensive
            log.exception("progress update failed")

    def snapshot(self) -> dict:
        with self._lock:
            out = {
                "jobId": self.id,
                "status": self.status,
                "stage": self.stage,
                "detail": self.detail,
                "itemsFound": self.items,
                "elapsedSeconds": round(time.monotonic() - self.created, 1),
            }
        if self.status == DONE:
            out["receipt"] = self.result
        elif self.status == FAILED:
            out["error"] = getattr(self.error, "message", None) or "Couldn't read that receipt."
            out["code"] = getattr(self.error, "code", "parse_failed")
            # Carried out to the route, which answers with it rather than
            # 200 so the failure is visible in the access log — the only
            # place /stats can learn that a background job went wrong. The
            # route takes it back off the body before sending.
            out["httpStatus"] = getattr(self.error, "status", 502)
        return out


class JobStore:
    def __init__(self, ttl_s: int = TTL_S, max_entries: int = MAX_ENTRIES,
                 max_active: int = MAX_ACTIVE):
        self._ttl = ttl_s
        self._max = max_entries
        self._slots = threading.BoundedSemaphore(max_active)
        self._lock = threading.Lock()
        self._jobs: dict[str, Job] = {}

    def _evict(self) -> None:
        """Called with the lock held. Never evicts a job still running."""
        now = time.monotonic()
        for key in [
            k
            for k, j in self._jobs.items()
            if now - j.created > self._ttl and j.status in (DONE, FAILED)
        ]:
            self._jobs.pop(key, None)
        while len(self._jobs) > self._max:
            finished = [k for k, j in self._jobs.items() if j.status in (DONE, FAILED)]
            if not finished:
                break
            self._jobs.pop(min(finished, key=lambda k: self._jobs[k].created), None)

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def start(self, job_id: str, work) -> tuple[Job, bool]:
        """Begin `work(job)` in the background, unless this id is already going.

        @returns (job, is_new). A repeat upload of the same id — the phone
        retrying after a dropped connection — attaches to the job already
        running rather than reading the same receipt twice.
        """
        with self._lock:
            self._evict()
            existing = self._jobs.get(job_id)
            if existing is not None:
                return existing, False
            job = Job(id=job_id)
            self._jobs[job_id] = job

        thread = threading.Thread(
            target=self._run, args=(job, work), name=f"parse-{job_id[:8]}", daemon=True
        )
        thread.start()
        return job, True

    def _run(self, job: Job, work) -> None:
        acquired = self._slots.acquire(timeout=120)
        if not acquired:
            # Every reader has been busy for two minutes. Saying so is more
            # use than letting the job sit in "queued" forever.
            self._fail(job, RuntimeError("busy"), "The readers are all busy. Try again in a moment.", "busy")
            return
        try:
            job.status = RUNNING
            job.report(stage="Getting the photo ready")
            result = work(job.report)
        except Exception as exc:
            self._fail(job, exc)
        else:
            job.result = result
            job.status = DONE
            job.report(stage="Done", detail="")
            job.done.set()
        finally:
            self._slots.release()

    def _fail(self, job: Job, exc: Exception, message: str = None, code: str = None) -> None:
        if message is not None and not hasattr(exc, "message"):
            exc.message = message
        if code is not None and not hasattr(exc, "code"):
            exc.code = code
        if not hasattr(exc, "message"):
            log.exception("parse job %s failed unexpectedly", job.id)
        job.error = exc
        job.status = FAILED
        job.report(stage="Stopped", detail="")
        job.done.set()

    def drop(self, job_id: str) -> None:
        with self._lock:
            self._jobs.pop(job_id, None)


store = JobStore()
