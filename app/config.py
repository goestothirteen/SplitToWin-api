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

    # Hard ceiling on how long we'll wait for the model. Must stay comfortably
    # under the gunicorn worker timeout or the worker gets SIGKILLed mid-request
    # and the client sees a 502 instead of a useful error.
    GEMINI_TIMEOUT_S = _int("GEMINI_TIMEOUT_S", 45)

    # Which backend reads the receipt. "claude_code" runs headless Claude
    # Code on this machine against the local subscription login; "gemini"
    # calls the Gemini API. Setting a fallback gives you two independent
    # providers, which is the only real defence against one having a bad
    # minute -- a 503 from the primary is then invisible to the user.
    RECEIPT_PROVIDER = os.environ.get("RECEIPT_PROVIDER", "gemini").strip().lower()
    RECEIPT_FALLBACK_PROVIDER = (
        os.environ.get("RECEIPT_FALLBACK_PROVIDER", "").strip().lower()
    )

    # Retries against transient upstream errors. The deadline bounds every
    # attempt across every provider together, so a retry storm can never
    # outlive the gunicorn worker timeout and turn a slow parse into a 502.
    PROVIDER_MAX_ATTEMPTS = _int("PROVIDER_MAX_ATTEMPTS", 2)
    PARSE_DEADLINE_S = _int("PARSE_DEADLINE_S", 55)

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

    @classmethod
    def missing(cls) -> list[str]:
        """Config problems that make receipt parsing impossible."""
        needs_key = "gemini" in {cls.RECEIPT_PROVIDER, cls.RECEIPT_FALLBACK_PROVIDER}
        return ["GEMINI_API_KEY"] if needs_key and not cls.GEMINI_API_KEY else []
