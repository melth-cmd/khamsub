"""Small builders shared by tests."""

from __future__ import annotations

from khamsub.batcher import Batch, SourceEntry, make_batches


def make_batch(count: int = 3, start: int = 1, size: int | None = None) -> Batch:
    """A single batch of `count` entries, numbered from `start`."""
    entries = [SourceEntry(start + i, f"Line {start + i}") for i in range(count)]
    return make_batches(entries, size=size or count)[0]
