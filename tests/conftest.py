"""Shared test doubles.

The fake client is the only way tests reach a "model": nothing here touches the
network, and no real API key is ever needed.
"""

from __future__ import annotations

import pytest

from llm import Completion


class FakeClient:
    """Stands in for OpenRouterClient.

    Replies are queued in order; a reply may be a string, a Completion or an
    exception to raise. Every call is recorded for assertions.
    """

    def __init__(self, replies: list | None = None):
        self.replies: list = list(replies or [])
        self.calls: list[list[dict[str, str]]] = []
        self.temperatures: list[float | None] = []

    def queue(self, *replies) -> "FakeClient":
        self.replies.extend(replies)
        return self

    def complete(self, messages, temperature: float | None = None) -> Completion:
        self.calls.append(list(messages))
        self.temperatures.append(temperature)
        if not self.replies:
            raise AssertionError("FakeClient ran out of queued replies")
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        if isinstance(reply, Completion):
            return reply
        return Completion(text=str(reply))

    @property
    def last_messages(self) -> list[dict[str, str]]:
        return self.calls[-1]

    @property
    def call_count(self) -> int:
        return len(self.calls)


@pytest.fixture
def fake_client() -> FakeClient:
    return FakeClient()


@pytest.fixture(autouse=True)
def _no_price_fetch(monkeypatch):
    """No test reaches the network for prices; config prices are used instead."""
    import pricing

    monkeypatch.setattr(pricing, "fetch_price", lambda *args, **kwargs: None)
