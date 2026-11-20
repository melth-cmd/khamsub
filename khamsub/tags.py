"""Formatting-tag protection.

Formatting tags are removed before text is sent to the model and put back
afterwards, so the model can never mangle them. Each tag becomes a numbered
placeholder: the first becomes {0}, the second {1}, and so on.

Placeholders are validated by count: a token the model dropped, duplicated or
invented is a TagMismatch, which the retry layer turns into a re-request.

A model sometimes writes the tags themselves *beside* the placeholders (issue
#22: "{0}{1} <i>♪ … ♪</i> {2}{3}"). restore() drops a literal tag the source
also had before putting the placeholders back, so the markup is never doubled;
a literal tag the source did not have is left for verification to report.

Sound-effect brackets ([door slams]) and speaker dashes ("- Hello.") are *not*
tags here; they are content and pass through untouched.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# Order matters. ASS override blocks come first because they contain braces.
# Literal {0}-style braces in the source are also protected, so the model can
# never mistake source text for one of our placeholders.
_TAG_RE = re.compile(
    r"(?P<ass>\{\\[^{}]*\})"
    r"|(?P<html></?[a-zA-Z][a-zA-Z0-9]*(?:\s[^<>]*)?/?>)"
    r"|(?P<music>[♪♫♬♩])"
    r"|(?P<literal>\{\s*\d+\s*\})"
)

# Tolerant on the way back: models like to add spaces inside braces.
_PLACEHOLDER_RE = re.compile(r"\{\s*(\d+)\s*\}")


@dataclass(frozen=True)
class Tag:
    """One removed tag and what kind it was."""

    raw: str
    kind: str  # "ass" | "html" | "music" | "literal"


@dataclass
class ProtectedText:
    """Text with placeholders in place, plus what it takes to undo it."""

    text: str
    tags: list[Tag] = field(default_factory=list)

    @property
    def counts(self) -> Counter[str]:
        return Counter(tag.raw for tag in self.tags)

    def __len__(self) -> int:
        return len(self.tags)


@dataclass(frozen=True)
class TagIssue:
    """One placeholder problem found in a translation."""

    kind: str  # "missing" | "duplicate" | "unknown"
    index: int | None
    tag: str | None
    message: str


class TagMismatch(ValueError):
    """Raised when a translation's placeholders do not match the source."""

    def __init__(self, issues: list[TagIssue]):
        self.issues = issues
        super().__init__("; ".join(issue.message for issue in issues))


def strip_markup(text: str) -> str:
    """Text without formatting tags, placeholders or music notes.

    For checks that care about words, not markup (verification compares the
    English source with the Thai by their words).
    """
    return _PLACEHOLDER_RE.sub("", _TAG_RE.sub("", text))


def protect(text: str) -> ProtectedText:
    """Replace formatting tags with numbered placeholders."""
    tags: list[Tag] = []
    parts: list[str] = []
    position = 0

    for match in _TAG_RE.finditer(text):
        parts.append(text[position : match.start()])
        parts.append(f"{{{len(tags)}}}")
        tags.append(Tag(raw=match.group(0), kind=match.lastgroup or "html"))
        position = match.end()

    parts.append(text[position:])
    protected = ProtectedText(text="".join(parts), tags=tags)
    if tags:
        log.debug("Protected %d tag(s): %s", len(tags), protected.text)
    return protected


def _literal_matches(text: str) -> list[re.Match[str]]:
    """Tag-like matches in `text` that are not one of our own placeholders."""
    return [
        match
        for match in _TAG_RE.finditer(text)
        if not _PLACEHOLDER_RE.fullmatch(match.group(0))
    ]


def literal_tags(text: str) -> list[Tag]:
    """Tags a reply wrote itself, instead of (or beside) the placeholders.

    The model only ever received placeholders, so real markup in a reply is
    something it invented or copied. A placeholder ("{0}", "{ 1 }") is not a
    tag and is not returned.
    """
    return [
        Tag(raw=match.group(0), kind=match.lastgroup or "html")
        for match in _literal_matches(text)
    ]


def strip_literals(text: str, protected: ProtectedText) -> tuple[str, list[Tag]]:
    """Remove literal markup that would otherwise double the source's tags.

    Only a tag whose raw form appeared in the source is removed: the model
    wrote the tag a placeholder already stands for, so keeping both puts the
    markup back twice. A literal tag the source did not have is left in place
    for verification to report, because deleting it could remove content the
    model wrote as text. Placeholders are never touched.
    """
    spans = [
        match for match in _literal_matches(text) if match.group(0) in protected.counts
    ]
    if not spans:
        return text, []

    parts: list[str] = []
    position = 0
    for match in spans:
        parts.append(text[position : match.start()])
        position = match.end()
    parts.append(text[position:])

    # Removing inline markup leaves the spaces around it; Thai never needs two.
    cleaned = re.sub(r"[ \t]{2,}", " ", "".join(parts))
    removed = [
        Tag(raw=match.group(0), kind=match.lastgroup or "html") for match in spans
    ]
    return cleaned, removed


def has_extra_tags(text: str, protected: ProtectedText) -> bool:
    """True if `text` carries more of a source tag than the source does.

    `text` is already restored (the cache stores finished lines, not replies).
    A cached line written by the version that accepted literal markup beside
    the placeholders has doubled tags, so a rerun must translate it again
    instead of serving the corruption (issue #22).
    """
    return bool(protect(text).counts - protected.counts)


def check_protected(translated: str, protected: ProtectedText) -> list[TagIssue]:
    """Compare a translation's placeholders against the protected source."""
    seen = Counter(int(m.group(1)) for m in _PLACEHOLDER_RE.finditer(translated))
    expected = len(protected.tags)
    issues: list[TagIssue] = []

    for index in range(expected):
        tag = protected.tags[index].raw
        found = seen.get(index, 0)
        if found == 0:
            issues.append(
                TagIssue(
                    kind="missing",
                    index=index,
                    tag=tag,
                    message=f"placeholder {{{index}}} for {tag!r} is missing",
                )
            )
        elif found > 1:
            issues.append(
                TagIssue(
                    kind="duplicate",
                    index=index,
                    tag=tag,
                    message=f"placeholder {{{index}}} for {tag!r} appears {found} times",
                )
            )

    for index in sorted(seen):
        if index >= expected:
            issues.append(
                TagIssue(
                    kind="unknown",
                    index=index,
                    tag=None,
                    message=f"placeholder {{{index}}} was not in the source",
                )
            )

    return issues


def check(source: str, translated: str) -> list[TagIssue]:
    """Convenience wrapper: protect `source` then compare with `translated`."""
    return check_protected(translated, protect(source))


def restore(text: str, protected: ProtectedText, strict: bool = True) -> str:
    """Put the real tags back where the placeholders are.

    A literal tag the source also had is removed first: the model wrote the tag
    beside its placeholder, and keeping both would double the markup (issue
    #22). A literal tag the source did not have is left in place, since no
    placeholder owns it; verification reports it.

    With strict=False a placeholder mismatch is logged instead of raised, and
    any tags that never made it back are simply lost. Duplicates and unknown
    placeholders are left as-is, since there is no correct tag to substitute.
    """
    text, removed = strip_literals(text, protected)
    if removed:
        log.debug(
            "Removed %d literal tag(s): %s",
            len(removed),
            ", ".join(tag.raw for tag in removed),
        )

    issues = check_protected(text, protected)
    if issues:
        if strict:
            raise TagMismatch(issues)
        for issue in issues:
            log.warning("Tag mismatch: %s", issue.message)

    def substitute(match: re.Match[str]) -> str:
        index = int(match.group(1))
        if index < len(protected.tags):
            return protected.tags[index].raw
        return match.group(0)

    return _PLACEHOLDER_RE.sub(substitute, text)
