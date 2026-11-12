from __future__ import annotations

import pytest

from khamsub import batcher
from khamsub.batcher import SourceEntry


def make_entries(count: int, start_index: int = 1) -> list[SourceEntry]:
    return [
        SourceEntry(index=start_index + i, text=f"Line {start_index + i}")
        for i in range(count)
    ]


# --- parse_range ------------------------------------------------------------


def test_parse_range_from_string():
    assert batcher.parse_range("120-180") == (120, 180)


def test_parse_range_from_tuple():
    assert batcher.parse_range((5, 9)) == (5, 9)


def test_parse_range_tolerates_spaces_and_en_dash():
    assert batcher.parse_range(" 5 – 9 ") == (5, 9)


@pytest.mark.parametrize("spec", ["abc", "5", "5-", "a-b"])
def test_parse_range_rejects_junk(spec):
    with pytest.raises(ValueError):
        batcher.parse_range(spec)


def test_parse_range_rejects_reversed_bounds():
    with pytest.raises(ValueError):
        batcher.parse_range("9-5")


# --- select_range -----------------------------------------------------------


def test_select_range_is_inclusive_on_both_ends():
    entries = make_entries(20)
    selected = batcher.select_range(entries, range_="5-7")
    assert [e.index for e in selected] == [5, 6, 7]


def test_select_range_outside_file_is_empty():
    assert batcher.select_range(make_entries(5), range_="100-200") == []


def test_select_limit_keeps_first_n():
    entries = make_entries(10)
    assert [e.index for e in batcher.select_range(entries, limit=3)] == [1, 2, 3]


def test_select_range_then_limit():
    entries = make_entries(20)
    selected = batcher.select_range(entries, limit=2, range_="10-20")
    assert [e.index for e in selected] == [10, 11]


def test_select_negative_limit_rejects():
    with pytest.raises(ValueError):
        batcher.select_range(make_entries(3), limit=-1)


def test_select_nothing_returns_everything():
    entries = make_entries(4)
    assert batcher.select_range(entries) == entries


# --- splitting --------------------------------------------------------------


def test_split_covers_every_entry_once_in_order():
    entries = make_entries(125)
    batches = batcher.make_batches(entries, size=60)
    indexes = [index for batch in batches for index in batch.indexes]
    assert indexes == [e.index for e in entries]
    assert len(indexes) == len(set(indexes))


def test_split_sizes():
    assert [len(b) for b in batcher.make_batches(make_entries(125), size=60)] == [60, 60, 5]


def test_split_exact_multiple_has_no_empty_batch():
    assert [len(b) for b in batcher.make_batches(make_entries(120), size=60)] == [60, 60]


def test_split_single_entry():
    batches = batcher.make_batches(make_entries(1))
    assert len(batches) == 1
    assert batches[0].indexes == [1]


def test_split_no_entries():
    assert batcher.make_batches([]) == []


def test_split_never_exceeds_size():
    for batch in batcher.make_batches(make_entries(97), size=10):
        assert len(batch) <= 10


def test_split_respects_size_of_one():
    batches = batcher.make_batches(make_entries(3), size=1)
    assert [b.indexes for b in batches] == [[1], [2], [3]]


@pytest.mark.parametrize("size", [0, -5])
def test_split_rejects_non_positive_size(size):
    with pytest.raises(ValueError):
        batcher.make_batches(make_entries(3), size=size)


def test_split_rejects_negative_context():
    with pytest.raises(ValueError):
        batcher.make_batches(make_entries(3), context_before=-1)


def test_split_rejects_selection_not_in_entries():
    with pytest.raises(ValueError):
        batcher.make_batches(make_entries(3), selection=[SourceEntry(99, "x")])


def test_split_rejects_duplicate_selection():
    entries = make_entries(3)
    with pytest.raises(ValueError):
        batcher.make_batches(entries, selection=[entries[0], entries[0]])


def test_item_chars_counts_translatable_text_only():
    batch = batcher.make_batches([SourceEntry(1, "abcd")], size=1)[0]
    assert batch.item_chars == 4


# --- context ----------------------------------------------------------------


def test_first_batch_has_no_before_context():
    batches = batcher.make_batches(make_entries(10), size=4, context_before=2)
    assert batches[0].before == []


def test_before_context_holds_previous_entries():
    batches = batcher.make_batches(make_entries(10), size=4, context_before=2)
    assert [c.index for c in batches[1].before] == [3, 4]


def test_after_context_holds_next_entries():
    batches = batcher.make_batches(make_entries(10), size=4, context_after=2)
    assert [c.index for c in batches[0].after] == [5, 6]


def test_context_is_clamped_at_end_of_file():
    batches = batcher.make_batches(make_entries(6), size=4, context_after=2)
    assert [c.index for c in batches[-1].after] == []


def test_context_is_clamped_at_start_of_file():
    entries = make_entries(10)
    selected = batcher.select_range(entries, range_="2-3")
    batches = batcher.make_batches(entries, selection=selected, context_before=5)
    assert [c.index for c in batches[0].before] == [1]


def test_context_is_not_counted_toward_batch_size():
    batch = batcher.make_batches(
        make_entries(20), size=4, context_before=3, context_after=3
    )[1]
    assert len(batch) == 4
    assert len(batch.before) + len(batch.after) == 6


def test_thai_context_only_on_lines_before():
    entries = make_entries(10)
    translated = {e.index: f"ไทย {e.index}" for e in entries}
    batches = batcher.make_batches(
        entries, size=4, context_before=2, context_after=2, translated=translated
    )
    second = batches[1]
    assert [c.thai for c in second.before] == ["ไทย 3", "ไทย 4"]
    assert all(c.thai is None for c in second.after)


def test_thai_context_absent_when_unknown():
    batches = batcher.make_batches(make_entries(10), size=4, context_before=2)
    assert all(c.thai is None for c in batches[1].before)


def test_context_text_comes_from_entries():
    entries = [SourceEntry(1, "first"), SourceEntry(2, "second"), SourceEntry(3, "third")]
    batches = batcher.make_batches(entries, size=1, context_after=2)
    assert [c.text for c in batches[0].after] == ["second", "third"]


def test_zero_context_means_none():
    batch = batcher.make_batches(
        make_entries(5), size=2, context_before=0, context_after=0
    )[1]
    assert batch.before == [] and batch.after == []


# --- selection with context -------------------------------------------------


def test_range_selection_keeps_context_outside_range():
    entries = make_entries(20)
    selected = batcher.select_range(entries, range_="10-12")
    batches = batcher.make_batches(
        entries, selection=selected, size=60, context_before=2, context_after=2
    )
    assert batches[0].indexes == [10, 11, 12]
    assert [c.index for c in batches[0].before] == [8, 9]
    assert [c.index for c in batches[0].after] == [13, 14]


def test_selection_order_follows_the_file_not_the_input():
    entries = make_entries(6)
    backwards = list(reversed(entries[:3]))
    batches = batcher.make_batches(entries, selection=backwards, size=2)
    assert [b.indexes for b in batches] == [[1, 2], [3]]


def test_entries_missing_from_the_list_are_absent_from_context():
    # Simulates HI-stripped entries: they are simply not in `entries`.
    entries = [SourceEntry(1, "a"), SourceEntry(2, "b"), SourceEntry(4, "d")]
    batches = batcher.make_batches(entries, size=1, context_after=2)
    assert [c.index for c in batches[0].after] == [2, 4]


def test_select_by_indexes():
    entries = make_entries(5)
    selected = batcher.select_range(entries, indexes={2, 4})
    assert [entry.index for entry in selected] == [2, 4]


def test_select_by_indexes_ignores_unknown_indexes():
    assert batcher.select_range(make_entries(3), indexes={99}) == []


def test_select_by_indexes_after_a_range():
    entries = make_entries(10)
    selected = batcher.select_range(entries, range_="3-6", indexes={1, 4, 6, 8})
    assert [entry.index for entry in selected] == [4, 6]
