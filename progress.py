"""A live progress line for a run that takes ten minutes or more.

Nothing else is printed while a batch is in flight, and a batch can take a
minute, so the tool looked hung (#5). This draws one line on stderr for the
whole run: the batch, a bar, the time spent and an estimate of what is left.

On a terminal the line is redrawn in place four times a second. That redraw is
the only honest signal that a single long request is still alive, because the
client does not stream tokens and there is no intra-batch number to show. When
stderr is not a terminal (a log file, a pipe, CI) the same line is printed once
per batch instead, because carriage returns there are noise.

Timing is the only thing this can promise. The estimate assumes the remaining
batches cost the same as the ones already done, which is wrong on a model that
thinks for a different length of time on each cue. It is printed with a `~`.
"""

from __future__ import annotations

import sys
import threading
import time
from typing import TextIO

import pipeline

# Four redraws a second: fast enough to look alive, slow enough to cost nothing.
TICK_S = 0.25

BAR_WIDTH = 12
SPINNER = "-\\|/"

# Escapes used to redraw one line without leaving a trail.
RETURN = "\r"
CLEAR_TO_END = "\x1b[K"


def clock(seconds: float) -> str:
    """A duration as m:ss, or h:mm:ss once it runs past an hour."""
    total = max(0, int(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def bar(done: int, total: int, width: int = BAR_WIDTH) -> str:
    """A filled bar, always the same width, so the line does not jump."""
    if total <= 0:
        return "[" + "-" * width + "]"
    filled = round(width * done / total)
    filled = min(width, max(0, filled))
    return "[" + "#" * filled + "-" * (width - filled) + "]"


def render(
    event: pipeline.Progress,
    tick: int = 0,
    elapsed_s: float | None = None,
    eta_s: float | None = None,
) -> str:
    """The one line for this state, without any terminal control characters.

    `elapsed_s` and `eta_s` override the event's own values so a live reporter
    can move the clock between batches.
    """
    if event.started:
        entries = "entry" if event.entries_total == 1 else "entries"
        batches = "batch" if event.batches_total == 1 else "batches"
        return (
            f"translating {event.entries_total} {entries} in "
            f"{event.batches_total} {batches}"
        )

    spent = event.elapsed_s if elapsed_s is None else elapsed_s
    left = event.eta_s if eta_s is None else eta_s
    spin = SPINNER[tick % len(SPINNER)]
    line = (
        f"{spin} {bar(event.batches_done, event.batches_total)} "
        f"batch {event.batches_done}/{event.batches_total}  "
        f"entries {event.first_index}-{event.last_index}  "
        f"{clock(spent)}"
    )
    if left is not None:
        line += f"  eta {clock(left)}"
    return line


class Reporter:
    """Draws the progress line. Use as a context manager.

    `update` is what `pipeline.translate_subtitles` calls, so it can be passed
    straight in as the progress callback.
    """

    def __init__(
        self,
        stream: TextIO | None = None,
        tick_s: float = TICK_S,
        live: bool | None = None,
    ):
        self.stream = stream if stream is not None else sys.stderr
        self.tick_s = tick_s
        # `live=False` is for a run that prints log lines of its own, where a
        # redrawn line and the log would fight over the same row.
        if live is not None:
            self.live = live
        else:
            try:
                self.live = bool(self.stream.isatty())
            except (AttributeError, OSError, ValueError):
                # A closed or unusual stream is not a terminal.
                self.live = False
        self._event: pipeline.Progress | None = None
        self._updated_at = 0.0
        self._tick = 0
        self._drawn = False
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "Reporter":
        if self.live:
            self._thread = threading.Thread(
                target=self._run, name="progress", daemon=True
            )
            self._thread.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
        if self.live and self._drawn:
            # Leave the terminal on a clean line for the summary.
            self.stream.write(RETURN + CLEAR_TO_END)
            self.stream.flush()

    def update(self, event: pipeline.Progress) -> None:
        """Record a new state and draw it once, whatever the stream is."""
        with self._lock:
            self._event = event
            self._updated_at = time.monotonic()
        if event.started or not self.live:
            # The opening line is printed and kept: it is the only place the
            # total number of batches is stated.
            self._write(render(event) + "\n")
        else:
            self._draw()

    # --- internals ---------------------------------------------------------

    def _run(self) -> None:
        while not self._stop.wait(self.tick_s):
            with self._lock:
                if self._event is None:
                    continue
            self._draw()

    def _draw(self) -> None:
        with self._lock:
            event = self._event
            if event is None or event.started:
                # The opening line is already on screen, with a newline after it.
                return
            spent = event.elapsed_s + (time.monotonic() - self._updated_at)
            left = None
            if event.eta_s is not None:
                left = max(0.0, event.eta_s - (time.monotonic() - self._updated_at))
            self._tick += 1
            line = render(event, self._tick, spent, left)
        self._write(RETURN + line + CLEAR_TO_END)
        self._drawn = True

    def _write(self, text: str) -> None:
        try:
            self.stream.write(text)
            self.stream.flush()
        except (OSError, ValueError):
            # A closed or broken stream must not kill a run that is otherwise fine.
            self.live = False
