from __future__ import annotations

import pytest

import pricing

PAYLOAD = {
    "data": [
        {
            "id": "other/model",
            "pricing": {"prompt": "0.000001", "completion": "0.000002"},
        },
        {
            "id": "deepseek/x",
            "pricing": {"prompt": "0.00000015", "completion": "0.0000006"},
        },
    ]
}


# --- reading the payload ----------------------------------------------------


def test_a_price_is_reported_per_million_tokens():
    price = pricing.price_from_payload(PAYLOAD, "deepseek/x")
    assert price == pytest.approx((0.15, 0.60))


def test_only_the_asked_for_model_is_read():
    price = pricing.price_from_payload(PAYLOAD, "other/model")
    assert price == pytest.approx((1.0, 2.0))


def test_an_unknown_model_has_no_price():
    assert pricing.price_from_payload(PAYLOAD, "nope/nope") is None


def test_a_payload_that_is_not_a_mapping_has_no_price():
    assert pricing.price_from_payload(None, "deepseek/x") is None
    assert pricing.price_from_payload([], "deepseek/x") is None


def test_an_entry_without_prices_has_no_price():
    payload = {"data": [{"id": "deepseek/x", "pricing": {}}]}
    assert pricing.price_from_payload(payload, "deepseek/x") is None


def test_a_price_that_is_not_a_number_has_no_price():
    payload = {"data": [{"id": "deepseek/x", "pricing": {"prompt": "free"}}]}
    assert pricing.price_from_payload(payload, "deepseek/x") is None


# --- fetching ---------------------------------------------------------------


def test_a_non_openrouter_endpoint_is_never_asked():
    assert pricing.fetch_price("deepseek/x", "https://api.example.com/v1") is None


def test_an_empty_model_is_never_asked():
    assert pricing.fetch_price("", "https://openrouter.ai/api/v1") is None
