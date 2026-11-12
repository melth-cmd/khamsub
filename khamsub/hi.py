"""Hearing-impaired (SDH) description stripping.

Subtitles for the deaf and hard of hearing carry sound cues the model would
otherwise translate or drop inconsistently: [door slams], (laughs),
[MUSIC PLAYING]. With HI stripping on, those are removed from the source before
batching, so they are never sent and never cost tokens.

Only bracketed descriptions go. Music markers and speaker labels are content
the user asked to keep. An entry left with no words at all is reported as
dropped and omitted from the output entirely.

This module is pure text; it knows nothing about srt objects.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# Square and round brackets, one level deep, never spanning a line break so a
# stray opening bracket cannot swallow the rest of the file.
_DESCRIPTION_RE = re.compile(r"\[[^\[\]\n]*\]|\([^()\n]*\)")


def _keep_bracket(segment: str) -> bool:
    """Capitalised brackets are names and stay: [Maya], [Mr. O], [Yuki, Anna].

    All-caps brackets are still treated as cues and removed: [MUSIC PLAYING],
    [DOOR SLAMS]. Mixed-case ones like [Anna chuckles] survive; a few unwanted
    survivors are cheaper than deleting the speaker's name.
    """
    inner = segment[1:-1].strip()
    if not inner or inner.isupper():
        return False
    return any(character.isupper() for character in inner)

# A line holding nothing but a speaker dash is a leftover from "- [sighs]".
_DASH_ONLY_RE = re.compile(r"^[-–—]\s*$")
_MULTI_SPACE_RE = re.compile(r"[ \t]{2,}")

# A letter or digit. Thai counts; punctuation and music notes do not.
_WORD_CHAR_RE = re.compile(r"[^\W_]", re.UNICODE)


@dataclass(frozen=True)
class HiResult:
    """Cleaned text plus what came out of it."""

    text: str
    removed: tuple[str, ...] = field(default_factory=tuple)

    @property
    def changed(self) -> bool:
        return bool(self.removed)

    @property
    def dropped(self) -> bool:
        """True when nothing translatable is left, so the entry should go."""
        return not _WORD_CHAR_RE.search(self.text)


def strip_hi(text: str) -> HiResult:
    """Remove bracketed descriptions from one subtitle entry.

    Capitalised brackets (character names) are left in place. Whitespace
    tidying is skipped when nothing was removed, so entries without
    descriptions come back byte-identical.
    """
    removed: list[str] = []

    def drop(match: re.Match[str]) -> str:
        segment = match.group(0)
        if _keep_bracket(segment):
            return segment
        removed.append(segment)
        return ""

    cleaned = _DESCRIPTION_RE.sub(drop, text)
    if not removed:
        return HiResult(text=text)

    lines: list[str] = []
    for line in cleaned.split("\n"):
        line = _MULTI_SPACE_RE.sub(" ", line).strip()
        if not line or _DASH_ONLY_RE.match(line):
            continue
        lines.append(line)

    result = HiResult(text="\n".join(lines), removed=tuple(removed))
    if result.dropped:
        log.debug("HI stripping emptied the entry (removed %s)", ", ".join(removed))
    return result
