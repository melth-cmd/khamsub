"""The glossary file: terms that must be translated the same way every time.

The pre-pass (bible.py) writes Movie.glossary.toml; you may then edit it by
hand. Each term says what to do with one English word or phrase:

  keep           leave it in English (brands, acronyms, words the audience knows)
  transliterate  write it in Thai letters, as given in th (names, places)
  translate      write its Thai meaning, as given in th

Parsing is strict: a typo in a hand-edited file is reported with the term that
caused it, rather than silently changing how a name is spelled.

For translation, matching() picks the terms whose English word actually appears
in a batch, for_prompt() renders just those for the request, and fingerprint()
summarises them (plus the story bible) for the cache key, so editing one term
re-translates only the lines that word touches.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence

log = logging.getLogger(__name__)

# Only these three are meaningful; anything else is a typo or a future version.
ACTIONS: tuple[str, ...] = ("keep", "transliterate", "translate")

# actions that must carry a Thai rendering in `th`
_SPELLED_ACTIONS = frozenset({"transliterate", "translate"})


class GlossaryError(Exception):
    """The glossary file is malformed or a term is incomplete."""


@dataclass(frozen=True)
class Term:
    """One glossary entry.

    `th` is required for transliterate/translate and dropped for keep, so a term
    has exactly one meaning: the action says what `th` is for.
    """

    en: str
    action: str = "keep"
    th: str = ""

    def __post_init__(self) -> None:
        en = self.en.strip()
        action = self.action.strip().lower()
        th = self.th.strip()

        if not en:
            raise GlossaryError("a glossary term needs a word in `en`")
        if action not in ACTIONS:
            raise GlossaryError(
                f"term {en!r}: unknown action {self.action!r}; "
                f"use one of {', '.join(ACTIONS)}"
            )
        if action in _SPELLED_ACTIONS and not th:
            raise GlossaryError(
                f"term {en!r}: action {action!r} needs the Thai spelling in `th`"
            )
        if action == "keep":
            th = ""

        object.__setattr__(self, "en", en)
        object.__setattr__(self, "action", action)
        object.__setattr__(self, "th", th)


def _toml_string(value: str) -> str:
    """A TOML basic string. JSON escaping is a subset of TOML's."""
    return json.dumps(value, ensure_ascii=False)


def render(terms: Iterable[Term] = (), title: str = "") -> str:
    """Render terms as a hand-editable TOML file.

    The header is written for a human, not parsed: it says what each action
    means, so a file opened months later still explains itself.
    """
    lines = [
        f"# Glossary for {title}" if title else "# Glossary",
        "# Edit by hand; the file is read again on every run.",
        "# action is one of:",
        '#   keep          leave the English word as it is',
        '#   transliterate write it in Thai letters, given in `th`',
        '#   translate     write its Thai meaning, given in `th`',
        "# Only terms that appear in a batch are sent with that batch.",
    ]

    terms = list(terms)
    if not terms:
        lines += ["", "# (no terms found)"]
        return "\n".join(lines) + "\n"

    for term in terms:
        lines += [
            "",
            "[[term]]",
            f"en = {_toml_string(term.en)}",
            f"action = {_toml_string(term.action)}",
        ]
        if term.th:
            lines.append(f"th = {_toml_string(term.th)}")
    return "\n".join(lines) + "\n"


def parse(text: str) -> list[Term]:
    """Read glossary TOML into terms, in file order.

    Duplicates are an error rather than a silent first-wins, because both
    copies usually mean two different Thai spellings and only you know which
    one was the edit.
    """
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise GlossaryError(f"glossary is not valid TOML: {exc}") from exc

    raw = data.get("term", [])
    if isinstance(raw, Mapping):  # `[term]` where `[[term]]` was meant
        raise GlossaryError("glossary terms must be [[term]] tables, not [term]")
    if not isinstance(raw, list):
        raise GlossaryError("[term] must be a list of tables")

    terms: list[Term] = []
    seen: dict[str, int] = {}
    for number, item in enumerate(raw, 1):
        if not isinstance(item, Mapping):
            raise GlossaryError(f"term {number} is not a TOML table")

        unknown = sorted(set(item) - {"en", "action", "th"})
        if unknown:
            log.warning("Ignoring unknown field(s) %s in glossary term %d",
                        ", ".join(unknown), number)

        for field in ("en", "action", "th"):
            if field in item and not isinstance(item[field], str):
                raise GlossaryError(
                    f"term {number}: `{field}` must be a string, got {item[field]!r}"
                )
        if "action" not in item:
            raise GlossaryError(
                f"term {number} ({item.get('en', '')!r}) has no `action`"
            )

        term = Term(
            en=item.get("en", ""),
            action=item.get("action", ""),
            th=item.get("th", ""),
        )
        key = term.en.casefold()
        if key in seen:
            raise GlossaryError(
                f"term {number} repeats {term.en!r} (already term {seen[key]})"
            )
        seen[key] = number
        terms.append(term)

    return terms


def load(path: str | Path) -> list[Term]:
    """Read a glossary file. A missing file means no terms, not an error."""
    path = Path(path)
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise GlossaryError(f"could not read {path}: {exc}") from exc
    try:
        return parse(text)
    except GlossaryError as exc:
        raise GlossaryError(f"{path}: {exc}") from exc


def unique(terms: Sequence[Term]) -> list[Term]:
    """Drop repeated English words, keeping the first. Used on model output."""
    seen: set[str] = set()
    kept: list[Term] = []
    for term in terms:
        key = term.en.casefold()
        if key in seen:
            continue
        seen.add(key)
        kept.append(term)
    return kept


# --- what one batch needs ---------------------------------------------------

FINGERPRINT_CHARS = 32


def matching(terms: Sequence[Term], text: str) -> list[Term]:
    """The terms whose English word appears in `text`, in glossary order.

    Word boundaries keep "He" out of "the" and "Mai" out of "Maid"; case does
    not matter, and an apostrophe after a name ("Gandalf's") still matches.
    """
    found: list[Term] = []
    for term in terms:
        pattern = r"(?<!\w)" + re.escape(term.en) + r"(?!\w)"
        if re.search(pattern, text, re.IGNORECASE):
            found.append(term)
    return found


def for_prompt(terms: Sequence[Term]) -> str:
    """Render the selected terms for the model, one line each.

    Short on purpose: this goes with every batch that uses any of these words.
    """
    lines: list[str] = []
    for term in terms:
        if term.action == "keep":
            lines.append(f"{term.en} = keep")
        else:
            lines.append(f"{term.en} = {term.action}: {term.th}")
    return "\n".join(lines)


def fingerprint(terms: Sequence[Term], bible: str = "") -> str:
    """What the cache must remember about the notes behind one entry.

    Sorted by English word, so reordering the file invalidates nothing. The
    bible is included whole: any line's register can depend on it. With no
    terms and no bible this returns "" rather than a hash, which keeps caches
    made before the glossary existed valid for files that still have none.
    """
    if not terms and not bible.strip():
        return ""

    payload = json.dumps(
        {
            "bible": bible,
            "terms": [
                [term.en, term.action, term.th]
                for term in sorted(terms, key=lambda term: term.en.casefold())
            ],
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:FINGERPRINT_CHARS]
