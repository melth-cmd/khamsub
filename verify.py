"""Verification pass: look at a finished translation and flag what looks wrong.

This module never changes the subtitles. It compares each entry with the source
it was translated from, returns flags, and renders them into report.txt beside
the output. Fixing is a human decision, or an explicit --fix run later.

Only the English source and the finished Thai are compared, so cached and
freshly translated entries are treated the same.

Flag kinds:
  failed               the entry was left in English after retries
  empty                nothing but whitespace, tags or placeholders came back
  short / long         the length is far from the source's (advisory)
  line_count           a different number of lines than the source
  truncation           fewer lines than the source and far shorter: text is lost
  bleed                the first line repeats a line from the cue above
  dashes_lost          a speaker dash the source had is gone
  stray_punctuation    a line ends with a full stop, which Thai does not use
  tag_mismatch         formatting tags are missing or invented
  lost_token           a number, link or short code from the source is missing
  glossary_missing     a glossary term is not rendered as the glossary says
  glossary_inconsistent  a term that should be Thai is still in English
  mixed_script         Latin letters glued to Thai: a mangled fragment
  english_left         English left in the translation (a mistake, except for
                       links, handles and short letter-digit codes)

Non-verbal sources ("Mm-hmm", "[MUSIC PLAYING]") are only checked for being
empty: they are allowed to come back untranslated.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Protocol, Sequence

import glossary
import layout
import llm
import srtio
import tags

log = logging.getLogger(__name__)

# Stable order, so a report is laid out the same way on every run.
KINDS: tuple[str, ...] = (
    "failed",
    "empty",
    "short",
    "long",
    "line_count",
    "truncation",
    "bleed",
    "dashes_lost",
    "stray_punctuation",
    "tag_mismatch",
    "lost_token",
    "glossary_missing",
    "glossary_inconsistent",
    "mixed_script",
    "english_left",
)

# Length heuristics. Thai is written without spaces, so it often comes out
# shorter than the English; these only catch outliers, and are advisory.
MIN_LENGTH_RATIO = 0.15
MAX_LENGTH_RATIO = 4.0
# Below this many source characters a ratio says nothing ("Yes." -> "ใช่").
MIN_COMPARE_CHARS = 4

# A merge that is also far shorter has lost text rather than saved a line. On
# the 16 cached runs in subs/ (10,083 pairs) the Thai/English content ratio is
# 0.97 at the median and 0.53 at p1, the lowest legitimate two-to-one merge is
# 0.48, and the two truncations in issue #19 were 0.354 and 0.333. The check is
# scoped to merges because a one-line cue can be legitimately terse
# ("Bend your knees." -> "งอเข่า" is 0.43).
TRUNCATION_RATIO = 0.45

# Latin words, keeping apostrophes inside a word ("Gandalf's", "don't").
_ENGLISH_WORD_RE = re.compile(r"[A-Za-z]+(?:['\u2019][A-Za-z]+)*")


@dataclass(frozen=True)
class Flag:
    """One thing worth a look, tied to one entry."""

    index: int
    kind: str
    message: str
    source: str = ""
    translation: str = ""


@dataclass
class Verification:
    """All flags for one file, plus how many entries were checked."""

    flags: list[Flag] = field(default_factory=list)
    checked: int = 0

    def extend(self, flags: Sequence[Flag]) -> None:
        self.flags.extend(flags)

    @property
    def by_kind(self) -> dict[str, int]:
        return dict(Counter(flag.kind for flag in self.flags))

    @property
    def entries(self) -> list[int]:
        """Entry indexes with at least one flag, in order."""
        return sorted({flag.index for flag in self.flags})

    def for_index(self, index: int) -> list[Flag]:
        return [flag for flag in self.flags if flag.index == index]


class Verifiable(Protocol):
    """What verify_file() needs: pipeline.TranslatedFile satisfies this."""

    results: Sequence[object]  # EntryResult-like: index/text/ok/reason
    sources: Mapping[int, str]


def scrub(text: str) -> str:
    """Text without formatting tags or placeholders, for word and length checks."""
    return tags.strip_markup(text)


def content_length(text: str) -> int:
    """Characters that carry meaning: tags, placeholders and spaces removed."""
    return len(re.sub(r"\s+", "", scrub(text)))


def english_words(text: str) -> list[str]:
    """Latin-script words in `text`, in order, without duplicates."""
    words: list[str] = []
    seen: set[str] = set()
    for word in _ENGLISH_WORD_RE.findall(scrub(text)):
        key = word.casefold()
        if key not in seen:
            seen.add(key)
            words.append(word)
    return words


def _line_key(line: str) -> str:
    """A line reduced to what makes two lines "the same" for a bleed check."""
    return re.sub(r"\s+", " ", scrub(line)).strip().casefold()


def bleed_flag(
    index: int,
    previous_source: str,
    previous: str,
    source: str,
    translated: str,
) -> Flag | None:
    """A translation that opens with a line copied from the cue above.

    A repeated refrain is a deliberate repetition, so the check is skipped when
    the two sources share a line: if the source repeats, the translation should.
    """
    first = next((line for line in translated.split("\n") if line.strip()), "")
    if not first.strip() or not previous.strip():
        return None
    earlier = {_line_key(line) for line in previous.split("\n") if line.strip()}
    if _line_key(first) not in earlier:
        return None
    previous_lines = {
        _line_key(line) for line in previous_source.split("\n") if line.strip()
    }
    source_lines = {_line_key(line) for line in source.split("\n") if line.strip()}
    if previous_lines & source_lines:
        return None
    return Flag(
        index,
        "bleed",
        "the first line repeats a line from the cue above",
        source,
        translated,
    )


def tag_mismatch(source: str, translated: str) -> str | None:
    """A one-line description of missing/invented tags, or None if they match.

    Comparing the tags themselves (not placeholders) is what matters here: by
    now the pipeline has restored them, and a tag the model dropped is simply
    gone. An invented {N} survives restore as literal text, so it shows up too.
    """
    wanted = tags.protect(source).counts
    got = tags.protect(translated).counts
    missing = wanted - got
    extra = got - wanted
    if not missing and not extra:
        return None

    parts: list[str] = []
    if missing:
        parts.append("missing " + _render_tags(missing))
    if extra:
        parts.append("extra " + _render_tags(extra))
    return "tag mismatch: " + "; ".join(parts)


def _render_tags(counts: Counter[str]) -> str:
    return ", ".join(
        f"{raw} x{count}" if count > 1 else raw for raw, count in sorted(counts.items())
    )


def glossary_flags(
    index: int, source: str, translated: str, terms: Sequence[glossary.Term]
) -> list[Flag]:
    """Check the terms this entry actually uses, as the glossary says to write them."""
    flags: list[Flag] = []
    for term in glossary.matching(terms, source):
        if term.action == "keep":
            if not _has_en(translated, term.en):
                flags.append(
                    Flag(
                        index,
                        "glossary_missing",
                        f"glossary term {term.en!r} (keep) is missing from the translation",
                        source,
                        translated,
                    )
                )
            continue

        if term.th in translated:
            continue
        if _has_en(translated, term.en):
            flags.append(
                Flag(
                    index,
                    "glossary_inconsistent",
                    f"glossary term {term.en!r} is still in English, expected {term.th!r}",
                    source,
                    translated,
                )
            )
        else:
            flags.append(
                Flag(
                    index,
                    "glossary_missing",
                    f"glossary term {term.en!r} expected as {term.th!r} is missing",
                    source,
                    translated,
                )
            )
    return flags


def _has_en(text: str, word: str) -> bool:
    """True if `word` appears as a whole word, case-insensitively."""
    pattern = r"(?<!\w)" + re.escape(word) + r"(?!\w)"
    return re.search(pattern, text, re.IGNORECASE) is not None


def mixed_script(index: int, source: str, translated: str) -> list[Flag]:
    """Latin glued to Thai, which is never a deliberate keep."""
    runs = llm.mixed_script(translated)
    if not runs:
        return []
    return [
        Flag(
            index,
            "mixed_script",
            "Latin glued to Thai: " + ", ".join(runs),
            source,
            translated,
        )
    ]


def leftover_english(
    index: int,
    source: str,
    translated: str,
    terms: Sequence[glossary.Term],
) -> list[Flag]:
    """English words in the Thai that no "keep" term accounts for.

    Words covered by any glossary term are left to the glossary checks, so a
    name that should have been transliterated is reported once, not twice.
    A run glued to Thai is a mangled fragment and is left to mixed_script, so
    this stays about words that were deliberately left in English.
    """
    covered = {term.en.casefold() for term in terms}
    glued = {word.casefold() for word in llm.mixed_script(translated)}
    # Links, handles and letter-digit codes stay Latin on purpose, so they are
    # not leftover English; drop them before looking for words.
    allowed = translated
    for token in llm.kept_verbatim(translated):
        allowed = allowed.replace(token, " ")
    words = [
        word
        for word in english_words(allowed)
        if word.casefold() not in covered and word.casefold() not in glued
    ]
    if not words:
        return []
    return [
        Flag(
            index,
            "english_left",
            "English left in the translation: " + ", ".join(words),
            source,
            translated,
        )
    ]


def stray_punctuation(index: int, source: str, translated: str) -> list[Flag]:
    """A Thai line that ends with a full stop, which the convention forbids.

    The prompt already says so; the check is here because a bare "." is
    invisible to every other one. It is advisory in the report and a soft
    retry during translation, so a stubborn stop never turns Thai into English.
    """
    lines = llm.stray_punctuation(translated)
    if not lines:
        return []
    return [
        Flag(
            index,
            "stray_punctuation",
            "line ends with a full stop: " + " / ".join(lines),
            source,
            translated,
        )
    ]


def length_flags(index: int, source: str, translated: str) -> list[Flag]:
    """Flag a translation that is implausibly short or long for its source."""
    source_length = content_length(source)
    translated_length = content_length(translated)
    if source_length < MIN_COMPARE_CHARS or translated_length == 0:
        return []

    ratio = translated_length / source_length
    detail = f"{translated_length} vs {source_length} characters"
    if ratio < MIN_LENGTH_RATIO:
        return [Flag(index, "short", f"unusually short ({detail})", source, translated)]
    if ratio > MAX_LENGTH_RATIO:
        return [Flag(index, "long", f"unusually long ({detail})", source, translated)]
    return []


def truncation_flag(index: int, source: str, translated: str) -> Flag | None:
    """A merge that lost text: fewer lines than the source and far shorter.

    Only merges are judged, so a terse one-line cue is never called truncated.
    The two known cases (issue #19) were 2-line cues that came back as one line
    holding only the first half.
    """
    source_lines = layout.line_count(source)
    translated_lines = layout.line_count(translated)
    if translated_lines >= source_lines:
        return None

    source_length = content_length(source)
    translated_length = content_length(translated)
    if source_length < MIN_COMPARE_CHARS or translated_length == 0:
        return None

    share = translated_length / source_length
    if share >= TRUNCATION_RATIO:
        return None
    return Flag(
        index,
        "truncation",
        f"possible truncation: {source_lines} line(s) in, {translated_lines} out, "
        f"only {share:.0%} of the source length",
        source,
        translated,
    )


def check_text(
    index: int,
    translated: str,
    terms: Sequence[glossary.Term] = (),
    source: str = "",
) -> list[Flag]:
    """The checks that need only the Thai text, not its source.

    They are: an empty translation, Latin glued to Thai, and English left in
    the text. `--repair` runs these on files whose source is missing or only
    partly aligned; `check_entry` adds the source-dependent checks around them.

    `source` is only copied into the flags for the report -- no check reads it,
    so a caller without a source can leave it empty.
    """
    if content_length(translated) == 0:
        return [Flag(index, "empty", "the translation is empty", source, translated)]

    flags: list[Flag] = []
    flags.extend(mixed_script(index, source, translated))
    flags.extend(leftover_english(index, source, translated, terms))
    return flags


def check_entry(
    index: int,
    source: str,
    translated: str,
    terms: Sequence[glossary.Term] = (),
    ok: bool = True,
    reason: str | None = None,
    rewrapped: str | None = None,
) -> list[Flag]:
    """Every flag one entry deserves, in KINDS order.

    `rewrapped` is the layout result for the entry, so a merge layout.py made
    on purpose reads differently from a merge it refused to split.
    """
    if not ok:
        message = "left in English" + (f" ({reason})" if reason else "")
        return [Flag(index, "failed", message, source, translated)]

    text_flags = check_text(index, translated, terms, source)
    if llm.is_non_verbal(source):
        # A kept "[MUSIC PLAYING]" is not leftover English; only emptiness
        # still matters for a non-verbal source.
        return [flag for flag in text_flags if flag.kind == "empty"]
    if any(flag.kind == "empty" for flag in text_flags):
        return text_flags

    flags: list[Flag] = []

    source_lines = layout.line_count(source)
    translated_lines = layout.line_count(translated)
    truncated = truncation_flag(index, source, translated)
    if truncated is not None:
        # This says more than line_count would, so it replaces that flag.
        flags.append(truncated)
    elif source_lines != translated_lines:
        message = (
            f"{source_lines} line(s) in the source, "
            f"{translated_lines} in the translation"
        )
        if rewrapped == "unrepaired" and translated_lines < source_lines:
            message = "left merged by layout repair (no safe break point)"
        flags.append(Flag(index, "line_count", message, source, translated))

    source_dashes = layout.dash_count(source)
    translated_dashes = layout.dash_count(translated)
    if translated_dashes < source_dashes:
        flags.append(
            Flag(
                index,
                "dashes_lost",
                f"{source_dashes - translated_dashes} speaker dash line(s) lost",
                source,
                translated,
            )
        )

    flags.extend(stray_punctuation(index, source, translated))

    tag_problem = tag_mismatch(source, translated)
    if tag_problem:
        flags.append(Flag(index, "tag_mismatch", tag_problem, source, translated))

    lost = llm.missing_kept(source, translated)
    if lost:
        flags.append(
            Flag(
                index,
                "lost_token",
                "number or code missing from the translation: " + ", ".join(lost),
                source,
                translated,
            )
        )

    flags.extend(glossary_flags(index, source, translated, terms))
    flags.extend(text_flags)
    flags.extend(length_flags(index, source, translated))
    return flags


def verify_file(translated: Verifiable, terms: Sequence[glossary.Term] = ()) -> Verification:
    """Check every entry in a finished file. Never raises on bad content."""
    verification = Verification()
    results = sorted(translated.results, key=lambda result: result.index)
    previous_source = ""
    previous_text = ""

    for result in results:
        source = translated.sources.get(result.index)
        if source is None:
            log.warning(
                "no source text for entry [%d]; not verified", result.index
            )
            continue
        verification.checked += 1
        flags = check_entry(
            result.index,
            source,
            result.text,
            terms,
            ok=getattr(result, "ok", True),
            reason=getattr(result, "reason", None),
            rewrapped=getattr(result, "rewrapped", None),
        )
        bleed = bleed_flag(
            result.index, previous_source, previous_text, source, result.text
        )
        if bleed is not None:
            flags.append(bleed)
        verification.extend(flags)
        previous_source = source
        previous_text = result.text

    verification.flags.sort(key=lambda flag: (flag.index, KINDS.index(flag.kind)))
    return verification


# --- report.txt -------------------------------------------------------------
#
# The report is written after every translating run and never changes the
# subtitles. It says what was checked, what was flagged, what layout had to
# repair, and how many entries the HI cleanup removed, so a surprisingly short
# file or a bad line can be understood without re-reading the translation.

REPORT_SUFFIX = ".report.txt"


def report_path(source: str | Path) -> Path:
    """Where the report for `source` goes: Movie.en.srt -> Movie.report.txt.

    Like the other sidecars, the ".en." tag is dropped rather than rewritten.
    """
    return srtio.sidecar_path(source, REPORT_SUFFIX)


def write_report(path: str | Path, text: str) -> Path:
    """Write the rendered report atomically as UTF-8."""
    return srtio.write_text(path, text)


def _plural(count: int, word: str, plural: str | None = None) -> str:
    if count == 1:
        return f"{count} {word}"
    return f"{count} {plural or word + 's'}"


def layout_summary(translated: Verifiable) -> str:
    """One line about line-count and dash repairs, with the merge rate.

    The merge rate is the point: it shows whether the prompt rule is working,
    or whether layout.py is silently fixing every second multi-line entry.
    """
    sources = getattr(translated, "sources", {})
    dash = proportional = extra = dashes = unrepaired = 0
    multi_line = 0
    for result in translated.results:
        source = sources.get(result.index)
        if source is None:
            continue
        if layout.line_count(source) > 1:
            multi_line += 1
        if result.rewrapped == "dash":
            dash += 1
        elif result.rewrapped == "proportional":
            proportional += 1
        elif result.rewrapped == "extra_lines":
            extra += 1
        elif result.rewrapped == "unrepaired":
            unrepaired += 1
        if result.dashes_restored:
            dashes += 1

    if multi_line == 0 and not extra and not dashes:
        return "Layout repairs: none (no multi-line entries)"

    split = dash + proportional
    parts: list[str] = []
    if multi_line:
        rate = split / multi_line * 100
        how = []
        if dash:
            how.append(f"{dash} dash")
        if proportional:
            how.append(f"{proportional} proportional")
        detail = f", {', '.join(how)}" if how else ""
        parts.append(
            f"{split} of {multi_line} multi-line "
            f"{'entry' if multi_line == 1 else 'entries'} re-split "
            f"({rate:.0f}%{detail})"
        )
    if extra:
        parts.append(
            f"{_plural(extra, 'entry', 'entries')} merged down to the source line count"
        )
    if unrepaired:
        parts.append(
            f"{_plural(unrepaired, 'entry', 'entries')} left merged "
            "(no safe break point)"
        )
    if dashes:
        parts.append(f"{_plural(dashes, 'speaker dash', 'speaker dashes')} put back")
    return "Layout repairs: " + ", ".join(parts)


def _one_line(text: str) -> str:
    """A whole suggestion on one line, so the advice stays short at the top."""
    return " / ".join(part.strip() for part in text.split("\n") if part.strip())


def _flag_block(flag: Flag) -> str:
    """One flag: its reason, then the source and the translation, line by line."""
    lines = [f"[{flag.index}] {flag.kind}", f"  reason: {flag.message}"]
    for label, text in (("original", flag.source), ("translation", flag.translation)):
        content = text.split("\n") if text else [""]
        lines.append(f"  {label}:")
        lines.extend(f"    {line}" for line in content)
    return "\n".join(lines)


def render_report(
    translated: Verifiable,
    verification: Verification,
    source_name: str = "",
    target_name: str = "",
    model: str = "",
    now: dt.datetime | None = None,
) -> str:
    """The whole report.txt as text, ready to write.

    Everything comes from the finished run: flags and counts from
    `verification`, dropped entries and layout repairs from `translated`.
    `now` is injectable so tests can pin the timestamp.
    """
    created = (now or dt.datetime.now()).strftime("%Y-%m-%d %H:%M")
    flagged = len(verification.entries)

    lines = ["Translation report"]
    if source_name:
        lines.append(f"  source:  {source_name}")
    if target_name:
        lines.append(f"  output:  {target_name}")
    if model:
        lines.append(f"  model:   {model}")
    lines.append(f"  created: {created}")
    lines.append("")

    checked = _plural(verification.checked, "entry", "entries")
    if verification.flags:
        lines.append(
            f"Checked {checked}, flagged {_plural(flagged, 'entry', 'entries')}."
        )
        counts = ", ".join(
            f"{kind} {verification.by_kind[kind]}"
            for kind in KINDS
            if kind in verification.by_kind
        )
        lines.append(f"Flags: {counts}")
    else:
        lines.append(f"Checked {checked}; nothing flagged.")

    # --review advice, kept at the top so it is read before the details.
    reviewed = getattr(translated, "reviewed", [])
    suggestions = getattr(translated, "suggestions", {})
    if reviewed:
        if suggestions:
            lines.append("Suggestions (from --review, nothing applied):")
            for index in sorted(suggestions):
                lines.append(f"  [{index}] {_one_line(suggestions[index])}")
        else:
            lines.append(
                "Suggestions (from --review): none, it agreed with every flagged line"
            )

    dropped = getattr(translated, "dropped", [])
    if dropped:
        lines.append(
            f"Dropped before translation: {_plural(len(dropped), 'entry', 'entries')} "
            "(HI cleanup, or nothing left to translate; not in the output)"
        )
    else:
        lines.append("Dropped before translation: none")

    unverified = len(translated.results) - verification.checked
    if unverified > 0:
        lines.append(f"Not verified: {_plural(unverified, 'entry', 'entries')}")

    lines.append(layout_summary(translated))

    fixed = getattr(translated, "fixed", [])
    if fixed:
        still = sum(
            1 for index in fixed if any(flag.index == index for flag in verification.flags)
        )
        clean = len(fixed) - still
        detail = f"{clean} clean, {still} still flagged" if still else "all clean now"
        lines.append(
            f"Fix pass: re-translated {_plural(len(fixed), 'entry', 'entries')} "
            f"({detail})"
        )

    if verification.flags:
        lines.append("")
        lines.append(
            "Each flag below is for you to check; this program changes nothing."
        )
        for flag in verification.flags:
            lines.append("")
            lines.append(_flag_block(flag))

    return "\n".join(lines) + "\n"
