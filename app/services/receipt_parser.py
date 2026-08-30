"""Turn a photo of a receipt into structured line items.

The old pipeline ran Tesseract in the browser and sent us the text. That threw
away the layout, could not read the Chinese sample receipt at all, and made the
phone download several MB of wasm from a CDN before it could start. We send the
image straight to the model instead.

The provider lives behind `parse_receipt_image` so swapping it (e.g. to the
Anthropic API) is a change to this file and nothing else.
"""

import io
import json
import logging
import time
from dataclasses import dataclass

from google import genai
from google.genai import types
from PIL import Image, ImageOps

from ..config import Config

log = logging.getLogger(__name__)

SUPPORTED_MIME = {"image/jpeg", "image/png", "image/webp", "image/heic", "image/heif"}


def _is_transient(exc: Exception) -> bool:
    """Worth retrying: upstream congestion, rate limits, timeouts, and 5xx."""
    status = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    if status in {429, 500, 502, 503, 504}:
        return True
    text = f"{type(exc).__name__} {exc}".lower()
    return any(
        marker in text
        for marker in ("unavailable", "timeout", "timed out", "deadline", "overloaded",
                       "resource_exhausted", "503", "429", "connection")
    )


class ReceiptParseError(Exception):
    """Raised when we cannot turn the image into usable items."""

    def __init__(self, message: str, *, status: int = 502):
        super().__init__(message)
        self.message = message
        self.status = status


# Categories matter to the split, not just the display. Service charge and tax
# have to be re-apportioned across people in proportion to what each person
# actually ate — splitting them evenly is what made the old bills unfair — so
# the model has to tell us which lines are charges rather than food.
RECEIPT_SCHEMA = {
    "type": "object",
    "properties": {
        "currency": {
            "type": "string",
            "description": "ISO 4217 code if determinable, else empty string.",
        },
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "quantity": {
                        "type": "integer",
                        "description": "Units on this line. 1 when not printed.",
                    },
                    "line_total": {
                        "type": "number",
                        "description": "Total for the whole line, not the unit price.",
                    },
                    "category": {
                        "type": "string",
                        "enum": [
                            "item",
                            "service_charge",
                            "tax",
                            "discount",
                            "rounding",
                        ],
                    },
                },
                "required": ["name", "quantity", "line_total", "category"],
            },
        },
        "subtotal": {"type": "number"},
        "total": {
            "type": "number",
            "description": "Grand total as printed. 0 if not shown.",
        },
    },
    "required": ["currency", "items", "subtotal", "total"],
}

SYSTEM_INSTRUCTION = """\
You read restaurant receipts and return structured data. Rules:

- Return every charged line, in the order printed.
- `line_total` is the amount printed for that line — the total for all units on
  it, never the unit price. If only a unit price is printed, multiply it out.
- `quantity` is the number of units on the line; use 1 when none is printed.
- Classify each line: food and drink are "item"; service charge is
  "service_charge"; GST/VAT/sales tax is "tax"; discounts and vouchers are
  "discount" (negative line_total); rounding adjustments are "rounding".
- Sub-items printed under a set or combo with no price of their own are not
  separate lines. Fold their names into the parent, e.g.
  "Cocktail Party for 2 (Pineapple Rum, Ume Dream)".
- Receipts may be in any language. Keep item names in the language printed.
- If a price is smudged or unreadable, use 0 rather than guessing.
- `subtotal` is the pre-charge total; `total` is the grand total as printed.
"""

PROMPT = "Extract every line from this receipt."


@dataclass(frozen=True)
class ParsedReceipt:
    currency: str
    items: list[dict]
    subtotal: float
    total: float
    # Set when the lines don't add up to the printed total, so the UI can warn
    # instead of silently splitting a bill that's already wrong.
    discrepancy: float | None

    def to_dict(self) -> dict:
        return {
            "currency": self.currency,
            "items": self.items,
            "subtotal": self.subtotal,
            "total": self.total,
            "discrepancy": self.discrepancy,
        }


_client: genai.Client | None = None


def _get_client() -> genai.Client:
    """Built on first use, not at import, so a missing key can't crash boot."""
    global _client
    if _client is None:
        if not Config.GEMINI_API_KEY:
            raise ReceiptParseError(
                "Receipt parsing is not configured on this server.", status=503
            )
        _client = genai.Client(
            api_key=Config.GEMINI_API_KEY,
            http_options=types.HttpOptions(
                timeout=Config.GEMINI_TIMEOUT_S * 1000  # milliseconds
            ),
        )
    return _client


def normalise_image(raw: bytes) -> tuple[bytes, str]:
    """Downscale, strip EXIF rotation, and re-encode as JPEG.

    Phone photos arrive at 4000px and often with an EXIF orientation flag that
    the model would otherwise read sideways. Shrinking also cuts the token
    count roughly fourfold with no loss of legibility on a receipt.
    """
    try:
        with Image.open(io.BytesIO(raw)) as img:
            original_format = img.format
            rotated = ImageOps.exif_transpose(img)
            was_rotated = rotated.size != img.size or rotated.tobytes() != img.tobytes()

            img = rotated.convert("RGB")
            before = img.size
            img.thumbnail(
                (Config.MAX_IMAGE_EDGE, Config.MAX_IMAGE_EDGE), Image.LANCZOS
            )
            was_resized = img.size != before

            out = io.BytesIO()
            img.save(out, format="JPEG", quality=85, optimize=True)
            encoded = out.getvalue()
    except Exception as exc:
        raise ReceiptParseError(
            "That file doesn't look like an image we can read.", status=400
        ) from exc

    # Re-encoding an already-small JPEG can come out larger than the original.
    # When there was nothing to fix, send what we were given.
    if (
        not was_resized
        and not was_rotated
        and original_format == "JPEG"
        and len(encoded) >= len(raw)
    ):
        return raw, "image/jpeg"
    return encoded, "image/jpeg"


def _coerce(payload: dict) -> ParsedReceipt:
    """Normalise the model's output into something the UI can trust.

    The schema guarantees the shape but not the arithmetic, so quantities are
    floored at 1 and prices coerced to real numbers — a null or a string here
    used to propagate as NaN through every person's total.
    """
    items = []
    for idx, raw in enumerate(payload.get("items") or []):
        name = str(raw.get("name") or "").strip() or "Unnamed item"
        try:
            line_total = round(float(raw.get("line_total") or 0), 2)
        except (TypeError, ValueError):
            line_total = 0.0
        try:
            quantity = max(1, int(raw.get("quantity") or 1))
        except (TypeError, ValueError):
            quantity = 1
        category = raw.get("category")
        if category not in {"item", "service_charge", "tax", "discount", "rounding"}:
            category = "item"
        items.append(
            {
                "id": f"i{idx}",
                "name": name,
                "quantity": quantity,
                "lineTotal": line_total,
                "category": category,
            }
        )

    def _num(key: str) -> float:
        try:
            return round(float(payload.get(key) or 0), 2)
        except (TypeError, ValueError):
            return 0.0

    total = _num("total")
    summed = round(sum(i["lineTotal"] for i in items), 2)
    # Only meaningful when the receipt actually printed a total to check against.
    discrepancy = round(total - summed, 2) if total else None
    if discrepancy is not None and abs(discrepancy) < 0.01:
        discrepancy = None

    return ParsedReceipt(
        currency=str(payload.get("currency") or "").strip(),
        items=items,
        subtotal=_num("subtotal"),
        total=total,
        discrepancy=discrepancy,
    )


def parse_receipt_image(raw: bytes, mime_type: str) -> ParsedReceipt:
    if mime_type not in SUPPORTED_MIME:
        raise ReceiptParseError(f"Unsupported image type: {mime_type}", status=415)

    image_bytes, image_mime = normalise_image(raw)
    client = _get_client()

    part = types.Part.from_bytes(data=image_bytes, mime_type=image_mime)
    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_INSTRUCTION,
        response_mime_type="application/json",
        response_schema=RECEIPT_SCHEMA,
        temperature=0,
    )

    # The free tier returns 503 UNAVAILABLE under load often enough to see it
    # in a handful of test calls. One of those used to be a failed receipt at
    # the table, so retry transient upstream errors — but against a wall-clock
    # deadline, so retries can never outlast the gunicorn worker timeout.
    deadline = time.monotonic() + Config.GEMINI_DEADLINE_S
    backoff = 1.0
    last_exc: Exception | None = None

    for attempt in range(1, Config.GEMINI_MAX_ATTEMPTS + 1):
        try:
            response = client.models.generate_content(
                model=Config.GEMINI_MODEL, contents=[part, PROMPT], config=config
            )
            break
        except Exception as exc:
            last_exc = exc
            if not _is_transient(exc):
                log.exception("receipt model call failed permanently")
                raise ReceiptParseError(
                    "Couldn't reach the receipt reader. Try again in a moment."
                ) from exc
            remaining = deadline - time.monotonic()
            if attempt >= Config.GEMINI_MAX_ATTEMPTS or remaining <= backoff:
                log.warning(
                    "model unavailable after %d attempt(s): %s", attempt, exc
                )
                raise ReceiptParseError(
                    "The receipt reader is busy right now. Try again in a moment."
                ) from exc
            log.info("attempt %d hit a transient error, retrying: %s", attempt, exc)
            time.sleep(backoff)
            backoff *= 2
    else:  # pragma: no cover - loop always breaks or raises
        raise ReceiptParseError("Couldn't reach the receipt reader.") from last_exc

    text = (response.text or "").strip()
    if not text:
        raise ReceiptParseError("The receipt reader returned nothing. Try again.")

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        # response_schema makes this close to unreachable; kept so a provider
        # change can't turn it into a 500.
        log.error("model returned non-JSON despite schema: %.300s", text)
        raise ReceiptParseError("Couldn't read that receipt. Try a clearer photo.") from exc

    parsed = _coerce(payload)
    if not parsed.items:
        raise ReceiptParseError(
            "No items found on that receipt. Try a clearer, straighter photo.",
            status=422,
        )
    return parsed
