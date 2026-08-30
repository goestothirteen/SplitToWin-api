"""Turn a photo of a receipt into structured line items.

The old pipeline ran Tesseract in the browser and sent us the text. That threw
away the layout, could not read the Chinese sample receipt at all, and made the
phone download several MB of wasm from a CDN before it could start. We send the
image straight to a model instead.

Which model is a deployment choice, not a code change — see `providers/`.
This module owns everything provider-independent: image preparation, the
failover and retry policy, and coercing the reply into something the frontend
can trust.
"""

import io
import logging
import time
from dataclasses import dataclass

from PIL import Image, ImageOps

from ..config import Config
from . import providers
from .providers.base import ProviderError

log = logging.getLogger(__name__)

SUPPORTED_MIME = {"image/jpeg", "image/png", "image/webp", "image/heic", "image/heif"}


class ReceiptParseError(Exception):
    """Raised when we cannot turn the image into usable items."""

    def __init__(self, message: str, *, status: int = 502):
        super().__init__(message)
        self.message = message
        self.status = status


@dataclass(frozen=True)
class ParsedReceipt:
    currency: str
    items: list[dict]
    subtotal: float
    total: float
    # Set when the lines don't add up to the printed total, so the UI can warn
    # instead of silently splitting a bill that's already wrong.
    discrepancy: float | None
    provider: str

    def to_dict(self) -> dict:
        return {
            "currency": self.currency,
            "items": self.items,
            "subtotal": self.subtotal,
            "total": self.total,
            "discrepancy": self.discrepancy,
            "provider": self.provider,
        }


def normalise_image(raw: bytes) -> tuple[bytes, str]:
    """Downscale, strip EXIF rotation, and re-encode as JPEG.

    Phone photos arrive at 4000px and often with an EXIF orientation flag that
    the model would otherwise read sideways. Shrinking a real phone photo took
    it from 961KB to 149KB in testing, which cuts both upload time and the
    token count with no loss of legibility on a receipt.
    """
    try:
        with Image.open(io.BytesIO(raw)) as img:
            original_format = img.format
            rotated = ImageOps.exif_transpose(img)
            was_rotated = rotated.size != img.size or rotated.tobytes() != img.tobytes()

            img = rotated.convert("RGB")
            before = img.size
            img.thumbnail((Config.MAX_IMAGE_EDGE, Config.MAX_IMAGE_EDGE), Image.LANCZOS)
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


def _coerce(payload: dict, provider_name: str) -> ParsedReceipt:
    """Normalise the model's output into something the UI can trust.

    Only the Gemini provider gets server-enforced schema; Claude Code returns
    free-form text. Either way the arithmetic is unverified, so quantities are
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
        provider=provider_name,
    )


def active_providers() -> list[str]:
    chain = providers.resolve_chain(
        Config.RECEIPT_PROVIDER, Config.RECEIPT_FALLBACK_PROVIDER
    )
    return [p.NAME for p in chain]


def parse_receipt_image(raw: bytes, mime_type: str) -> ParsedReceipt:
    if mime_type not in SUPPORTED_MIME:
        raise ReceiptParseError(f"Unsupported image type: {mime_type}", status=415)

    chain = providers.resolve_chain(
        Config.RECEIPT_PROVIDER, Config.RECEIPT_FALLBACK_PROVIDER
    )
    if not chain:
        raise ReceiptParseError(
            "Receipt parsing is not configured on this server.", status=503
        )

    image_bytes, image_mime = normalise_image(raw)

    # Every attempt across every provider shares one wall-clock deadline, so a
    # retry storm can never outlive the gunicorn worker timeout and turn a slow
    # parse into a SIGKILLed worker and a 502.
    deadline = time.monotonic() + Config.PARSE_DEADLINE_S
    last: ProviderError | None = None

    for provider in chain:
        backoff = 1.0
        for attempt in range(1, Config.PROVIDER_MAX_ATTEMPTS + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 1:
                break
            try:
                payload = provider.parse(
                    image_bytes, image_mime, timeout_s=int(remaining)
                )
            except ProviderError as exc:
                last = exc
                if not exc.transient:
                    break  # a permanent failure won't improve on retry
                if attempt < Config.PROVIDER_MAX_ATTEMPTS and remaining > backoff + 1:
                    log.info(
                        "%s attempt %d was transient, retrying", provider.NAME, attempt
                    )
                    time.sleep(backoff)
                    backoff *= 2
                    continue
                break
            except Exception as exc:  # a provider bug must not become a 500
                log.exception("provider %s raised unexpectedly", provider.NAME)
                last = ProviderError("Couldn't read that receipt.", transient=True)
                break

            parsed = _coerce(payload, provider.NAME)
            if not parsed.items:
                last = ProviderError(
                    "No items found on that receipt. Try a clearer, straighter photo.",
                    status=422,
                )
                break
            if provider is not chain[0]:
                log.warning("served by fallback provider %s", provider.NAME)
            return parsed

        if len(chain) > 1 and provider is not chain[-1]:
            log.warning("provider %s exhausted, failing over", provider.NAME)

    if last is not None:
        raise ReceiptParseError(last.message, status=last.status)
    raise ReceiptParseError("Couldn't read that receipt. Try again.")
