"""HTTP surface.

Every failure leaves here as JSON. The old app let exceptions escape into
Werkzeug's HTML error page, so the browser's `response.json()` threw while
parsing the error and the user saw a generic alert with the real cause lost.
"""

import logging
import time
from collections import defaultdict, deque

from flask import Blueprint, current_app, jsonify, request

from .config import Config
from .services.jobs import cache as job_cache
from .services.receipt_parser import (
    ReceiptParseError,
    active_providers,
    parse_receipt_image,
)

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


@api.post("/parse-receipt")
def parse_receipt():
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

    started = time.monotonic()

    # Phones drop the connection when backgrounded, so the client retries with
    # the same job id. Reusing the result means a retry costs nothing and
    # returns instantly, instead of parsing the same receipt twice.
    job_id = (request.form.get("jobId") or "").strip()[:64]

    if job_id:
        is_owner, entry = job_cache.claim(job_id)
        if not is_owner:
            if entry.result is not None:
                log.info("job %s already parsed, serving cached result", job_id)
                return jsonify(entry.result)
            # Someone else is mid-parse: wait for them rather than starting a
            # second one. The wait is bounded well under the worker timeout.
            log.info("job %s already in flight, waiting", job_id)
            result, error, timed_out = job_cache.wait(entry, Config.PARSE_DEADLINE_S)
            if timed_out:
                return (
                    jsonify(
                        {
                            "error": "Still reading that receipt. Try again in a moment.",
                            "code": "in_progress",
                        }
                    ),
                    503,
                )
            if error is not None:
                return (
                    jsonify({"error": str(error), "code": "parse_failed"}),
                    getattr(error, "status", 502),
                )
            return jsonify(result)

    try:
        parsed = parse_receipt_image(raw, upload.mimetype or "image/jpeg")
    except ReceiptParseError as exc:
        log.warning(
            "parse failed status=%s reason=%s bytes=%d",
            exc.status,
            exc.message,
            len(raw),
        )
        if job_id:
            job_cache.finish(job_id, None, exc)
        return jsonify({"error": exc.message, "code": "parse_failed"}), exc.status

    payload = parsed.to_dict()
    if job_id:
        job_cache.finish(job_id, payload, None)

    log.info(
        "parsed receipt items=%d bytes=%d ms=%d job=%s",
        len(parsed.items),
        len(raw),
        int((time.monotonic() - started) * 1000),
        job_id or "-",
    )
    return jsonify(payload)


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
