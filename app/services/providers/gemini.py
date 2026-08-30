"""Receipt parsing via the Gemini API.

Unlike the Claude Code provider this gets server-enforced structured output
(`response_schema`), so the shape of the reply is guaranteed rather than
unwrapped and hoped for.
"""

from google import genai
from google.genai import types

from ...config import Config
from .base import PROMPT, RECEIPT_SCHEMA, SYSTEM_INSTRUCTION, ProviderError, log

NAME = "gemini"

_client: genai.Client | None = None

_TRANSIENT_MARKERS = (
    "unavailable",
    "timeout",
    "timed out",
    "deadline",
    "overloaded",
    "resource_exhausted",
    "503",
    "429",
    "500",
    "502",
    "504",
    "connection",
)


def available() -> bool:
    return bool(Config.GEMINI_API_KEY)


def _get_client() -> genai.Client:
    """Built on first use, not at import, so a missing key can't crash boot."""
    global _client
    if _client is None:
        if not Config.GEMINI_API_KEY:
            raise ProviderError(
                "Receipt parsing is not configured on this server.", status=503
            )
        _client = genai.Client(
            api_key=Config.GEMINI_API_KEY,
            http_options=types.HttpOptions(timeout=Config.GEMINI_TIMEOUT_S * 1000),
        )
    return _client


def _is_transient(exc: Exception) -> bool:
    status = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    if status in {429, 500, 502, 503, 504}:
        return True
    text = f"{type(exc).__name__} {exc}".lower()
    return any(marker in text for marker in _TRANSIENT_MARKERS)


def parse(image_bytes: bytes, mime_type: str, timeout_s: int) -> dict:
    client = _get_client()
    try:
        response = client.models.generate_content(
            model=Config.GEMINI_MODEL,
            contents=[
                types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
                PROMPT,
            ],
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_INSTRUCTION,
                response_mime_type="application/json",
                response_schema=RECEIPT_SCHEMA,
                temperature=0,
            ),
        )
    except Exception as exc:
        transient = _is_transient(exc)
        log.log(
            20 if transient else 40, "gemini call failed (transient=%s): %s", transient, exc
        )
        raise ProviderError(
            "The receipt reader is busy right now. Try again in a moment."
            if transient
            else "Couldn't reach the receipt reader. Try again in a moment.",
            transient=transient,
        ) from exc

    text = (response.text or "").strip()
    if not text:
        raise ProviderError("The receipt reader returned nothing.", transient=True)

    import json

    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        # response_schema makes this close to unreachable; kept so a provider
        # change can't turn it into a 500.
        log.error("gemini returned non-JSON despite schema: %.300s", text)
        raise ProviderError("Couldn't read that receipt. Try a clearer photo.") from exc
