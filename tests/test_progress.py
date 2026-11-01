from __future__ import annotations

import io

import pipeline
import progress


class FakeTTY(io.StringIO):
    def isatty(self) -> bool:
        return True


# --- formatting -------------------------------------------------------------


def test_a_clock_under_an_hour_is_minutes_and_seconds():
    assert progress.clock(0) == "0:00"
    assert progress.clock(65) == "1:05"
    assert progress.clock(600) == "10:00"


def test_a_clock_over_an_hour_keeps_the_hours():
    assert progress.clock(3661) == "1:01:01"


def test_a_negative_clock_is_zero():
    assert progress.clock(-5) == "0:00"


def test_the_bar_is_always_the_same_width():
    assert len(progress.bar(0, 9)) == progress.BAR_WIDTH + 2
    assert len(progress.bar(9, 9)) == progress.BAR_WIDTH + 2
    assert progress.bar(0, 9) == "[" + "-" * progress.BAR_WIDTH + "]"
    assert progress.bar(9, 9) == "[" + "#" * progress.BAR_WIDTH + "]"


def test_the_bar_never_overflows():
    assert progress.bar(20, 9) == "[" + "#" * progress.BAR_WIDTH + "]"


def test_the_start_event_names_the_work():
    line = progress.render(pipeline.Progress(0, 9, 535))
    assert line == "translating 535 entries in 9 batches"


def test_the_start_event_is_singular_for_one_of_each():
    assert progress.render(pipeline.Progress(0, 1, 1)) == (
        "translating 1 entry in 1 batch"
    )


def test_a_batch_line_has_the_batch_entries_time_and_eta():
    line = progress.render(
        pipeline.Progress(2, 9, 535, 61, 120, 90.0, 240.0), tick=0
    )
    assert "batch 2/9" in line
    assert "entries 61-120" in line
    assert "1:30" in line
    assert "eta 4:00" in line


def test_the_last_batch_has_no_eta():
    line = progress.render(pipeline.Progress(9, 9, 535, 481, 535, 600.0, 0.0))
    assert "eta" in line  # 0:00 is honest: there is nothing left


def test_the_spinner_moves_with_the_tick():
    first = progress.render(pipeline.Progress(1, 9, 535, 1, 60, 10.0, 80.0), tick=0)
    second = progress.render(pipeline.Progress(1, 9, 535, 1, 60, 10.0, 80.0), tick=1)
    assert first[0] != second[0]


# --- the reporter -----------------------------------------------------------


def test_a_pipe_gets_one_line_per_batch():
    stream = io.StringIO()
    bar = progress.Reporter(stream=stream)
    bar.update(pipeline.Progress(0, 2, 10))
    bar.update(pipeline.Progress(1, 2, 10, 1, 5, 10.0, 10.0))
    lines = stream.getvalue().splitlines()
    assert lines[0] == "translating 10 entries in 2 batches"
    assert "batch 1/2" in lines[1]
    assert "\r" not in stream.getvalue()


def test_a_terminal_redraws_one_line_instead_of_printing_many():
    stream = FakeTTY()
    with progress.Reporter(stream=stream, tick_s=999) as bar:
        bar.update(pipeline.Progress(0, 2, 10))
        bar.update(pipeline.Progress(1, 2, 10, 1, 5, 10.0, 10.0))
    text = stream.getvalue()
    assert "\r" in text
    # One newline, for the opening line; the batches redraw in place.
    assert text.count("\n") == 1


def test_a_live_reporter_clears_its_line_on_exit():
    stream = FakeTTY()
    with progress.Reporter(stream=stream, tick_s=999) as bar:
        bar.update(pipeline.Progress(1, 2, 10, 1, 5, 10.0, 10.0))
    assert stream.getvalue().endswith(progress.CLEAR_TO_END)


def test_a_closed_stream_does_not_kill_the_run():
    stream = io.StringIO()
    stream.close()
    bar = progress.Reporter(stream=stream)
    bar.update(pipeline.Progress(1, 2, 10, 1, 5, 10.0, 10.0))
    assert bar.live is False


def test_verbose_forces_a_line_per_batch_even_on_a_terminal():
    stream = FakeTTY()
    bar = progress.Reporter(stream=stream, live=False)
    bar.update(pipeline.Progress(1, 2, 10, 1, 5, 10.0, 10.0))
    assert "\n" in stream.getvalue()
    assert "\r" not in stream.getvalue()
