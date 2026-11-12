"""Repair a Thai .srt against the English .srt beside it.

Two jobs, always together:

1. Text hygiene, which needs no source and can never change meaning: normalise
   spaces and line endings, drop stray digit-only placeholders from another
   tool, close one missing tag or drop one orphan close, and reduce a 3+ line
   cue to two lines at a safe space. Timings and indexes are never touched.

2. A source-aware audit: align the two files by time (align.py), then use the
   English line to find the Thai lines that are empty, truncated, still English,
   mixed script, or disagree with the glossary. Those are the only lines
   `--retranslate` may later rewrite; everything else is reported and left
   alone.

Nothing here calls a model or writes a file. `repair_file` reads and returns a
`RepairResult`; the CLI decides what to write, and backs up the original first.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

import srt

import align
import glossary
import layout
import llm
import srtio
import verify

log = logging.getLogger(__name__)

# Flags worth showing a human. line_count/short/long are omitted: across two
# sources they are segmentation and style noise, not evidence of a bad line.
REPORT_KINDS = {
    "empty",
    "truncation",
    "english_left",
    "mixed_script",
    "glossary_missing",
    "glossary_inconsistent",
    "stray_punctuation",
    "tag_mismatch",
    "lost_token",
    "dashes_lost",
}

# Strong enough to spend a request on: the Thai is missing or clearly wrong,
# not merely styled differently. Stray punctuation and tags are fixed by code
# or left to a human; a retry would not reliably help.
CANDIDATE_KINDS = {
    "empty",
    "truncation",
    "english_left",
    "mixed_script",
    "glossary_missing",
    "glossary_inconsistent",
}

_STRAY_PLACEHOLDER_RE = re.compile(r"\{\s*\d+\s*\}")
_TAG_RE = re.compile(r"</?([ibu])>", re.IGNORECASE)
_TAG_SPAN_RE = re.compile(r"<[^<>\n]*>")
_THAI_RE = re.compile(r"[\u0e00-\u0e7f]")

# A report for a 1,500-cue file does not need every timing overlap spelled out.
MAX_PROBLEM_LINES = 40


@dataclass(frozen=True)
class Change:
    index: int | None  # None for a file-level change (encoding, line endings)
    kind: str
    detail: str


@dataclass(frozen=True)
class Problem:
    index: int | None
    kind: str
    detail: str


@dataclass
class TextFix:
    text: str
    changes: list[Change] = field(default_factory=list)
    problems: list[Problem] = field(default_factory=list)


@dataclass
class RepairResult:
    subtitles: list = field(default_factory=list)
    changes: list[Change] = field(default_factory=list)
    problems: list[Problem] = field(default_factory=list)
    # Cues a --retranslate pass may rewrite, strongest evidence only.
    candidates: list[int] = field(default_factory=list)
    alignment: align.Alignment = field(default_factory=align.Alignment)
    sources: dict[int, str] = field(default_factory=dict)
    encoding: str = "utf-8"
    thai_path: Path | None = None
    english_path: Path | None = None
    thai_count: int = 0
    english_count: int = 0
    skipped: int = 0
    # The --retranslate pass: what it rewrote, what it could not, and the cost.
    retranslated: list[int] = field(default_factory=list)
    retranslate_failed: list[int] = field(default_factory=list)
    retranslate_usage: llm.Usage = field(default_factory=llm.Usage)
    retranslate_providers: Counter[str] = field(default_factory=Counter)


def has_thai(text: str) -> bool:
    """True when the text contains at least one Thai character."""
    return bool(_THAI_RE.search(text))


def repair_text(index: int, text: str) -> TextFix:
    """Fix one cue's text. The rules are ordered so later ones see clean input."""
    fix = TextFix(text=text)

    cleaned = _normalise_whitespace(text)
    if cleaned != text:
        fix.changes.append(Change(index, "whitespace", "trimmed and collapsed spaces"))
        text = cleaned

    text, removed = _strip_placeholders(text)
    if removed:
        fix.changes.append(
            Change(index, "placeholder", "removed " + ", ".join(removed))
        )

    text, tag_changes, tag_problem = _balance_tags(text)
    fix.changes.extend(Change(index, "tag", detail) for detail in tag_changes)
    if tag_problem is not None:
        fix.problems.append(Problem(index, "tags", tag_problem))
    text = text.strip()

    if layout.line_count(text) >= 3:
        wrapped = _fit_two_lines(text)
        if wrapped is None:
            fix.problems.append(
                Problem(
                    index,
                    "long_cue",
                    f"{layout.line_count(text)} lines; no safe split",
                )
            )
        elif wrapped != text:
            fix.changes.append(
                Change(
                    index,
                    "wrap",
                    f"{layout.line_count(text)} lines -> {layout.line_count(wrapped)}",
                )
            )
            text = wrapped

    fix.text = text
    return fix


def repair_cue(cue) -> tuple:
    """Repair one srt.Subtitle, keeping its index and timestamps exactly."""
    fix = repair_text(cue.index, cue.content)
    fixed = srt.Subtitle(
        index=cue.index,
        start=cue.start,
        end=cue.end,
        content=fix.text,
        proprietary=getattr(cue, "proprietary", ""),
    )
    return fixed, fix


def repair_subtitles(
    thai: Sequence, english: Sequence, terms: Sequence[glossary.Term] = ()
) -> RepairResult:
    """Repair every Thai cue and audit it against the aligned English source."""
    alignment = align.align(english, thai)
    english_by_index = {cue.index: cue.content for cue in english}
    sources = {
        pair.thai: english_by_index[pair.english]
        for pair in alignment.pairs
        if pair.english in english_by_index
    }

    repaired = []
    changes: list[Change] = []
    problems: list[Problem] = []
    for cue in thai:
        fixed, fix = repair_cue(cue)
        repaired.append(fixed)
        changes.extend(fix.changes)
        problems.extend(fix.problems)

    tag_problem_indexes = {p.index for p in problems if p.kind == "tags"}
    text_problems, candidates = _text_audit(
        repaired, sources, alignment, terms, tag_problem_indexes
    )
    problems.extend(text_problems)
    problems.extend(_timing_problems(repaired))

    if not alignment.ok:
        problems.append(
            Problem(None, "alignment", f"{alignment.reason}; no retranslation")
        )
    for index in alignment.ambiguous_thai:
        problems.append(
            Problem(index, "ambiguous", "matches more than one English line; left as is")
        )
    for index in alignment.unmatched_thai:
        problems.append(
            Problem(index, "no_source", "no confident English line; left as is")
        )
    dashes = _dash_problem(repaired)
    if dashes is not None:
        problems.append(dashes)

    _sort_problems(problems)
    return RepairResult(
        subtitles=repaired,
        changes=changes,
        problems=problems,
        candidates=candidates,
        alignment=alignment,
        sources=sources,
        thai_count=len(thai),
        english_count=len(english),
    )


def _text_audit(
    cues: Sequence,
    sources: dict[int, str],
    alignment: align.Alignment,
    terms: Sequence[glossary.Term],
    tag_problem_indexes: set[int],
) -> tuple[list[Problem], list[int]]:
    """The source-aware text flags, and the cues strong enough to retranslate."""
    problems: list[Problem] = []
    candidates: set[int] = set()
    for cue in cues:
        # A poor alignment is not evidence: use the source-free checks only.
        source = sources.get(cue.index) if alignment.ok else None
        if source is not None:
            flags = verify.check_entry(cue.index, source, cue.content, terms)
        elif llm.is_non_verbal(cue.content):
            flags = []
        else:
            flags = verify.check_text(cue.index, cue.content, terms)

        for flag in flags:
            if flag.kind not in REPORT_KINDS:
                continue
            if flag.kind == "tag_mismatch" and cue.index in tag_problem_indexes:
                continue
            problems.append(Problem(cue.index, flag.kind, flag.message))
        if alignment.ok and any(flag.kind in CANDIDATE_KINDS for flag in flags):
            candidates.add(cue.index)
    return problems, sorted(candidates)


def refresh_audit(
    result: RepairResult, terms: Sequence[glossary.Term] = ()
) -> RepairResult:
    """Re-run the text audit after a retranslation, keeping the other problems.

    Timing, alignment, no_source, ambiguous, dashes, tags and long_cue do not
    change when text is replaced, so they stay. Every text flag is recomputed
    and the candidate list is rebuilt.
    """
    tag_problem_indexes = {p.index for p in result.problems if p.kind == "tags"}
    text_problems, candidates = _text_audit(
        result.subtitles,
        result.sources,
        result.alignment,
        terms,
        tag_problem_indexes,
    )
    keep = [
        problem
        for problem in result.problems
        if problem.kind not in REPORT_KINDS and problem.kind != "dashes"
    ]
    result.problems = keep + text_problems
    dashes = _dash_problem(result.subtitles)
    if dashes is not None:
        result.problems.append(dashes)
    _sort_problems(result.problems)
    result.candidates = candidates
    return result


def _sort_problems(problems: list[Problem]) -> None:
    problems.sort(
        key=lambda problem: (problem.index is None, problem.index or 0, problem.kind)
    )


def repair_file(
    thai_path: str | Path,
    english_path: str | Path,
    terms: Sequence[glossary.Term] = (),
) -> RepairResult:
    """Read both files and repair the Thai one. Nothing is written."""
    thai_path = Path(thai_path)
    english_path = Path(english_path)
    thai = srtio.load_srt(thai_path)
    english = srtio.load_srt(english_path)

    result = repair_subtitles(thai.subtitles, english.subtitles, terms=terms)
    result.thai_path = thai_path
    result.english_path = english_path
    result.encoding = thai.encoding
    result.skipped = thai.skipped

    if thai.encoding != "utf-8":
        result.changes.insert(
            0, Change(None, "encoding", f"{thai.encoding} -> UTF-8")
        )
    if b"\r\n" in thai_path.read_bytes():
        result.changes.insert(0, Change(None, "line endings", "CRLF -> LF"))
    if thai.skipped:
        result.problems.append(
            Problem(None, "unreadable", f"{thai.skipped} block(s) could not be parsed")
        )
    return result


def render_report(result: RepairResult, dry_run: bool = False) -> str:
    """A text report that separates what was fixed from what needs a human."""
    lines: list[str] = []
    thai_name = result.thai_path.name if result.thai_path else "Thai subtitles"
    lines.append(f"Repair report: {thai_name}")

    if result.english_path is not None:
        alignment = result.alignment
        lines.append(
            f"English: {result.english_path.name}  "
            f"aligned {len(alignment.pairs)}/{result.thai_count} "
            f"({alignment.coverage:.1%}), offset "
            f"{alignment.offset.total_seconds():+.1f}s"
        )
        if not alignment.ok:
            lines.append(
                f"Alignment: {alignment.reason} -- text checks only, no retranslation"
            )
    mode = "Mode: source-aware; text and layout only, timings untouched"
    if dry_run:
        mode += " (dry run; nothing written)"
    lines.append(mode)
    lines.append("")

    action = "Would repair" if dry_run else "Repaired"
    if result.changes:
        count = len(result.changes)
        cues = len(
            {change.index for change in result.changes if change.index is not None}
        )
        kinds = Counter(change.kind for change in result.changes)
        summary = ", ".join(f"{kind} {number}" for kind, number in sorted(kinds.items()))
        lines.append(
            f"{action} {_plural(count, 'change')} in {_plural(cues, 'cue')}: {summary}"
        )
        for change in result.changes:
            lines.append(f"  {_where(change.index)} {change.kind}: {change.detail}")
    else:
        lines.append(f"{action}: nothing needed")

    lines.append("")
    if result.problems:
        kinds = Counter(problem.kind for problem in result.problems)
        summary = ", ".join(
            f"{kind} {number}" for kind, number in sorted(kinds.items())
        )
        lines.append(f"Needs a look ({len(result.problems)}): {summary}")
        for problem in result.problems[:MAX_PROBLEM_LINES]:
            lines.append(
                f"  {_where(problem.index)} {problem.kind}: {problem.detail}"
            )
        remaining = len(result.problems) - MAX_PROBLEM_LINES
        if remaining > 0:
            lines.append(f"  ... and {remaining} more")
    else:
        lines.append("Needs a look: nothing")

    lines.append("")
    if result.retranslated or result.retranslate_failed:
        if result.retranslated:
            usage = result.retranslate_usage
            tokens = usage.prompt_tokens + usage.completion_tokens
            detail = f"{tokens:,} tokens"
            if usage.cost_usd is not None:
                detail += f", ${usage.cost_usd:.4f}"
            lines.append(
                f"Retranslated {_plural(len(result.retranslated), 'cue')} ({detail})"
            )
        if result.retranslate_failed:
            lines.append(
                f"Retranslation failed for "
                f"{_plural(len(result.retranslate_failed), 'cue')}; "
                "the earlier text was kept"
            )
        lines.append("")
    if not result.alignment.ok:
        lines.append("Retranslation: disabled (alignment is poor)")
    elif result.candidates:
        lines.append(
            f"Retranslation candidates: {len(result.candidates)} "
            "(opt-in with --retranslate)"
        )
    else:
        lines.append("Retranslation candidates: none")

    return "\n".join(lines) + "\n"


def _normalise_whitespace(text: str) -> str:
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
    return "\n".join(line for line in lines if line)


def _strip_placeholders(text: str) -> tuple[str, list[str]]:
    removed = [match.group(0) for match in _STRAY_PLACEHOLDER_RE.finditer(text)]
    if not removed:
        return text, []
    text = _STRAY_PLACEHOLDER_RE.sub("", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r" *\n *", "\n", text).strip()
    return text, removed


def _balance_tags(text: str) -> tuple[str, list[str], str | None]:
    """Close one missing tag or drop one orphan close; report anything else."""
    stack: list[str] = []
    orphan = None
    for match in _TAG_RE.finditer(text):
        name = match.group(1).lower()
        if match.group(0).startswith("</"):
            if not stack:
                if orphan is not None:
                    return text, [], "unbalanced tags: more than one orphan close"
                orphan = match
            elif stack[-1] != name:
                return text, [], "tags are crossed or out of order"
            else:
                stack.pop()
        else:
            stack.append(name)

    if not stack and orphan is None:
        return text, [], None
    if len(stack) == 1 and orphan is None:
        name = stack[0]
        return text + f"</{name}>", [f"closed <{name}>"], None
    if not stack and orphan is not None:
        name = orphan.group(1).lower()
        cleaned = (text[: orphan.start()] + text[orphan.end() :]).strip()
        return cleaned, [f"dropped </{name}>"], None
    if stack:
        names = ", ".join(f"<{name}>" for name in stack)
        return text, [], f"unbalanced tags: missing {names}"
    return text, [], "unbalanced tags"


def _fit_two_lines(text: str) -> str | None:
    """Join a 3+ line cue and split it once at a safe space, or give up."""
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    if len(lines) < 3:
        return text
    if "<" in text or ">" in text:
        # A split must not land inside a tag; leave the cue and report it.
        return None

    joined = " ".join(lines)
    if len(joined) <= layout.SOURCE_WRAP_WIDTH:
        return joined

    spans = layout._blocked_spans(joined)
    floor = len(joined) * layout.MIN_PIECE_SHARE
    best: tuple[int, str, str] | None = None
    for match in re.finditer(r"\s+", joined):
        point = match.start()
        left = joined[:point].strip()
        right = joined[point:].strip()
        if not left or not right or layout._inside(point, spans):
            continue
        if len(left) > layout.SOURCE_WRAP_WIDTH or len(right) > layout.SOURCE_WRAP_WIDTH:
            continue
        if len(left) < floor or len(right) < floor:
            continue
        if layout._bad_boundary(left) or layout._bad_boundary(right):
            continue
        score = abs(len(left) - len(right))
        if best is None or score < best[0]:
            best = (score, left, right)
    if best is None:
        return None
    return best[1] + "\n" + best[2]


def _timing_problems(subtitles: Sequence) -> list[Problem]:
    problems: list[Problem] = []
    previous = None
    for cue in subtitles:
        length = (cue.end - cue.start).total_seconds()
        if cue.start >= cue.end:
            problems.append(Problem(cue.index, "timing", "starts at or after it ends"))
        elif length < 0.5:
            problems.append(
                Problem(cue.index, "timing", f"shorter than 0.5s ({length:.2f}s)")
            )
        if previous is not None and cue.start < previous.end:
            overlap = (previous.end - cue.start).total_seconds()
            problems.append(
                Problem(
                    cue.index,
                    "timing",
                    f"overlaps cue {previous.index} by {overlap:.1f}s",
                )
            )
        previous = cue
    return problems


def _dash_problem(subtitles: Sequence) -> Problem | None:
    styles: set[str] = set()
    first_2010 = None
    for cue in subtitles:
        for line in cue.content.split("\n"):
            line = line.strip()
            if not line or line[0] not in "-\u2010":
                continue
            styles.add(line[0])
            if line[0] == "\u2010" and first_2010 is None:
                first_2010 = cue.index
    if "-" in styles and "\u2010" in styles:
        return Problem(
            first_2010,
            "dashes",
            "mixed dash styles (- and U+2010); left as is",
        )
    return None


def _where(index: int | None) -> str:
    return "[file]" if index is None else f"[{index}]"


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"
