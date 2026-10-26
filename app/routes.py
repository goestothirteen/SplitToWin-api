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

from flask import Blueprint, current_app, jsonify, request, send_file

from .config import Config
from .services.archive import store as archive
from .services.jobs import store as jobs
from .services.receipt_parser import active_providers, parse_receipt_image
from .services.stats import access_log_report, render_html, with_archive

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

    Deliberately an open URL, by the owner's choice: this is a personal app
    used among friends, and a login would be more friction than the page is
    worth. Each row is what the access log saw — device class, visitor
    number, outcome — joined with what the archive kept: the names the bill
    was split between, its total, and a link to a small copy of the photo.
    A browser gets a table; curl and scripts get the JSON behind it.
    """
    report = access_log_report(Config.ACCESS_LOG_GLOB, Config.STATS_CACHE_S)
    report = with_archive(
        report,
        archive.by_job(),
        photo_url=lambda job: "%s/stats/photo/%s" % (Config.API_PUBLIC_PREFIX, job),
    )
    wants = request.accept_mimetypes.best_match(["application/json", "text/html"])
    if wants == "text/html":
        return render_html(report), 200, {"Content-Type": "text/html; charset=utf-8"}
    return jsonify(report)


@api.get("/stats/photo/<job_id>")
def stats_photo(job_id: str):
    """The small copy of one receipt photo."""
    job_id = (job_id or "").strip()
    if not archive.has_photo(job_id):
        return jsonify({"error": "No photo kept for that upload.", "code": "not_found"}), 404
    return send_file(
        archive.photo_path(job_id),
        mimetype="image/jpeg",
        max_age=3600,
        conditional=True,
    )


@api.post("/parse-receipt/<job_id>/people")
def record_people(job_id: str):
    """The names at the table, sent when the phone goes on to split the bill.

    Idempotent and small: the latest list replaces the last. Only an upload
    the archive has a row for is accepted, so the id has to come from a real
    parse. The phone fires this and forgets — a failure here must never be
    something the person at the table sees.
    """
    body = request.get_json(silent=True)
    names = body.get("names") if isinstance(body, dict) else None
    if not isinstance(names, list):
        return jsonify({"error": "Send {\"names\": [...]}.", "code": "bad_request"}), 400
    if not archive.record_people((job_id or "").strip()[:64], names):
        return jsonify({"error": "Unknown upload.", "code": "job_unknown"}), 404
    return "", 204


@api.post("/parse-receipt")
@api.post("/parse-receipt/<path_job_id>")
def parse_receipt(path_job_id: str = ""):
    """Take the photo, start reading it, and answer immediately with a job id.

    The response is 202, never the receipt: the caller polls for the result.

    The id belongs in the path so that the access log, which is all /stats has
    to work with, can tie an upload to the progress checks that followed it.
    Without that the log sees an accepted upload and never learns how it went.
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
    job_id = (
        (path_job_id or "").strip()[:64]
        or (request.form.get("jobId") or "").strip()[:64]
        or uuid.uuid4().hex
    )
    mime = upload.mimetype or "image/jpeg"

    def work(report):
        started = time.monotonic()
        # Kept before the parse, so a photo the reader then refuses is still
        # on record as having been sent. This is on the worker thread: the
        # 202 has already gone back to the phone.
        archive.record_upload(job_id, raw)
        parsed = parse_receipt_image(raw, mime, report)
        log.info(
            "parsed receipt items=%d bytes=%d ms=%d job=%s",
            len(parsed.items),
            len(raw),
            int((time.monotonic() - started) * 1000),
            job_id,
        )
        result = parsed.to_dict()
        archive.record_parsed(job_id, result)
        return result

    job, is_new = jobs.start(job_id, work)
    if not is_new:
        log.info("job %s already under way, attaching", job_id)
    return jsonify(job.snapshot()), 202


@api.get("/parse-receipt/<job_id>")
def parse_progress(job_id: str):
    """How that parse is going. Polled about once a second while it runs.

    A failed parse answers with the failure's own status code, not 200. The
    body is the same either way and the client reads the outcome from it, but
    the code is what puts the failure in the access log, which is the only
    place /stats can learn that a background job went wrong.
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
    snapshot = job.snapshot()
    # Taken off the body: it is how this reply is coded, not something the
    # client needs to read.
    code = snapshot.pop("httpStatus", 200)
    return jsonify(snapshot), code


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
