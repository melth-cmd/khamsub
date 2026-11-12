"""Per-entry JSON cache, so a rerun does not pay for work already done.

The file sits next to the subtitles (Movie.cache.json) and is rewritten after
each batch, atomically, so an interrupted run loses at most the batch it was on.

What makes an entry reusable is a single key per entry, a hash of:

  - this file format's version
  - the source text of that entry
  - the model that translated it
  - the prompt version
  - the glossary entries that applied to that entry

The key is stored with the entry, so a stale line is simply a miss: changing one
line of a subtitle, or one glossary term, re-translates only the lines affected.
Changing the model or the prompt also only misses, instead of throwing the file
away. Old caches (format version 1) are ignored and start empty.

Who decides which glossary entries apply to a line is `glossary.py` (Phase 3).
Until then the cache takes a `glossary` callable that returns a fingerprint for
one entry and defaults to "no glossary".
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import llm
from . import srtio

log = logging.getLogger(__name__)

FORMAT_VERSION = 2

# 128 bits of SHA-256; keys are stored once per entry, so shorter is cheaper and
# a collision would need two entries agreeing on source, model, prompt and terms.
KEY_CHARS = 32


GlossaryKey = Callable[[int, str], str]


def no_glossary(index: int, source: str) -> str:
    """Default fingerprint: a cache with no glossary applies to every line."""
    return ""


def entry_key(
    source: str,
    model: str = "",
    prompt_version: int = llm.PROMPT_VERSION,
    glossary: str = "",
) -> str:
    """The cache key for one entry.

    The index is deliberately not part of it: if a line keeps its text but moves
    to another SRT number, the translation is still good.
    """
    payload = json.dumps(
        [FORMAT_VERSION, source, model, prompt_version, glossary],
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:KEY_CHARS]


@dataclass(frozen=True)
class CacheEntry:
    """One remembered translation."""

    key: str  # entry_key() for the source/model/prompt/glossary it was made with
    source: str  # the protected source text the model was given
    text: str  # the finished Thai, tags already restored


class Cache:
    """A JSON file mapping subtitle index to a remembered translation."""

    def __init__(
        self,
        path: str | Path,
        model: str = "",
        prompt_version: int = llm.PROMPT_VERSION,
        glossary: GlossaryKey | None = None,
    ):
        self.path = Path(path)
        self.model = model
        self.prompt_version = prompt_version
        self._glossary_key = glossary or no_glossary
        self._entries: dict[int, CacheEntry] = {}
        self._dirty = False

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, index: int) -> bool:
        return index in self._entries

    def key_for(self, index: int, source: str) -> str:
        """The key this program would use for this entry right now."""
        return entry_key(
            source,
            model=self.model,
            prompt_version=self.prompt_version,
            glossary=self._glossary_key(index, source),
        )

    def load(self) -> "Cache":
        """Read the file if it exists. A broken cache is a warning, never fatal."""
        if not self.path.exists():
            return self

        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("Ignoring unreadable cache %s: %s", self.path.name, exc)
            return self

        if not isinstance(data, dict) or data.get("version") != FORMAT_VERSION:
            log.warning("Ignoring cache %s: unknown format", self.path.name)
            return self

        raw_entries = data.get("entries") or {}
        if not isinstance(raw_entries, dict):
            log.warning("Ignoring cache %s: entries are not a table", self.path.name)
            return self

        stale = 0
        for key, value in raw_entries.items():
            try:
                index = int(key)
            except (TypeError, ValueError):
                log.debug("Ignoring cache key %r", key)
                continue
            if not isinstance(value, dict):
                continue
            source, text, stored = value.get("source"), value.get("text"), value.get("key")
            if not (isinstance(source, str) and isinstance(text, str) and isinstance(stored, str)):
                continue
            if stored != self.key_for(index, source):
                stale += 1
                continue
            self._entries[index] = CacheEntry(key=stored, source=source, text=text)

        log.info(
            "Loaded %d cached entr%s from %s",
            len(self._entries),
            "y" if len(self._entries) == 1 else "ies",
            self.path.name,
        )
        if stale:
            log.info(
                "%d cached entr%s %s stale (source, model, prompt or glossary changed)",
                stale,
                "y" if stale == 1 else "ies",
                "is" if stale == 1 else "are",
            )
        return self

    def get(self, index: int, source: str) -> str | None:
        """The cached Thai for this entry, or None if it is missing or stale."""
        entry = self._entries.get(index)
        if entry is None or entry.key != self.key_for(index, source):
            return None
        return entry.text

    def put(self, index: int, source: str, text: str) -> None:
        key = self.key_for(index, source)
        existing = self._entries.get(index)
        if existing is not None and existing.key == key and existing.text == text:
            return
        self._entries[index] = CacheEntry(key=key, source=source, text=text)
        self._dirty = True

    def save(self) -> None:
        """Write the cache atomically, but only if something changed."""
        if not self._dirty:
            return
        payload = {
            "version": FORMAT_VERSION,
            "model": self.model,
            "prompt_version": self.prompt_version,
            "entries": {
                str(index): {"key": entry.key, "source": entry.source, "text": entry.text}
                for index, entry in sorted(self._entries.items())
            },
        }
        data = json.dumps(payload, ensure_ascii=False, indent=1).encode("utf-8")
        srtio._atomic_write(self.path, data)
        self._dirty = False
