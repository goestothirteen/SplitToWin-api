"""HTTP surface.

Every failure leaves here as JSON. The old app let exceptions escape into
Werkzeug's HTML error page, so the browser's `response.json()` threw while
parsing the error and the user saw a generic alert with the real cause lost.

Uploading a receipt starts a job and returns at once; the phone then polls
`GET /parse-receipt/<jobId>` for progress. See `services/jobs.py` for why
nothing waits on the model with a socket held open any more.
"""

import logging
import time
import uuid
from collections import defaultdict, deque

from flask import Blueprint, current_app, jsonify, request

from .config import Config
from .services.jobs import store as jobs
from .services.receipt_parser import active_providers, parse_receipt_image
from .services.stats import access_log_report, render_html

log = logging.getLogger(__name__)

api = Blueprint("api", __name__)

# Per-IP sliding window. In-process and single-instance, which is all this
# needs — it exists so a stranger who finds the URL can't drain the quota,
# not to coordinate limits across a fleet.
_hits: dict[str, deque] = defaultdict(deque)
_WINDOW_S = 3600


def _rate_limited(ip: str) -> bool:
    now = time.monotonic()
    seen = _hits[ip]
    while seen and now - seen[0] > _WINDOW_S:
        seen.popleft()
    if len(seen) >= Config.RATE_LIMIT_PER_HOUR:
        return True
    seen.append(now)
    if len(_hits) > 2048:  # bound memory against spoofed source addresses
        for stale in [k for k, v in _hits.items() if not v][:1024]:
            _hits.pop(stale, None)
    return False


def _client_ip() -> str:
    # Caddy sets X-Forwarded-For and is the only thing that can reach us, so
    # the first hop is trustworthy here in a way it would not be if the
    # container were exposed directly.
    forwarded = request.headers.get("X-Forwarded-For", "")
    return forwarded.split(",")[0].strip() or (request.remote_addr or "unknown")


@api.get("/healthz")
def healthz():
    """Liveness. Must not call the model — it is polled by Docker."""
    missing = Config.missing()
    return (
        jsonify(
            {
                "status": "ok" if not missing else "degraded",
                "missingConfig": missing,
                "providers": active_providers(),
            }
        ),
        200 if not missing else 503,
    )


@api.get("/stats")
def stats():
    """How the app has been used: one row per receipt uploaded.

    Deliberately an open URL. A row is counts, a device class and a visitor
    number — no addresses, and nothing off the receipt itself, which never
    reaches the server — so there is nothing on it worth the friction of a
    login. A browser gets a table; curl and scripts get the JSON behind it.
    """
    report = access_log_report(Config.ACCESS_LOG_GLOB, Config.STATS_CACHE_S)
    wants = request.accept_mimetypes.best_match(["application/json", "text/html"])
    if wants == "text/html":
        return render_html(report), 200, {"Content-Type": "text/html; charset=utf-8"}
    return jsonify(report)


@api.post("/parse-receipt")
def parse_receipt():
    """Take the photo, start reading it, and answer immediately with a job id.

    The response is 202, never the receipt: the caller polls for the result.
    """
    if _rate_limited(_client_ip()):
        return (
            jsonify(
                {
                    "error": "Too many receipts from this address. Try again later.",
                    "code": "rate_limited",
                }
            ),
            429,
        )

    upload = request.files.get("image")
    if upload is None:
        return (
            jsonify(
                {
                    "error": "No image was uploaded.",
                    "code": "missing_image",
                }
            ),
            400,
        )

    raw = upload.read(Config.MAX_UPLOAD_BYTES + 1)
    if len(raw) > Config.MAX_UPLOAD_BYTES:
        limit_mb = Config.MAX_UPLOAD_BYTES // (1024 * 1024)
        return (
            jsonify(
                {
                    "error": f"That image is larger than {limit_mb} MB.",
                    "code": "too_large",
                }
            ),
            413,
        )
    if not raw:
        return jsonify({"error": "That file was empty.", "code": "empty"}), 400

    # Phones drop the connection when backgrounded, so the client reuses its
    # job id. The same id arriving twice attaches to the parse already
    # running instead of reading the same receipt a second time.
    job_id = (request.form.get("jobId") or "").strip()[:64] or uuid.uuid4().hex
    mime = upload.mimetype or "image/jpeg"

    def work(report):
        started = time.monotonic()
        parsed = parse_receipt_image(raw, mime, report)
        log.info(
            "parsed receipt items=%d bytes=%d ms=%d job=%s",
            len(parsed.items),
            len(raw),
            int((time.monotonic() - started) * 1000),
            job_id,
        )
        return parsed.to_dict()

    job, is_new = jobs.start(job_id, work)
    if not is_new:
        log.info("job %s already under way, attaching", job_id)
    return jsonify(job.snapshot()), 202


@api.get("/parse-receipt/<job_id>")
def parse_progress(job_id: str):
    """How that parse is going. Polled about once a second while it runs.

    A finished-but-failed job is still a successful poll, so the failure
    travels in the body rather than as an HTTP error the client has to
    unpick. Only "I have never heard of this job" is a 404.
    """
    job = jobs.get((job_id or "").strip()[:64])
    if job is None:
        return (
            jsonify(
                {
                    "error": "That upload has expired. Send the photo again.",
                    "code": "job_unknown",
                }
            ),
            404,
        )
    return jsonify(job.snapshot())


@api.app_errorhandler(413)
def _too_large(_):
    return jsonify({"error": "That upload was too large.", "code": "too_large"}), 413


@api.app_errorhandler(404)
def _not_found(_):
    return jsonify({"error": "Not found.", "code": "not_found"}), 404


@api.app_errorhandler(Exception)
def _unhandled(exc):
    # Last line of defence: never let an HTML traceback page reach the client.
    current_app.logger.exception("unhandled error")
    return jsonify({"error": "Something went wrong.", "code": "internal"}), 500
