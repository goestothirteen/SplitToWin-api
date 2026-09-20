"""Runtime configuration, read once at startup.

Nothing here raises at import time. The old parser did
`os.environ["GEMINI_API_KEY"]` at module level, so a missing key killed the
gunicorn worker during boot and the platform restarted it forever — a crash
loop that looked exactly like "the backend sometimes fails to start".
Now a missing key is reported by /healthz and turned into a clean 503 on the
one endpoint that needs it, and every other route keeps working.
"""

import os


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


class Config:
    GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
    GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

    # Thinking tokens before it answers. Flash thinks by default, and on a
    # dense bilingual receipt it spent 50 SECONDS thinking before emitting a
    # character — the whole of the timeout problem, in one setting. Measured
    # on that receipt: 80.5s thinking on vs 5.1s off, same nine lines, and
    # the no-thinking read reconciled to the printed total while the thinking
    # one did not. Raise it if a receipt ever needs the reasoning.
    GEMINI_THINKING_BUDGET = _int("GEMINI_THINKING_BUDGET", 0)

    # Thinking for the second look at a receipt whose lines don't add up.
    # -1 lets the model decide how long to think. Only receipts that failed
    # to reconcile ever pay this, which is a small minority of them.
    GEMINI_RECHECK_BUDGET = _int("GEMINI_RECHECK_BUDGET", -1)

    # Hard ceiling on one call to Gemini. No HTTP request waits on this any
    # more — the parse runs as a background job — so it is set by how long a
    # dense bilingual receipt legitimately takes, not by a worker timeout.
    GEMINI_TIMEOUT_S = _int("GEMINI_TIMEOUT_S", 120)

    # Which backend reads the receipt. "claude_code" runs headless Claude
    # Code on this machine against the local subscription login; "gemini"
    # calls the Gemini API. Setting a fallback gives you two independent
    # providers, which is the only real defence against one having a bad
    # minute -- a 503 from the primary is then invisible to the user.
    RECEIPT_PROVIDER = os.environ.get("RECEIPT_PROVIDER", "gemini").strip().lower()
    RECEIPT_FALLBACK_PROVIDER = (
        os.environ.get("RECEIPT_FALLBACK_PROVIDER", "").strip().lower()
    )

    # Retries against transient upstream errors.
    PROVIDER_MAX_ATTEMPTS = _int("PROVIDER_MAX_ATTEMPTS", 2)

    # How long EACH provider gets, attempts included. Previously one deadline
    # was shared across the whole chain, so a slow primary spent all of it and
    # the fallback was handed a second or two — every failover failed
    # instantly. A budget per provider is what makes the backup reader real.
    PROVIDER_BUDGET_S = _int("PROVIDER_BUDGET_S", 120)

    # Receipt photos from phones are 3-12 MB. Anything past this is not a
    # receipt, and refusing it early keeps someone from burning the quota.
    MAX_UPLOAD_BYTES = _int("MAX_UPLOAD_BYTES", 12 * 1024 * 1024)

    # Longest edge the image is downscaled to before upload. Receipts stay
    # legible well below phone-camera resolution, and this cuts both the
    # request size and the token count substantially.
    MAX_IMAGE_EDGE = _int("MAX_IMAGE_EDGE", 1600)

    # Comma-separated origins. Empty means same-origin only, which is the
    # production setup: Caddy serves the UI and the API under one hostname,
    # so there is no cross-origin request to allow.
    CORS_ORIGINS = [
        o.strip() for o in os.environ.get("CORS_ORIGINS", "").split(",") if o.strip()
    ]

    RATE_LIMIT_PER_HOUR = _int("RATE_LIMIT_PER_HOUR", 60)
    LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

    # Caddy's JSON access log, mounted read-only by compose. /stats is built
    # from it. The glob must catch Caddy's rotations too, which are named
    # `split2win-<timestamp>.log.gz`, not `split2win.log.1`.
    ACCESS_LOG_GLOB = os.environ.get(
        "ACCESS_LOG_GLOB", "/var/log/caddy/split2win*.log*"
    ).strip()
    # /stats is an open URL, so this is what stops a scanner making the box
    # re-read the log for every hit.
    STATS_CACHE_S = _int("STATS_CACHE_S", 30)

    @classmethod
    def missing(cls) -> list[str]:
        """Config problems that make receipt parsing impossible."""
        needs_key = "gemini" in {cls.RECEIPT_PROVIDER, cls.RECEIPT_FALLBACK_PROVIDER}
        return ["GEMINI_API_KEY"] if needs_key and not cls.GEMINI_API_KEY else []
