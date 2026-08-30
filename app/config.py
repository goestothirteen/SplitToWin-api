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
        return [] if cls.GEMINI_API_KEY else ["GEMINI_API_KEY"]
