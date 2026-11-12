"""Splitting subtitle entries into batches, with context.

A batch is a run of at most `size` translatable entries. Boundaries only ever
fall between entries, never inside one, and every selected entry appears in
exactly one batch, in file order.

Each batch also carries context the model may read but must not translate:
the entries before it (English plus the Thai already produced, when known) and
the entries after it (English only). Context is drawn from the full entry list,
so a --range run still sees the lines just outside its window.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Collection, Iterable, Mapping, Sequence

log = logging.getLogger(__name__)

_RANGE_RE = re.compile(r"^\s*(\d+)\s*[-–]\s*(\d+)\s*$")


@dataclass(frozen=True)
class SourceEntry:
    """One subtitle entry. `index` is the real SRT index, preserved by srtio."""

    index: int
    text: str


@dataclass(frozen=True)
class ContextLine:
    """A neighbouring entry shown for understanding only.

    `thai` is only ever set for lines before the batch: the translation already
    produced carries tone and pronouns forward.
    """

    index: int
    text: str
    thai: str | None = None


@dataclass
class Batch:
    """One request's worth of work."""

    items: list[SourceEntry] = field(default_factory=list)
    before: list[ContextLine] = field(default_factory=list)
    after: list[ContextLine] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.items)

    @property
    def indexes(self) -> list[int]:
        return [entry.index for entry in self.items]

    @property
    def item_chars(self) -> int:
        """Characters of translatable text; used for cost estimates."""
        return sum(len(entry.text) for entry in self.items)


def parse_range(spec: str | tuple[int, int]) -> tuple[int, int]:
    """Parse "--range 120-180" into (120, 180). Bounds are inclusive."""
    if isinstance(spec, tuple):
        low, high = spec
    else:
        match = _RANGE_RE.match(spec)
        if not match:
            raise ValueError(f"Range must look like A-B, got {spec!r}")
        low, high = int(match.group(1)), int(match.group(2))

    if low > high:
        raise ValueError(f"Range start {low} is after its end {high}")
    return low, high


def select_range(
    entries: Sequence[SourceEntry],
    limit: int | None = None,
    range_: str | tuple[int, int] | None = None,
    indexes: Collection[int] | None = None,
) -> list[SourceEntry]:
    """Pick entries by real SRT index, then by count.

    `range_` is inclusive on both ends. `limit` keeps the first N of whatever
    survives the range, which is what --limit does on its own. `indexes` keeps
    only those entries; --fix uses it to re-translate exactly the flagged ones.
    """
    selected = list(entries)

    if range_ is not None:
        low, high = parse_range(range_)
        selected = [entry for entry in selected if low <= entry.index <= high]

    if indexes is not None:
        wanted = set(indexes)
        selected = [entry for entry in selected if entry.index in wanted]

    if limit is not None:
        if limit < 0:
            raise ValueError(f"Limit must not be negative, got {limit}")
        selected = selected[:limit]

    return selected


def make_batches(
    entries: Sequence[SourceEntry],
    selection: Iterable[SourceEntry] | None = None,
    size: int = 60,
    context_before: int = 4,
    context_after: int = 2,
    translated: Mapping[int, str] | None = None,
) -> list[Batch]:
    """Split `selection` into batches, attaching context from `entries`.

    `entries` is the full, already-cleaned, file-ordered list: the source of
    context. `selection` defaults to all of it; pass the result of
    select_range() to translate only part of a file while still seeing the
    neighbours around it. `translated` maps entry index -> Thai already
    produced, for context-before lines.
    """
    if size < 1:
        raise ValueError(f"Batch size must be at least 1, got {size}")
    if context_before < 0 or context_after < 0:
        raise ValueError("Context counts must not be negative")

    ordered = list(entries)
    position_of = {entry.index: position for position, entry in enumerate(ordered)}
    chosen = ordered if selection is None else list(selection)

    unknown = [entry.index for entry in chosen if entry.index not in position_of]
    if unknown:
        raise ValueError(f"Selected entries are not in the entry list: {unknown}")

    positions = sorted(position_of[entry.index] for entry in chosen)
    if len(set(positions)) != len(positions):
        raise ValueError("Selection contains the same entry more than once")
    translated = translated or {}

    batches: list[Batch] = []
    for start in range(0, len(positions), size):
        chunk = positions[start : start + size]
        first, last = chunk[0], chunk[-1]

        before_positions = range(max(first - context_before, 0), first)
        after_positions = range(last + 1, min(last + context_after, len(ordered) - 1) + 1)

        batch = Batch(
            items=[ordered[position] for position in chunk],
            before=[
                ContextLine(
                    index=ordered[position].index,
                    text=ordered[position].text,
                    thai=translated.get(ordered[position].index),
                )
                for position in before_positions
            ],
            after=[
                ContextLine(index=ordered[position].index, text=ordered[position].text)
                for position in after_positions
            ],
        )
        batches.append(batch)

    log.info(
        "Split %d entr%s into %d batch(es) of up to %d",
        len(positions),
        "y" if len(positions) == 1 else "ies",
        len(batches),
        size,
    )
    return batches
