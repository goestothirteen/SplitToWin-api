"""What /stats can say about a receipt beyond what the access log saw.

The log knows that a photo arrived and how the parse went. It cannot know
what was on the photo, or who the bill was then split between — the names
are typed into the phone after the parse and used to go no further. This is
the small store that keeps both, one row per upload, keyed by the same job
id the log carries in its paths so the two can be joined.

Three things are kept, each written at the moment it is known:

  * the photo, downscaled hard and re-encoded, when the job starts;
  * the receipt's total and currency, once the parse succeeds;
  * the names at the table, whenever the phone sends them.

It is SQLite plus a directory of JPEGs under one data directory, which
compose mounts from the host so it outlives the container. The API has a
single worker process (see gunicorn.conf.py), so there is no multi-process
contention to design for; threads open a connection each.

Nothing here may fail a request. Every write swallows and logs its own
errors: a full disk should cost the archive a row, not the person their bill.

All of it is shown on /stats, which stays an open URL by the owner's
choice: a personal app used among friends, where a login would be more
friction than the page is worth.
"""

import io
import json
import logging
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone

from PIL import Image, ImageOps

log = logging.getLogger(__name__)

SGT = timezone(timedelta(hours=8))

# Job ids come from the phone (a uuid hex) or are minted here, but they also
# become filenames, so only this shape is ever trusted.
JOB_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# Kept small on purpose: the box has well under a GB to spare, and a receipt
# at 1000px / quality 70 is still readable while weighing ~100 KB.
PHOTO_EDGE = 1000
PHOTO_QUALITY = 70

MAX_PEOPLE = 30
MAX_NAME_LEN = 40

_SCHEMA = """
CREATE TABLE IF NOT EXISTS receipts (
    job_id      TEXT PRIMARY KEY,
    uploaded_at REAL NOT NULL,
    photo_kb    INTEGER,
    currency    TEXT,
    total       REAL,
    item_count  INTEGER,
    people      TEXT,
    people_at   REAL
);
"""


def valid_job_id(job_id: str) -> bool:
    return bool(job_id) and JOB_ID.match(job_id) is not None


class Archive:
    def __init__(self, directory: str):
        self.dir = directory
        self.photos = os.path.join(directory, "photos")
        self.db = os.path.join(directory, "receipts.sqlite")
        self._ready = False
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.dir)

    # -- plumbing ----------------------------------------------------------

    def _prepare(self) -> bool:
        """Create the directory and schema on first use. False if it cannot be
        done, in which case every write is a no-op and says so once."""
        if self._ready:
            return True
        if not self.enabled:
            return False
        with self._lock:
            if self._ready:
                return True
            try:
                os.makedirs(self.photos, exist_ok=True)
                with self._connect() as conn:
                    conn.executescript(_SCHEMA)
                self._ready = True
            except Exception:
                log.exception("receipt archive unavailable at %s", self.dir)
                return False
        return True

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db, timeout=5, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _write(self, sql: str, params: tuple) -> None:
        if not self._prepare():
            return
        try:
            with self._connect() as conn:
                conn.execute(sql, params)
        except Exception:
            log.exception("receipt archive write failed")

    # -- writes, one per fact ---------------------------------------------

    def record_upload(self, job_id: str, raw: bytes) -> None:
        """A photo has arrived: keep a small copy and open the row.

        Called on the worker thread, not in the request, so the 202 goes back
        to the phone before any image work happens. A repeat upload of the
        same id (the phone retrying) leaves the first row alone.
        """
        if not valid_job_id(job_id) or not self._prepare():
            return
        kb = None
        try:
            with Image.open(io.BytesIO(raw)) as img:
                img = ImageOps.exif_transpose(img).convert("RGB")
                img.thumbnail((PHOTO_EDGE, PHOTO_EDGE), Image.LANCZOS)
                img.save(self.photo_path(job_id), format="JPEG", quality=PHOTO_QUALITY, optimize=True)
            kb = round(os.path.getsize(self.photo_path(job_id)) / 1024)
        except Exception:
            # Not an image we can open. The parse will say so to the person;
            # the row still records that something was sent.
            log.info("archive: could not keep photo for job %s", job_id)
        self._write(
            "INSERT OR IGNORE INTO receipts (job_id, uploaded_at, photo_kb) VALUES (?, ?, ?)",
            (job_id, time.time(), kb),
        )

    def record_parsed(self, job_id: str, receipt: dict) -> None:
        if not valid_job_id(job_id):
            return
        self._write(
            "UPDATE receipts SET currency = ?, total = ?, item_count = ? WHERE job_id = ?",
            (
                str(receipt.get("currency") or ""),
                receipt.get("total"),
                len(receipt.get("items") or []),
                job_id,
            ),
        )

    def record_people(self, job_id: str, names: list) -> bool:
        """The names at the table, as the phone has them right now.

        Only an upload this archive has seen may be annotated: that keeps a
        stranger from filling the store with rows that never had a receipt.
        @returns whether the job was known.
        """
        if not valid_job_id(job_id) or not self._prepare():
            return False
        try:
            with self._connect() as conn:
                cur = conn.execute(
                    "UPDATE receipts SET people = ?, people_at = ? WHERE job_id = ?",
                    (json.dumps(clean_names(names), ensure_ascii=False), time.time(), job_id),
                )
                return cur.rowcount > 0
        except Exception:
            log.exception("receipt archive write failed")
            return False

    # -- reads -------------------------------------------------------------

    def photo_path(self, job_id: str) -> str:
        return os.path.join(self.photos, job_id + ".jpg")

    def has_photo(self, job_id: str) -> bool:
        return valid_job_id(job_id) and os.path.isfile(self.photo_path(job_id))

    def by_job(self) -> dict:
        """Everything kept, keyed by job id, in the shape /stats merges in."""
        if not self.enabled or not os.path.isfile(self.db) or not self._prepare():
            return {}
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT job_id, uploaded_at, photo_kb, currency, total, item_count, people"
                    " FROM receipts"
                ).fetchall()
        except Exception:
            log.exception("receipt archive read failed")
            return {}
        out = {}
        for job_id, uploaded_at, photo_kb, currency, total, item_count, people in rows:
            try:
                names = json.loads(people) if people else []
            except ValueError:
                names = []
            out[job_id] = {
                "at": datetime.fromtimestamp(uploaded_at, SGT).isoformat(timespec="seconds"),
                "people": names,
                "currency": currency or "",
                "total": total,
                "items": item_count,
                "photo": self.has_photo(job_id) and photo_kb is not None,
            }
        return out


def clean_names(names) -> list[str]:
    """Strings only, trimmed, bounded, in the order given. Anything else the
    phone sends — objects, blanks, a 5 KB name — is dropped, not stored.

    Duplicates are kept: two people called Mark at one table is real, and
    the UI went out of its way to let them both exist.
    """
    out: list[str] = []
    for raw in names if isinstance(names, list) else []:
        if not isinstance(raw, str):
            continue
        name = " ".join(raw.split())[:MAX_NAME_LEN]
        if not name:
            continue
        out.append(name)
        if len(out) >= MAX_PEOPLE:
            break
    return out


# The data directory is a deploy decision (compose mounts a host directory
# at it); see Config.ARCHIVE_DIR. Importing Config here rather than passing
# it in keeps the store a module-level singleton the routes can share.
from ..config import Config  # noqa: E402

store = Archive(Config.ARCHIVE_DIR)
