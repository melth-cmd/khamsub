"""Time alignment between an English .srt and a Thai .srt.

The two files usually come from the same release, so cue times match. When they
do not, the difference is one of:

  - a constant offset (a re-timed release, PAL/NTSC, an added intro);
  - a few cues added or removed (a different cut);
  - progressive drift (different frame rates);
  - one cue split in two, or two merged.

Only time is compared: the Thai and English words cannot be matched against each
other. The module is deliberately conservative. A wrong pair would later rewrite
a good Thai line from unrelated dialogue, so anything it is not sure about is
returned as unmatched or ambiguous, and reported instead of guessed.

The result is geometry only -- cue indexes, an offset, coverage and a verdict.
`repair.py` decides what to do with it.
"""

from __future__ import annotations

import datetime as dt
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Protocol, Sequence

# Two cues within these gaps are the same line. Exact timestamps are common;
# a re-timed release often differs by a small amount.
NEAR_START = 0.5  # seconds
NEAR_END = 0.5

# A "close" pair needs this much of the shorter cue in common, and a duration
# ratio inside these bounds, so a stub inside a long line is not mistaken for a
# split.
MIN_OVERLAP = 0.5
MIN_RATIO = 0.5
MAX_RATIO = 2.0

# Look this many seconds around a Thai cue for candidates.
WINDOW = 5.0

# Offset histogram: range, bin size, candidates kept.
OFFSET_RANGE = 30.0
OFFSET_STEP = 0.5
MAX_OFFSETS = 5

# A file needs this share of its Thai cues paired before it is trusted.
MIN_COVERAGE = 0.60

# Residual spread (p10 to p90) that means the timestamps drift.
DRIFT_SPREAD = 1.5
DRIFT_MIN_PAIRS = 10


class Timed(Protocol):
    """What alignment needs: a cue index and two timestamps."""

    index: int
    start: dt.timedelta
    end: dt.timedelta


@dataclass(frozen=True)
class Pair:
    thai: int
    english: int
    confidence: str  # "exact" or "close"


@dataclass(frozen=True)
class Alignment:
    pairs: tuple[Pair, ...] = ()
    unmatched_thai: tuple[int, ...] = ()
    unmatched_english: tuple[int, ...] = ()
    ambiguous_thai: tuple[int, ...] = ()
    offset: dt.timedelta = dt.timedelta(0)
    coverage: float = 0.0
    quality: str = "poor"  # "good" or "poor"
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.quality == "good"

    @property
    def source_by_thai(self) -> dict[int, int]:
        """Thai cue index -> English cue index, for confident pairs only."""
        return {pair.thai: pair.english for pair in self.pairs}


@dataclass(frozen=True)
class _Attempt:
    offset: dt.timedelta
    pairs: tuple[Pair, ...]
    ambiguous: tuple[int, ...]
    # Thai cues that saw at least one plausible English cue, paired or not.
    # Ranked first: a shifted offset that "fixes" a split by matching only one
    # half must never beat the true offset, where both halves are ambiguous.
    support: int = 0

    @property
    def rank(self) -> tuple[int, int, int, float]:
        """More support, then more pairs, then fewer ambiguous cues, then offset."""
        return (
            self.support,
            len(self.pairs),
            -len(self.ambiguous),
            -abs(self.offset.total_seconds()),
        )


def align(english: Sequence[Timed], thai: Sequence[Timed]) -> Alignment:
    """Pair Thai cues with English cues by time. Never by text."""
    english = sorted(english, key=lambda cue: cue.start)
    thai = sorted(thai, key=lambda cue: cue.start)
    if not english or not thai:
        return Alignment(
            unmatched_thai=tuple(cue.index for cue in thai),
            unmatched_english=tuple(cue.index for cue in english),
            reason="no cues to align",
        )

    best = _pair(english, thai, dt.timedelta(0))
    for offset in _offset_candidates(english, thai):
        attempt = _pair(english, thai, offset)
        if attempt.rank > best.rank:
            best = attempt
    best = _refine(english, thai, best)

    by_thai = {cue.index: cue for cue in thai}
    by_english = {cue.index: cue for cue in english}
    paired_thai = {pair.thai for pair in best.pairs}
    paired_english = {pair.english for pair in best.pairs}
    ambiguous = set(best.ambiguous)

    coverage = len(best.pairs) / len(thai)
    quality, reason = "good", "aligned"
    if coverage < MIN_COVERAGE:
        quality, reason = "poor", f"low coverage ({coverage:.0%})"
    elif _drifts(best.pairs, by_thai, by_english):
        quality, reason = "poor", "timestamps drift"

    return Alignment(
        pairs=best.pairs,
        unmatched_thai=tuple(
            cue.index
            for cue in thai
            if cue.index not in paired_thai and cue.index not in ambiguous
        ),
        unmatched_english=tuple(
            cue.index for cue in english if cue.index not in paired_english
        ),
        ambiguous_thai=tuple(cue.index for cue in thai if cue.index in ambiguous),
        offset=best.offset,
        coverage=coverage,
        quality=quality,
        reason=reason,
    )


def _offset_candidates(
    english: Sequence[Timed], thai: Sequence[Timed]
) -> list[dt.timedelta]:
    """Likely constant shifts, most common first, with 0 always included."""
    starts = [cue.start for cue in english]
    counts: Counter[int] = Counter()
    for cue in thai:
        delta = (cue.start - _nearest(starts, cue.start)).total_seconds()
        if abs(delta) <= OFFSET_RANGE:
            counts[round(delta / OFFSET_STEP)] += 1

    offsets = [
        dt.timedelta(seconds=bucket * OFFSET_STEP)
        for bucket, _ in counts.most_common(MAX_OFFSETS)
    ]
    if all(abs(offset.total_seconds()) > 1e-9 for offset in offsets):
        offsets.append(dt.timedelta(0))
    return offsets


def _nearest(starts: Sequence[dt.timedelta], when: dt.timedelta) -> dt.timedelta:
    at = bisect_left(starts, when)
    options = []
    if at:
        options.append(starts[at - 1])
    if at < len(starts):
        options.append(starts[at])
    return min(options, key=lambda start: abs(start - when))


def _pair(
    english: Sequence[Timed], thai: Sequence[Timed], offset: dt.timedelta
) -> _Attempt:
    """One-to-one pairs under `offset`; anything shared is ambiguous."""
    shifted = [
        (cue.index, cue.start + offset, cue.end + offset) for cue in english
    ]
    starts = [start for _, start, _ in shifted]

    found_by_thai: dict[int, list[tuple[str, int]]] = {}
    for cue in thai:
        low = cue.start - dt.timedelta(seconds=WINDOW)
        high = cue.start + dt.timedelta(seconds=WINDOW)
        lo = bisect_left(starts, low)
        hi = bisect_right(starts, high)
        found = []
        for position in range(lo, hi):
            index, start, end = shifted[position]
            confidence = _confidence(cue, start, end)
            if confidence is not None:
                found.append((confidence, index))
        if found:
            found_by_thai[cue.index] = found

    by_english: dict[int, list[int]] = defaultdict(list)
    for thai_index, found in found_by_thai.items():
        for _, english_index in found:
            by_english[english_index].append(thai_index)

    ambiguous = {
        thai_index for thai_index, found in found_by_thai.items() if len(found) > 1
    }
    ambiguous.update(
        thai_index
        for thai_indexes in by_english.values()
        if len(thai_indexes) > 1
        for thai_index in thai_indexes
    )

    pairs: list[Pair] = []
    for cue in thai:
        found = found_by_thai.get(cue.index, [])
        if cue.index in ambiguous or len(found) != 1:
            continue
        confidence, english_index = found[0]
        if len(by_english[english_index]) != 1:
            continue
        pairs.append(Pair(cue.index, english_index, confidence))

    return _Attempt(
        offset=offset,
        pairs=tuple(pairs),
        ambiguous=tuple(cue.index for cue in thai if cue.index in ambiguous),
        support=len(found_by_thai),
    )


def _confidence(cue: Timed, start: dt.timedelta, end: dt.timedelta) -> str | None:
    """How sure the shifted English cue is the same line, or None."""
    start_gap = abs((start - cue.start).total_seconds())
    end_gap = abs((end - cue.end).total_seconds())
    if start_gap <= NEAR_START and end_gap <= NEAR_END:
        return "exact"

    thai_length = (cue.end - cue.start).total_seconds()
    english_length = (end - start).total_seconds()
    if thai_length <= 0 or english_length <= 0:
        return None
    ratio = thai_length / english_length
    if ratio < MIN_RATIO or ratio > MAX_RATIO:
        return None

    overlap = (min(cue.end, end) - max(cue.start, start)).total_seconds()
    if overlap <= 0:
        return None
    if overlap / min(thai_length, english_length) < MIN_OVERLAP:
        return None
    return "close"


def _refine(
    english: Sequence[Timed], thai: Sequence[Timed], attempt: _Attempt
) -> _Attempt:
    """Use the median residual of the pairs as a decimal offset, if it helps."""
    by_thai = {cue.index: cue for cue in thai}
    by_english = {cue.index: cue for cue in english}
    residuals = sorted(
        (by_thai[pair.thai].start - by_english[pair.english].start).total_seconds()
        for pair in attempt.pairs
    )
    if not residuals:
        return attempt
    median = residuals[len(residuals) // 2]
    refined = _pair(english, thai, dt.timedelta(seconds=median))
    return refined if refined.rank >= attempt.rank else attempt


def _drifts(
    pairs: Sequence[Pair],
    by_thai: dict[int, Timed],
    by_english: dict[int, Timed],
) -> bool:
    """True when the residual spread says the timestamps are not a constant shift."""
    if len(pairs) < DRIFT_MIN_PAIRS:
        return False
    residuals = sorted(
        (by_thai[pair.thai].start - by_english[pair.english].start).total_seconds()
        for pair in pairs
    )
    low = residuals[int((len(residuals) - 1) * 0.10)]
    high = residuals[int((len(residuals) - 1) * 0.90)]
    return (high - low) > DRIFT_SPREAD
