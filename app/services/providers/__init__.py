"""Provider registry.

RECEIPT_PROVIDER picks the primary; RECEIPT_FALLBACK_PROVIDER (if set and
actually usable) covers it. Two independent providers is the only real answer
to "the upstream had a bad minute" — a 503 from one is invisible if the other
answers.
"""

from . import claude_code, gemini
from .base import ProviderError, log

_REGISTRY = {claude_code.NAME: claude_code, gemini.NAME: gemini}


def get(name: str):
    provider = _REGISTRY.get((name or "").strip().lower())
    if provider is None:
        raise ProviderError(
            f"Unknown receipt provider: {name!r}. "
            f"Expected one of: {', '.join(sorted(_REGISTRY))}.",
            status=503,
        )
    return provider


def resolve_chain(primary: str, fallback: str = "") -> list:
    """Ordered providers to try. Unusable ones are dropped with a warning."""
    chain = []
    for name in (primary, fallback):
        if not name:
            continue
        try:
            provider = get(name)
        except ProviderError as exc:
            log.warning("%s", exc.message)
            continue
        if not provider.available():
            log.warning(
                "provider %r is configured but not usable here, skipping", provider.NAME
            )
            continue
        if provider not in chain:
            chain.append(provider)
    return chain


def names() -> list[str]:
    return sorted(_REGISTRY)


__all__ = ["get", "resolve_chain", "names", "ProviderError"]
