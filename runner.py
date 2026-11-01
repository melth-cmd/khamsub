"""Translate one batch, validating each reply and retrying what failed.

Only the entries that actually failed are re-sent, so one Chinese leak in a
60-entry batch costs one small extra call rather than a full re-translation.

When the retry budget runs out the entry is left in English and marked ok=False
with a reason. English is a better failure than the wrong language: it is
visibly wrong and easy to fix by hand. Nothing here writes the report; it only
produces the data for it.

Sleep and randomness are injected so tests can assert delays without waiting.
"""

from __future__ import annotations

import logging
import math
import random
import time
from dataclasses import dataclass, field
from typing import Callable, Mapping, Protocol, Sequence

import llm
from batcher import Batch, SourceEntry
from llm import Completion, Issue, Usage

log = logging.getLogger(__name__)


@dataclass
class EntryResult:
    """What happened to one entry."""

    index: int
    text: str  # Thai on success, the original English on failure
    ok: bool = True
    reason: str | None = None
    attempts: int = 0
    from_cache: bool = False
    rewrapped: str | None = None  # "dash" | "proportional" | "extra_lines" | "unrepaired"
    dashes_restored: bool = False  # a speaker dash the model dropped, put back


@dataclass
class BatchResult:
    """What happened to one batch."""

    entries: list[EntryResult] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    calls: int = 0

    @property
    def failures(self) -> list[EntryResult]:
        return [entry for entry in self.entries if not entry.ok]

    def as_dict(self) -> dict[int, str]:
        return {entry.index: entry.text for entry in self.entries}


def _backoff(
    attempt: int,
    base_delay: float,
    max_delay: float,
    jitter: float,
    rand: Callable[[], float],
) -> float:
    """Exponential backoff with ±jitter, never longer than max_delay."""
    raw = min(base_delay * (2**attempt), max_delay)
    return max(0.0, raw * (1 + jitter * (2 * rand() - 1)))


def _wait_for(
    exc: llm.RetryableError,
    attempt: int,
    base_delay: float,
    max_delay: float,
    jitter: float,
    max_retry_after: float,
    rand: Callable[[], float],
) -> float:
    """How long to wait before retrying after a 429, 5xx or timeout.

    A usable Retry-After wins over backoff, because only the server knows. It is
    still capped, so a bad hint cannot stall the run. An absurd or malformed
    value falls back to jittered exponential backoff.
    """
    hint = exc.retry_after
    if hint is not None and math.isfinite(hint) and hint >= 0:
        return min(hint, max_retry_after)
    return _backoff(attempt, base_delay, max_delay, jitter, rand)


def _add_usage(total: Usage, extra: Usage) -> Usage:
    return total + extra


def _mark_failed(
    result: BatchResult,
    batch: Batch,
    pending: Mapping[int, SourceEntry],
    attempts: Mapping[int, int],
    reasons: Mapping[int, str],
    fallback_reason: str,
) -> None:
    for index in pending:
        result.entries.append(
            EntryResult(
                index=index,
                text=pending[index].text,  # keep the English
                ok=False,
                reason=reasons.get(index, fallback_reason),
                attempts=attempts.get(index, 0),
            )
        )


def translate_batch(
    client: llm.ChatClient,
    batch: Batch,
    bible: str = "",
    glossary: str = "",
    max_retries: int = 3,
    retry_temperature: float = 0.1,
    base_delay: float = 2.0,
    max_delay: float = 30.0,
    jitter: float = 0.25,
    max_retry_after: float = 120.0,
    strict: bool = False,
    current: Mapping[int, str] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    rand: Callable[[], float] = random.random,
) -> BatchResult:
    """Translate `batch`, retrying failures up to `max_retries` times.

    The retry budget is shared: a validation failure, a 429 or a timeout all
    consume one retry. The first call uses the client's own temperature; every
    retry uses `retry_temperature` to make a repeat less likely.

    `strict` adds the correction rules to the system prompt; --fix uses it for
    lines the verification pass flagged. `current` shows the existing translation
    and switches to the review rules; --review uses it and writes suggestions.
    """
    result = BatchResult()
    if not batch.items:
        return result

    position = {entry.index: order for order, entry in enumerate(batch.items)}
    pending: dict[int, SourceEntry] = {entry.index: entry for entry in batch.items}
    attempts: dict[int, int] = {entry.index: 0 for entry in batch.items}
    reasons: dict[int, str] = {}
    retries_used = 0

    while pending:
        sub_batch = Batch(
            items=[entry for entry in batch.items if entry.index in pending],
            before=batch.before,
            after=batch.after,
        )
        messages = llm.build_messages(
            sub_batch, bible, glossary, strict=strict, current=current
        )
        temperature = None if retries_used == 0 else retry_temperature
        result.calls += 1

        try:
            completion = client.complete(messages, temperature=temperature)
        except llm.RetryableError as exc:
            # 429, 5xx, timeout, connection loss: the same shared budget as a
            # validation failure, so one bad entry cannot spin forever.
            reason = "rate_limited" if isinstance(exc, llm.RateLimited) else "transport"
            for index in pending:
                attempts[index] += 1
            if retries_used >= max_retries:
                log.error("giving up after %s: %s", reason, exc)
                _mark_failed(result, batch, pending, attempts, reasons, reason)
                break
            delay = _wait_for(
                exc, retries_used, base_delay, max_delay, jitter, max_retry_after, rand
            )
            log.warning("%s, retrying in %.1fs", reason, delay)
            sleep(delay)
            retries_used += 1
            continue

        result.usage = _add_usage(result.usage, completion.usage)
        for index in pending:
            attempts[index] += 1

        try:
            decoded = llm.decode_entries(completion.text)
            issues = llm.validate(sub_batch, decoded)
        except llm.BadResponse as exc:
            decoded = llm.Decoded()
            issues = [Issue("bad_response", None, str(exc))]

        broken = {issue.index for issue in issues if issue.index is not None}
        for index in list(pending):
            if index in decoded.entries and index not in broken:
                result.entries.append(
                    EntryResult(
                        index=index,
                        text=decoded.entries[index].strip(),
                        ok=True,
                        attempts=attempts[index],
                    )
                )
                pending.pop(index)

        for issue in issues:
            if issue.index in pending:
                reasons[issue.index] = issue.kind
            elif issue.index is None:
                for index in pending:
                    reasons[index] = issue.kind

        if not pending:
            break

        log.warning(
            "%d entr%s still failing (%s)",
            len(pending),
            "y" if len(pending) == 1 else "ies",
            ", ".join(sorted({reasons.get(i, "unknown") for i in pending})),
        )

        if retries_used >= max_retries:
            _mark_failed(result, batch, pending, attempts, reasons, "unknown")
            break
        retries_used += 1

    result.entries.sort(key=lambda entry: position[entry.index])
    return result
