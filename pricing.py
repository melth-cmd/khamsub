"""Model prices, asked of the provider instead of hardcoded in config.toml.

A static price table goes stale and cannot follow a provider's own overrides:
OpenRouter's list carries time-of-day prices, so the same model costs twice as
much at some hours. A finished run therefore reports the exact cost the provider
charged (`usage.cost`, see llm.usage_from_response), and this module is only
needed to make the `--dry-run` estimate use the same numbers.

Providers other than OpenRouter publish nothing machine-readable, so the caller
falls back to config.toml, and to no cost at all when that is unset.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

log = logging.getLogger(__name__)

OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
OPENROUTER_HOST = "openrouter.ai"
FETCH_TIMEOUT_S = 5.0


def price_from_payload(payload: object, model: str) -> tuple[float, float] | None:
    """USD per million tokens for `model`, from an OpenRouter /models payload.

    None when the model is absent or its prices cannot be read, which the caller
    treats as "no price known" rather than zero.
    """
    if not isinstance(payload, dict):
        return None
    for entry in payload.get("data") or []:
        if not isinstance(entry, dict) or entry.get("id") != model:
            continue
        pricing = entry.get("pricing") or {}
        try:
            return (
                float(pricing["prompt"]) * 1_000_000,
                float(pricing["completion"]) * 1_000_000,
            )
        except (KeyError, TypeError, ValueError):
            return None
    return None


def fetch_price(
    model: str, base_url: str, timeout: float = FETCH_TIMEOUT_S
) -> tuple[float, float] | None:
    """Ask the provider for `model`'s price, or None if it cannot be had.

    Never raises: an estimate that cannot reach the network falls back to the
    configured prices, and a dry run must not fail because of it.
    """
    if not model or OPENROUTER_HOST not in base_url:
        return None
    try:
        with urllib.request.urlopen(OPENROUTER_MODELS_URL, timeout=timeout) as reply:
            payload = json.load(reply)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        log.info("could not fetch model prices: %s", exc)
        return None
    price = price_from_payload(payload, model)
    if price is None:
        log.info("no published price for %s", model)
    return price
