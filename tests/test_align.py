from __future__ import annotations

import datetime as dt

import pytest
import srt

import align


def cue(index: int, start: float, end: float) -> srt.Subtitle:
    return srt.Subtitle(
        index=index,
        start=dt.timedelta(seconds=start),
        end=dt.timedelta(seconds=end),
        content="",
    )


def test_identical_timings_are_exact_pairs():
    english = [cue(1, 1, 3), cue(2, 4, 6)]
    thai = [cue(1, 1, 3), cue(2, 4, 6)]

    result = align.align(english, thai)

    assert result.ok
    assert [(pair.thai, pair.english, pair.confidence) for pair in result.pairs] == [
        (1, 1, "exact"),
        (2, 2, "exact"),
    ]
    assert result.coverage == 1.0
    assert result.offset == dt.timedelta(0)
    assert result.unmatched_thai == ()
    assert result.unmatched_english == ()
    assert result.ambiguous_thai == ()


def test_a_constant_offset_is_found_and_refined():
    english = [cue(1, 1, 3), cue(2, 4, 6)]
    thai = [cue(1, 3.4, 5.4), cue(2, 6.4, 8.4)]

    result = align.align(english, thai)

    assert result.ok
    assert result.offset.total_seconds() == pytest.approx(2.4, abs=0.1)
    assert result.source_by_thai == {1: 1, 2: 2}


def test_jitter_within_half_a_second_is_tolerated():
    english = [cue(1, 1, 3), cue(2, 4, 6)]
    thai = [cue(1, 1.2, 3.1), cue(2, 3.8, 6.2)]

    result = align.align(english, thai)

    assert result.ok
    assert len(result.pairs) == 2
    assert all(pair.confidence == "exact" for pair in result.pairs)


def test_an_added_thai_cue_is_unmatched_not_forced():
    english = [cue(1, 1, 3), cue(2, 4, 6), cue(3, 7, 9)]
    thai = [cue(1, 1, 3), cue(2, 4, 6), cue(99, 10, 12), cue(3, 7, 9)]

    result = align.align(english, thai)

    assert result.ok
    assert result.unmatched_thai == (99,)
    assert result.source_by_thai == {1: 1, 2: 2, 3: 3}


def test_an_added_english_cue_does_not_hurt_coverage():
    english = [cue(1, 1, 3), cue(2, 4, 6), cue(3, 20, 22)]
    thai = [cue(1, 1, 3), cue(2, 4, 6)]

    result = align.align(english, thai)

    assert result.ok
    assert result.coverage == 1.0
    assert result.unmatched_english == (3,)


def test_sixty_percent_coverage_is_still_good():
    english = [cue(1, 1, 3), cue(2, 4, 6), cue(3, 7, 9)]
    thai = [
        cue(1, 1, 3),
        cue(2, 4, 6),
        cue(3, 7, 9),
        cue(4, 30, 32),
        cue(5, 40, 42),
    ]

    result = align.align(english, thai)

    assert result.ok
    assert result.coverage == pytest.approx(0.6)
    assert result.unmatched_thai == (4, 5)


def test_unrelated_files_are_poor_with_no_pairs():
    english = [cue(1, 1, 2)]
    thai = [cue(1, 100, 102), cue(2, 200, 202)]

    result = align.align(english, thai)

    assert not result.ok
    assert result.pairs == ()
    assert result.coverage == 0.0
    assert "low coverage" in result.reason


def test_progressive_drift_is_refused():
    english = [cue(i, i * 10, i * 10 + 4) for i in range(1, 31)]
    thai = [
        cue(i, i * 10 + (i - 1) * 0.1, i * 10 + 4 + (i - 1) * 0.1)
        for i in range(1, 31)
    ]

    result = align.align(english, thai)

    assert not result.ok
    assert "drift" in result.reason


def test_a_split_thai_cue_is_ambiguous():
    english = [cue(1, 0, 4)]
    thai = [cue(1, 0, 2), cue(2, 2, 4)]

    result = align.align(english, thai)

    assert result.pairs == ()
    assert result.ambiguous_thai == (1, 2)
    assert result.unmatched_thai == ()
    assert not result.ok


def test_a_merged_thai_cue_is_ambiguous():
    english = [cue(1, 0, 2), cue(2, 2, 4)]
    thai = [cue(1, 0, 4)]

    result = align.align(english, thai)

    assert result.pairs == ()
    assert result.ambiguous_thai == (1,)
    assert not result.ok


def test_a_cue_matching_two_english_lines_is_ambiguous():
    english = [cue(1, 0, 2), cue(2, 2, 4)]
    thai = [cue(1, 0.5, 3.5)]

    result = align.align(english, thai)

    assert result.pairs == ()
    assert result.ambiguous_thai == (1,)


def test_empty_input_is_poor():
    result = align.align([], [])
    assert not result.ok
    assert result.reason == "no cues to align"


def test_one_side_empty_reports_what_was_there():
    result = align.align([cue(7, 1, 2)], [])
    assert not result.ok
    assert result.unmatched_english == (7,)
    assert result.unmatched_thai == ()


def test_unsorted_input_is_sorted_internally():
    english = [cue(2, 4, 6), cue(1, 1, 3)]
    thai = [cue(2, 4, 6), cue(1, 1, 3)]

    result = align.align(english, thai)

    assert [(pair.thai, pair.english) for pair in result.pairs] == [(1, 1), (2, 2)]
