"""Turn a photo of a receipt into structured line items.

The old pipeline ran Tesseract in the browser and sent us the text. That threw
away the layout, could not read the Chinese sample receipt at all, and made the
phone download several MB of wasm from a CDN before it could start. We send the
image straight to a model instead.

Which model is a deployment choice, not a code change — see `providers/`.
This module owns everything provider-independent: image preparation, the
failover and retry policy, and coercing the reply into something the frontend
can trust.

Each provider gets its *own* time budget rather than sharing one deadline.
Under the old shared deadline a slow primary spent almost all of it before
failing, so the fallback was handed a second or two and failed immediately —
the failover existed on paper and never once rescued a receipt. Nothing holds
an HTTP connection open while this runs any more (see `jobs.py`), so the
budgets can be generous enough for a long bilingual receipt.
"""

import io
import logging
import time
from dataclasses import dataclass

from PIL import Image, ImageOps

from ..config import Config
from . import providers
from .providers.base import (
    NotAReceipt,
    ProviderError,
    noop_report,
    reconcile_prompt,
)

log = logging.getLogger(__name__)

SUPPORTED_MIME = {"image/jpeg", "image/png", "image/webp", "image/heic", "image/heif"}


class ReceiptParseError(Exception):
    """Raised when we cannot turn the image into usable items."""

    def __init__(self, message: str, *, status: int = 502, code: str = "parse_failed"):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


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
    # The model's own account of what it could not read. Shown next to the
    # items, because a bill that is 90% right is worth more than an error —
    # but only if the missing 10% is named.
    warnings: list[str]

    def to_dict(self) -> dict:
        return {
            "currency": self.currency,
            "items": self.items,
            "subtotal": self.subtotal,
            "total": self.total,
            "discrepancy": self.discrepancy,
            "provider": self.provider,
            "warnings": self.warnings,
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
            "That file doesn't look like an image we can read.",
            status=400,
            code="bad_image",
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


def _one_line(value) -> str:
    """Model prose arrives wrapped at the width of the prompt it copied from.
    A newline mid-sentence breaks every alert it lands in."""
    return " ".join(str(value or "").split())


def _warnings(payload: dict) -> list[str]:
    out = []
    for raw in payload.get("warnings") or []:
        text = _one_line(raw)
        if text:
            out.append(text[:200])
    return out[:8]


def _rejected(exc: NotAReceipt) -> "ReceiptParseError":
    return ReceiptParseError(exc.message, status=422, code="not_a_receipt")


def _check_is_receipt(payload: dict) -> None:
    """Honour the model's own refusal.

    Only an explicit `false` counts. A provider that omits the key — an older
    Claude Code reply, say — must not have silence read as a rejection.
    """
    if payload.get("is_receipt") is not False:
        return
    reason = _one_line(payload.get("reject_reason"))
    raise NotAReceipt(
        reason or "That photo doesn't look like a receipt. Try the printed bill."
    )


def _coerce(payload: dict, provider_name: str) -> ParsedReceipt:
    """Normalise the model's output into something the UI can trust.

    Only the Gemini provider gets server-enforced schema; Claude Code returns
    free-form text. Either way the arithmetic is unverified, so quantities are
    floored at 1 and prices coerced to real numbers — a null or a string here
    used to propagate as NaN through every person's total.
    """
    items = []
    for idx, raw in enumerate(payload.get("items") or []):
        # Models sometimes wrap a long name onto two lines. A newline inside
        # a name breaks every row it is rendered in, so names are always
        # collapsed to one line here rather than trusted.
        name = _one_line(raw.get("name")) or "Unnamed item"
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

    warnings = _warnings(payload)
    # A price the model gave up on is the most common reason a bill won't
    # reconcile, and the item list alone doesn't say which line it was.
    unpriced = [i["name"] for i in items if i["lineTotal"] == 0]
    if unpriced and not warnings:
        warnings.append(
            "No price was readable for: " + ", ".join(unpriced[:5]) + "."
        )


    return ParsedReceipt(
        currency=str(payload.get("currency") or "").strip(),
        items=items,
        subtotal=_num("subtotal"),
        total=total,
        discrepancy=discrepancy,
        provider=provider_name,
        warnings=warnings,
    )


def active_providers() -> list[str]:
    chain = providers.resolve_chain(
        Config.RECEIPT_PROVIDER, Config.RECEIPT_FALLBACK_PROVIDER
    )
    return [p.NAME for p in chain]


# What the person waiting should be told each reader is doing. The provider
# name is an implementation detail; "the backup reader" is not.
def _reader_label(index: int) -> str:
    return "the receipt reader" if index == 0 else "the backup reader"


def _reconciled(provider, parsed, payload, image_bytes, image_mime, budget, report):
    """Give a bill that doesn't add up a second look before accepting it.

    A mismatch is nearly always a reading mistake — an add-on counted twice,
    a heading read as an item — and the mistake is usually obvious once the
    sum is held against the printed total. Telling the person "these lines
    don't add up, check them yourself" is the answer of last resort, so this
    spends one more call, with thinking switched on, trying not to have to.

    Only a read that actually reconciles replaces the first one. If the
    second look is no better, the original stands with its warning intact.
    """
    if parsed.discrepancy is None:
        return parsed

    remaining = budget - time.monotonic()
    if remaining <= 5:
        return parsed

    log.info(
        "%s: lines off the printed total by %.2f, looking again",
        provider.NAME,
        parsed.discrepancy,
    )
    report(stage="The lines don't add up — checking the receipt again", items=0)
    try:
        second = provider.parse(
            image_bytes,
            image_mime,
            timeout_s=int(remaining),
            report=report,
            prompt=reconcile_prompt(payload.get("items") or [], _summed(parsed), parsed.total),
            deliberate=True,
        )
    except NotAReceipt:
        raise
    except Exception:
        log.exception("%s: the second look failed, keeping the first read", provider.NAME)
        return parsed

    try:
        _check_is_receipt(second)
    except NotAReceipt:
        return parsed

    fixed = _coerce(second, provider.NAME)
    if fixed.items and fixed.discrepancy is None:
        log.info("%s: the second look reconciled", provider.NAME)
        return fixed
    return parsed


def _summed(parsed: ParsedReceipt) -> float:
    return round(sum(i["lineTotal"] for i in parsed.items), 2)


def parse_receipt_image(raw: bytes, mime_type: str, report=noop_report) -> ParsedReceipt:
    """Read the receipt, narrating progress through `report`.

    `report` is called with keyword arguments the job layer understands:
    `stage` (a sentence for the person waiting), `detail`, and `items` (how
    many lines have been read so far). It must never raise.
    """
    if mime_type not in SUPPORTED_MIME:
        raise ReceiptParseError(
            f"Unsupported image type: {mime_type}", status=415, code="unsupported_type"
        )

    chain = providers.resolve_chain(
        Config.RECEIPT_PROVIDER, Config.RECEIPT_FALLBACK_PROVIDER
    )
    if not chain:
        raise ReceiptParseError(
            "Receipt parsing is not configured on this server.",
            status=503,
            code="not_configured",
        )

    report(stage="Getting the photo ready")
    image_bytes, image_mime = normalise_image(raw)

    last: ProviderError | None = None

    for index, provider in enumerate(chain):
        label = _reader_label(index)
        if index > 0:
            log.warning("failing over to %s", provider.NAME)
            report(stage="First reader gave up — trying the backup", items=0)

        # Each provider gets a fresh budget. Attempts within one provider
        # share it, so a retry loop still cannot run forever.
        budget = time.monotonic() + Config.PROVIDER_BUDGET_S
        backoff = 1.0

        for attempt in range(1, Config.PROVIDER_MAX_ATTEMPTS + 1):
            remaining = budget - time.monotonic()
            if remaining <= 1:
                break
            report(
                stage=(
                    f"Reading the receipt with {label}"
                    if attempt == 1
                    else f"Trying {label} again"
                ),
                items=0,
            )
            try:
                payload = provider.parse(
                    image_bytes, image_mime, timeout_s=int(remaining), report=report
                )
            except NotAReceipt as exc:
                # The model looked and said this is not a bill. Asking a second
                # reader the same question wastes half a minute to be told the
                # same thing, so this ends the whole attempt.
                raise _rejected(exc) from exc
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
            except Exception:  # a provider bug must not become a 500
                log.exception("provider %s raised unexpectedly", provider.NAME)
                last = ProviderError("Couldn't read that receipt.", transient=True)
                break

            report(stage="Checking the lines add up")
            try:
                _check_is_receipt(payload)
            except NotAReceipt as exc:
                raise _rejected(exc) from exc
            parsed = _coerce(payload, provider.NAME)
            if not parsed.items:
                # It said it was a receipt and then listed nothing. That is a
                # bad read of a real bill, not a rejection, so it is worth
                # letting the other reader try.
                last = ProviderError(
                    "No line items could be read off that receipt. "
                    "Try a straighter photo with the whole bill in frame.",
                    status=422,
                    transient=True,
                )
                break
            parsed = _reconciled(provider, parsed, payload, image_bytes,
                                 image_mime, budget, report)
            if index > 0:
                log.warning("served by fallback provider %s", provider.NAME)
            return parsed

    if isinstance(last, ProviderError):
        raise ReceiptParseError(
            last.message,
            status=last.status,
            code="no_items" if last.status == 422 else "parse_failed",
        )
    raise ReceiptParseError("Couldn't read that receipt. Try again.")
