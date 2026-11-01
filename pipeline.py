"""The whole job for one file: subtitles in, translated subtitles out.

Order of operations matters here:

  parse -> (optional HI strip) -> protect tags -> batch -> translate
        -> restore tags -> rebuild subtitles -> write

Tags are protected before batching so the model never sees markup, and restored
afterwards from the protected copy, so a dropped placeholder can never silently
corrupt the output.

Entries dropped by HI stripping are left out of the batches and out of the
finished file, which is why the output may have fewer rows than the source.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Collection, Mapping, Sequence

import srt

import batcher
import cache as cache_module
import glossary
import hi
import layout
import llm
import runner
import srtio
import tags
import verify
from batcher import SourceEntry
from llm import Usage
from runner import EntryResult

log = logging.getLogger(__name__)

# A letter or digit; placeholders and punctuation do not count as content.
_WORD_CHAR_RE = re.compile(r"[^\W_]", re.UNICODE)


def _has_words(text: str) -> bool:
    """True if anything translatable is left once placeholders are removed."""
    return bool(_WORD_CHAR_RE.search(tags._PLACEHOLDER_RE.sub("", text)))


@dataclass
class Prepared:
    """Source entries ready for batching, plus what is needed to undo it."""

    entries: list[SourceEntry] = field(default_factory=list)
    protected: dict[int, tags.ProtectedText] = field(default_factory=dict)
    sources: dict[int, str] = field(default_factory=dict)
    dropped: list[int] = field(default_factory=list)


@dataclass
class TranslatedFile:
    """The finished subtitles and what happened along the way."""

    subtitles: list[srt.Subtitle] = field(default_factory=list)
    results: list[EntryResult] = field(default_factory=list)
    # The source text each result was translated from, after HI stripping and
    # with formatting tags intact, so verification can compare the two.
    sources: dict[int, str] = field(default_factory=dict)
    dropped: list[int] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    # Entries re-translated by the --fix pass, for the report.
    fixed: list[int] = field(default_factory=list)
    # Entries the --review pass looked at, and its proposed lines by index.
    reviewed: list[int] = field(default_factory=list)
    suggestions: dict[int, str] = field(default_factory=dict)

    @property
    def failures(self) -> list[EntryResult]:
        return [result for result in self.results if not result.ok]

    @property
    def rewrapped(self) -> list[EntryResult]:
        """Entries whose line breaks this program had to repair."""
        return [result for result in self.results if result.rewrapped in {"dash", "proportional"}]

    @property
    def restored_dashes(self) -> list[EntryResult]:
        """Entries where a speaker dash the model dropped was put back."""
        return [result for result in self.results if result.dashes_restored]

    @property
    def merged(self) -> list[EntryResult]:
        """Entries left on one line because no safe break point existed."""
        return [result for result in self.results if result.rewrapped == "unrepaired"]

    @property
    def translated(self) -> dict[int, str]:
        return {result.index: result.text for result in self.results}


def prepare(subtitles: Sequence[srt.Subtitle], strip_hi: bool = False) -> Prepared:
    """Turn subtitles into text-only entries, optionally dropping HI cues.

    Entries with nothing left to translate are recorded as dropped rather than
    sent as empty strings, which would fail validation forever.

    A source cue is normalised first (layout.normalise_source): a continuation
    the source wrapped onto its own line is joined to its speaker, so the line
    count every later step uses is the one the cue means, not the one it was
    wrapped at (#11).
    """
    prepared = Prepared()
    for subtitle in subtitles:
        # A stray carriage return would show up as a line break to the model but
        # not to layout.py, which counts lines by "\n".
        text = subtitle.content.replace("\r\n", "\n").replace("\r", "\n")
        if strip_hi:
            cleaned = hi.strip_hi(text)
            if cleaned.dropped:
                prepared.dropped.append(subtitle.index)
                continue
            text = cleaned.text
        text = layout.normalise_source(text)

        prepared.sources[subtitle.index] = text
        protected = tags.protect(text)
        if not _has_words(protected.text):
            prepared.dropped.append(subtitle.index)
            continue

        prepared.protected[subtitle.index] = protected
        prepared.entries.append(SourceEntry(subtitle.index, protected.text))

    if prepared.dropped:
        log.info("%d entr%s dropped (nothing to translate)", len(prepared.dropped),
                 "y" if len(prepared.dropped) == 1 else "ies")
    return prepared


def batch_terms(
    terms: Sequence[glossary.Term], batch: batcher.Batch
) -> list[glossary.Term]:
    """The terms whose word appears in this batch's items.

    Context lines are deliberately not searched: they are there to explain the
    batch, and a word in a neighbouring line is no reason to add a term.
    """
    text = "\n".join(entry.text for entry in batch.items)
    return glossary.matching(terms, text)


def glossary_for_batch(
    terms: Sequence[glossary.Term], batch: batcher.Batch
) -> str:
    """The glossary text for one batch: only the terms that batch uses."""
    return glossary.for_prompt(batch_terms(terms, batch))


def translate_subtitles(
    client: llm.ChatClient,
    subtitles: Sequence[srt.Subtitle],
    strip_hi: bool = False,
    size: int = 60,
    context_before: int = 4,
    context_after: int = 2,
    bible: str = "",
    terms: Sequence[glossary.Term] = (),
    cache: cache_module.Cache | None = None,
    limit: int | None = None,
    range_: str | tuple[int, int] | None = None,
    indexes: Collection[int] | None = None,
    ignore_cache: bool = False,
    seed: Mapping[int, str] | None = None,
    current: Mapping[int, str] | None = None,
    progress: Callable[[Progress], None] | None = None,
    **runner_kwargs,
) -> TranslatedFile:
    """Translate a whole file, batch by batch, keeping timestamps intact.

    With a cache, entries already translated are not sent again; they still
    count as context, so resumed batches see the same Thai they would have.

    `terms` is the whole glossary; each batch is sent only the terms whose word
    actually appears in that batch, which keeps the request small and focused.
    The bible is short enough to send with every batch.

    With `limit`, `range_` or `indexes`, only those entries are translated and
    written. Context is still drawn from the whole file, so a slice is
    translated with the surrounding lines in view.

    `ignore_cache` re-requests every selected entry even if it is cached (the
    result is still written back), which is what --fix needs; `seed` supplies
    Thai text used as context only, e.g. the first pass when fixing a few lines.
    `current` shows the existing translation to the model and switches to the
    review prompt; --review uses it.
    """
    prepared = prepare(subtitles, strip_hi=strip_hi)
    result = TranslatedFile(dropped=list(prepared.dropped))

    if not prepared.entries:
        log.info("Nothing to translate")
        return result

    selected = batcher.select_range(
        prepared.entries, limit=limit, range_=range_, indexes=indexes
    )
    selected_indexes = {entry.index for entry in selected}
    if not selected:
        log.warning("Selection is empty; nothing to translate")
        return result
    if len(selected) < len(prepared.entries):
        log.info(
            "Translating %d of %d entr%s", len(selected), len(prepared.entries),
            "y" if len(prepared.entries) == 1 else "ies",
        )

    translated: dict[int, str] = dict(seed or {})
    pending: list[SourceEntry] = []

    for entry in prepared.entries:
        if entry.index not in selected_indexes:
            continue
        hit = cache.get(entry.index, entry.text) if cache is not None else None
        if hit is None:
            pending.append(entry)
            continue
        # Re-wrapped on read, so cached entries merged by an older run are fixed
        # without paying to translate them again.
        fixed = layout.rewrap(entry.text, hit)
        translated[entry.index] = fixed.text
        if ignore_cache:
            # --fix: the cached text is still useful context, but the entry
            # itself is what was flagged, so it is requested again.
            pending.append(entry)
            continue
        result.results.append(
            EntryResult(
                index=entry.index,
                text=fixed.text,
                ok=True,
                from_cache=True,
                rewrapped=fixed.method,
                dashes_restored=fixed.dashes_restored,
            )
        )

    if cache is not None and not ignore_cache:
        cached = len(selected) - len(pending)
        log.info(
            "%d entr%s already cached, %d to translate",
            cached,
            "y" if cached == 1 else "ies",
            len(pending),
        )

    source_text = {entry.index: entry.text for entry in prepared.entries}
    batch_count = -(-len(pending) // size)

    started = time.monotonic()
    if progress is not None:
        progress(Progress(0, batch_count, len(pending)))

    for number in range(1, batch_count + 1):
        # Rebuilt each round so context-before picks up Thai produced so far.
        batch = batcher.make_batches(
            prepared.entries,
            selection=pending,
            size=size,
            context_before=context_before,
            context_after=context_after,
            translated=translated,
        )[number - 1]

        selected_terms = batch_terms(terms, batch)
        log.info(
            "Batch %d/%d: %d entr%s%s",
            number,
            batch_count,
            len(batch),
            "y" if len(batch) == 1 else "ies",
            f", {len(selected_terms)} glossary "
            f"term{'' if len(selected_terms) == 1 else 's'}"
            if selected_terms
            else "",
        )
        batch_current = (
            {
                entry.index: current[entry.index]
                for entry in batch.items
                if entry.index in current
            }
            if current is not None
            else None
        )
        batch_result: runner.BatchResult = runner.translate_batch(
            client,
            batch,
            bible=bible,
            glossary=glossary.for_prompt(selected_terms),
            current=batch_current,
            **runner_kwargs,
        )
        result.usage = result.usage + batch_result.usage

        for entry_result in batch_result.entries:
            restored = tags.restore(
                entry_result.text,
                prepared.protected[entry_result.index],
                strict=False,
            )
            fixed = layout.rewrap(source_text[entry_result.index], restored)
            result.results.append(
                EntryResult(
                    index=entry_result.index,
                    text=fixed.text,
                    ok=entry_result.ok,
                    reason=entry_result.reason,
                    attempts=entry_result.attempts,
                    rewrapped=fixed.method,
                    dashes_restored=fixed.dashes_restored,
                )
            )
            if entry_result.ok:
                translated[entry_result.index] = fixed.text
                if cache is not None:
                    # The cache keeps what the model actually said, so a later
                    # improvement to re-wrapping can still be applied on read.
                    cache.put(
                        entry_result.index, source_text[entry_result.index], restored
                    )

        # Saved per batch: an interrupted run loses at most this batch.
        if cache is not None:
            cache.save()

        if progress is not None:
            elapsed = time.monotonic() - started
            left = batch_count - number
            eta = elapsed / number * left if left else 0.0
            progress(
                Progress(
                    number,
                    batch_count,
                    len(pending),
                    batch.items[0].index,
                    batch.items[-1].index,
                    elapsed,
                    eta,
                )
            )

    result.results.sort(key=lambda entry: entry.index)
    # Verification compares each finished entry with the text it came from.
    result.sources = {
        entry.index: prepared.sources[entry.index]
        for entry in result.results
        if entry.index in prepared.sources
    }
    result.subtitles = _rebuild(subtitles, result, prepared, selected_indexes)
    return result


def _rebuild(
    subtitles: Sequence[srt.Subtitle],
    result: TranslatedFile,
    prepared: Prepared,
    included: set[int],
) -> list[srt.Subtitle]:
    """Put translated text back into the original entries, timestamps and all.

    Entries outside the selection (--range / --limit) are left out entirely, so
    a slice produces a file holding just that slice.
    """
    by_index = {entry.index: entry for entry in result.results}
    rebuilt: list[srt.Subtitle] = []

    for subtitle in subtitles:
        if subtitle.index in prepared.dropped or subtitle.index not in included:
            continue
        entry = by_index.get(subtitle.index)
        if entry is None:  # pragma: no cover - every selected entry has a result
            log.error("Entry [%d] has no result, keeping the original", subtitle.index)
            rebuilt.append(subtitle)
            continue
        rebuilt.append(
            srt.Subtitle(
                index=subtitle.index,
                start=subtitle.start,
                end=subtitle.end,
                content=entry.text,
                proprietary=subtitle.proprietary,
            )
        )
    return rebuilt


# Rough character-per-token ratios. English input runs about 4; Thai output is
# token-expensive, so it gets a lower number. Good enough for --dry-run, which
# only needs the order of magnitude.
CHARS_PER_PROMPT_TOKEN = 3.2
CHARS_PER_COMPLETION_TOKEN = 2.2
PROMPT_OVERHEAD_CHARS = 40  # section labels and [ID] prefixes
BIBLE_OVERHEAD_CHARS = len("[STORY BIBLE]\n\n")
GLOSSARY_OVERHEAD_CHARS = len("[GLOSSARY]\n\n")


@dataclass
@dataclass(frozen=True)
class Progress:
    """One progress event: the start of a run, or a batch that just finished.

    `batches_done` is 0 for the start event, which is what tells a reporter to
    print the opening line rather than a bar. `eta_s` is 0 on the last batch.
    """

    batches_done: int
    batches_total: int
    entries_total: int
    first_index: int = 0
    last_index: int = 0
    elapsed_s: float = 0.0
    eta_s: float | None = None

    @property
    def started(self) -> bool:
        return self.batches_done == 0


@dataclass(frozen=True)
class Estimate:
    """What a run would cost, without making it."""

    entries: int = 0
    batches: int = 0
    dropped: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


def estimate(
    subtitles: Sequence[srt.Subtitle],
    strip_hi: bool = False,
    size: int = 60,
    context_before: int = 4,
    context_after: int = 2,
    limit: int | None = None,
    range_: str | tuple[int, int] | None = None,
    bible: str = "",
    terms: Sequence[glossary.Term] = (),
    input_usd_per_mtok: float = 0.0,
    output_usd_per_mtok: float = 0.0,
) -> Estimate:
    """Predict batch count, tokens and cost for a run, with no API calls.

    The bible and the batch's glossary go with every request, so both are
    counted once per batch, exactly as translate_subtitles sends them.
    """
    prepared = prepare(subtitles, strip_hi=strip_hi)
    selected = batcher.select_range(prepared.entries, limit=limit, range_=range_)
    batches = batcher.make_batches(
        prepared.entries,
        selection=selected,
        size=size,
        context_before=context_before,
        context_after=context_after,
    )

    prompt_chars = completion_chars = 0
    for batch in batches:
        prompt_chars += len(llm.SYSTEM_PROMPT) + PROMPT_OVERHEAD_CHARS
        if bible.strip():
            prompt_chars += len(bible) + BIBLE_OVERHEAD_CHARS
        batch_terms = glossary_for_batch(terms, batch)
        if batch_terms:
            prompt_chars += len(batch_terms) + GLOSSARY_OVERHEAD_CHARS
        prompt_chars += sum(len(item.text) for item in batch.items)
        prompt_chars += sum(len(line.text) + len(line.thai or "") for line in batch.before)
        prompt_chars += sum(len(line.text) for line in batch.after)
        completion_chars += sum(len(item.text) for item in batch.items)

    prompt_tokens = int(prompt_chars / CHARS_PER_PROMPT_TOKEN)
    completion_tokens = int(completion_chars / CHARS_PER_COMPLETION_TOKEN)
    cost = (
        prompt_tokens / 1e6 * input_usd_per_mtok
        + completion_tokens / 1e6 * output_usd_per_mtok
    )
    return Estimate(
        entries=len(selected),
        batches=len(batches),
        dropped=len(prepared.dropped),
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cost_usd=cost,
    )


def retranslate_flagged(
    client: llm.ChatClient,
    subtitles: Sequence[srt.Subtitle],
    result: TranslatedFile,
    strip_hi: bool = False,
    bible: str = "",
    terms: Sequence[glossary.Term] = (),
    cache: cache_module.Cache | None = None,
    **kwargs,
) -> TranslatedFile:
    """Re-translate the entries the verification pass flagged (--fix).

    The flagged entries are requested again with the correction rules in the
    system prompt, ignoring their cached text but using the first pass as
    context. A fix that comes back valid replaces the old line; one that fails
    validation again leaves the earlier text in place, because a second failure
    is no reason to replace a translation that was at least readable.

    `limit`/`range_` are not re-applied: `result` already holds only the
    entries this run translates, and the flagged ones are a subset of those.
    """
    verification = verify.verify_file(result, terms)
    indexes = verification.entries
    if not indexes:
        log.info("--fix: nothing flagged, nothing to re-translate")
        return result

    log.info(
        "--fix: re-translating %d flagged entr%s",
        len(indexes),
        "y" if len(indexes) == 1 else "ies",
    )
    fix_kwargs = {
        key: value for key, value in kwargs.items() if key not in {"limit", "range_"}
    }
    fixed = translate_subtitles(
        client,
        subtitles,
        strip_hi=strip_hi,
        bible=bible,
        terms=terms,
        cache=cache,
        indexes=indexes,
        ignore_cache=True,
        # Only real Thai is context: a failed entry holds English, and labelling
        # that "TH:" would mislead the model.
        seed={entry.index: entry.text for entry in result.results if entry.ok},
        strict=True,
        **fix_kwargs,
    )

    by_index = {entry.index: entry for entry in result.results}
    for entry in fixed.results:
        if entry.ok:
            by_index[entry.index] = entry
        else:
            log.warning(
                "entry [%d] still failing after --fix (%s); keeping the earlier text",
                entry.index,
                entry.reason,
            )

    result.results = sorted(by_index.values(), key=lambda entry: entry.index)
    result.usage = result.usage + fixed.usage
    result.fixed = list(indexes)
    result.sources.update(fixed.sources)
    result.subtitles = _rebuild(
        subtitles,
        result,
        prepare(subtitles, strip_hi=strip_hi),
        set(by_index),
    )
    return result


def review_flagged(
    client: llm.ChatClient,
    subtitles: Sequence[srt.Subtitle],
    result: TranslatedFile,
    strip_hi: bool = False,
    bible: str = "",
    terms: Sequence[glossary.Term] = (),
    **kwargs,
) -> TranslatedFile:
    """Ask the model to review the flagged entries (--review).

    The flagged lines are sent again with the current translation shown, so the
    model can either improve them or repeat them. A suggestion that matches the
    line already in the file is dropped: there is nothing to decide then.

    Nothing is written here: suggestions go into report.txt and the file keeps
    its translation. The cache is not touched either, because advice is not a
    translation of record. `limit`/`range_` are not re-applied, as in --fix.
    """
    verification = verify.verify_file(result, terms)
    indexes = verification.entries
    result.reviewed = list(indexes)
    if not indexes:
        log.info("--review: nothing flagged, nothing to review")
        return result

    log.info(
        "--review: asking for suggestions on %d flagged entr%s",
        len(indexes),
        "y" if len(indexes) == 1 else "ies",
    )
    review_kwargs = {
        key: value for key, value in kwargs.items() if key not in {"limit", "range_"}
    }
    reviewed = translate_subtitles(
        client,
        subtitles,
        strip_hi=strip_hi,
        bible=bible,
        terms=terms,
        cache=None,
        indexes=indexes,
        ignore_cache=True,
        seed={entry.index: entry.text for entry in result.results if entry.ok},
        current={entry.index: entry.text for entry in result.results},
        **review_kwargs,
    )

    current = {entry.index: entry.text for entry in result.results}
    result.suggestions = {
        entry.index: entry.text
        for entry in reviewed.results
        if entry.ok and entry.text.strip() != current.get(entry.index, "").strip()
    }
    return result


def translate_file(
    client: llm.ChatClient,
    source: str | Path,
    output: str | Path | None = None,
    encoding: str = "utf-8",
    strip_hi: bool = False,
    use_cache: bool = True,
    model: str = "",
    bible: str = "",
    terms: Sequence[glossary.Term] = (),
    fix: bool = False,
    review: bool = False,
    **kwargs,
) -> tuple[TranslatedFile, Path]:
    """Read, translate and write one file. Returns (result, output path).

    `bible` and `terms` are the hand-editable notes for this file. They are part
    of every cache key: editing a term drops the lines that use that word, and
    editing the bible drops everything, because register is shared.

    With `fix`, the flagged entries are re-translated once, after the whole file
    is done and before anything is written, so the file never holds a half-fixed
    state. With `review`, the lines still flagged afterwards are reviewed and
    the suggestions go into the report; they never change the subtitles.
    """
    source = Path(source)
    loaded = srtio.load_srt(source)
    if loaded.skipped:
        log.warning("%s: %d malformed block(s) were skipped", source.name, loaded.skipped)

    def notes_key(index: int, text: str) -> str:
        return glossary.fingerprint(glossary.matching(terms, text), bible)

    cache = None
    if use_cache:
        cache = cache_module.Cache(
            srtio.sidecar_path(source, ".cache.json"),
            model=model,
            glossary=notes_key,
        ).load()

    result = translate_subtitles(
        client,
        loaded.subtitles,
        strip_hi=strip_hi,
        bible=bible,
        terms=terms,
        cache=cache,
        **kwargs,
    )
    if fix:
        result = retranslate_flagged(
            client,
            loaded.subtitles,
            result,
            strip_hi=strip_hi,
            bible=bible,
            terms=terms,
            cache=cache,
            **kwargs,
        )
    if review:
        result = review_flagged(
            client,
            loaded.subtitles,
            result,
            strip_hi=strip_hi,
            bible=bible,
            terms=terms,
            **kwargs,
        )
    target = Path(output) if output is not None else srtio.output_path_for(source)
    srtio.write_srt(target, result.subtitles, encoding=encoding)
    return result, target
