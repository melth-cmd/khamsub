"""The story bible pre-pass: read the script once, write notes for the translator.

Run only on request (``--bible-only``), never in the middle of a translation
run. It takes the cleaned source entries, splits them into parts of about
CHUNK_CHARS characters (never mid-entry), asks the model for translation notes
on each part, and merges the parts into one story bible plus a glossary.

The notes are about people, not scenes: who speaks, how old they are, what they
call each other, and which Thai pronouns and particles fit. Those facts are what
English subtitles leave out and Thai needs, so they are collected once and then
sent with every translation request (item 17).

Two files are written next to the subtitles:

    Movie.bible.md       summary, setting, characters with their Thai register
    Movie.glossary.toml  names/places/terms with keep/transliterate/translate

Both are yours to edit; the pre-pass refuses to overwrite them without --force.
The model's reply is JSON, checked and normalised here, so a missing field or an
unknown action becomes a warning, not a crash halfway through a paid run.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import glossary
import llm
import srtio
from batcher import SourceEntry

log = logging.getLogger(__name__)

# ~12k characters is roughly 3k English tokens: a comfortable part for the model
# to read whole, and small enough that several parts stay affordable.
CHUNK_CHARS = 12_000

# Notes want fewer random flourishes than a translation; retries are colder still.
PREPASS_TEMPERATURE = 0.2
RETRY_TEMPERATURE = 0.1
MAX_JSON_ATTEMPTS = 2


class BibleError(Exception):
    """The model's notes could not be read, or there was nothing to read."""


# --- the notes --------------------------------------------------------------


@dataclass(frozen=True)
class Character:
    """One speaker (or person spoken about) and how to address them in Thai."""

    name: str
    gender: str = ""
    age: str = ""
    role: str = ""
    relationships: str = ""
    register: str = ""  # Thai pronouns/particles, e.g. "ผม + ครับ with strangers"

    def heading(self) -> str:
        details = ", ".join(part for part in (self.gender, self.age) if part)
        return f"{self.name} ({details})" if details else self.name


@dataclass
class Bible:
    """What the pre-pass learned about one work."""

    title: str = ""
    year: str = ""
    summary: str = ""
    notes: str = ""  # setting, era, tone
    characters: list[Character] = field(default_factory=list)
    terms: list[glossary.Term] = field(default_factory=list)

    def to_markdown(self) -> str:
        """The bible as a hand-editable file, short enough to send every time."""
        heading = self.title or "Untitled"
        if self.year:
            heading += f" ({self.year})"
        lines = [
            f"# Story bible: {heading}",
            "",
            "<!-- Built by `translate.py --bible-only`. Edit by hand; this file is "
            "sent with every translation request. -->",
            "",
            "## Summary",
            self.summary or "(no summary)",
            "",
            "## Setting and tone",
            self.notes or "(none)",
            "",
            "## Characters",
        ]
        if not self.characters:
            lines += ["", "(none)"]
        for character in self.characters:
            lines += ["", f"### {character.heading()}"]
            if character.role:
                lines.append(f"- Role: {character.role}")
            if character.relationships:
                lines.append(f"- Relationships: {character.relationships}")
            if character.register:
                lines.append(f"- Thai: {character.register}")
        return "\n".join(lines).rstrip() + "\n"

    def to_glossary_toml(self) -> str:
        return glossary.render(self.terms, title=self.title)


@dataclass
class BibleResult:
    """The finished notes plus what they cost."""

    bible: Bible
    usage: llm.Usage = field(default_factory=llm.Usage)
    calls: int = 0

    @property
    def total_tokens(self) -> int:
        return self.usage.prompt_tokens + self.usage.completion_tokens


# --- the filename as a hint -------------------------------------------------

_YEAR_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")
_SEPARATORS_RE = re.compile(r"[._]+")
_BRACKETS_RE = re.compile(r"[()\[\]{}]")

# Release-name noise: it tells the model nothing and can confuse the title.
_JUNK_TOKENS = frozenset(
    {
        "en", "eng", "english", "hi", "sdh", "cc", "forced", "partial",
        "sub", "subs", "subtitle", "subtitles",
        "480p", "720p", "1080p", "2160p", "4k",
    }
)


def title_from_filename(source: str | Path) -> tuple[str, str]:
    """Guess (title, year) from the file name.

    "Movie (2026).en.srt" -> ("Movie", "2026"),
    "Show.S01E01.en.hi_partial.srt" -> ("Show S01E01", "").
    The filename is only a hint to help the model recognise the work; a wrong
    guess never changes the output, so this stays deliberately simple.
    """
    stem = srtio.sidecar_path(source, "").name  # drops the .en. tag
    spaced = _SEPARATORS_RE.sub(" ", stem)

    year = ""
    match = _YEAR_RE.search(spaced)
    if match:
        year = match.group(1)
        spaced = spaced[: match.start()] + " " + spaced[match.end() :]

    words = [word for word in spaced.split() if word.casefold() not in _JUNK_TOKENS]
    title = " ".join(_BRACKETS_RE.sub(" ", " ".join(words)).split())
    if not title:  # the name was nothing but a year, e.g. "2012.en.srt"
        title = " ".join(_BRACKETS_RE.sub(" ", _SEPARATORS_RE.sub(" ", stem)).split())
    return title, year


# --- splitting the script ---------------------------------------------------


def chunk_entries(
    entries: Sequence[SourceEntry], max_chars: int = CHUNK_CHARS
) -> list[list[SourceEntry]]:
    """Split entries into file-order parts of at most `max_chars` characters.

    Boundaries only fall between entries, so a two-line conversation is never
    cut in half. A single entry longer than the limit gets a part of its own.
    """
    if max_chars < 1:
        raise ValueError(f"Chunk size must be at least 1, got {max_chars}")

    chunks: list[list[SourceEntry]] = []
    current: list[SourceEntry] = []
    size = 0
    for entry in entries:
        length = len(entry.text) + 1  # + the newline that joins it
        if current and size + length > max_chars:
            chunks.append(current)
            current, size = [], 0
        current.append(entry)
        size += length
    if current:
        chunks.append(current)
    return chunks


def render_script(entries: Iterable[SourceEntry]) -> str:
    """The plain script text of one part, without indexes."""
    return "\n".join(entry.text for entry in entries)


# --- prompts ----------------------------------------------------------------

NOTES_SYSTEM_PROMPT = """\
You read English subtitle scripts and take notes for a Thai translator.
You do not translate the dialogue and you never write any part of the translation.

Write everything in English, except Thai suggestions: pronouns, particles and
glossary spellings are written in Thai script, so the translator can use them as
they are. Give the exact Thai forms, not descriptions of them, for example
"ผม + ครับ with strangers, หนู + ค่ะ with her mother".

Report only what the script supports. Do not invent a gender, age or relationship
that is not shown or strongly implied; write "unknown" instead. A wrong note is
worse than an empty one, because the translator follows these notes on every line.

Report:
- summary: 2-4 sentences on what happens in the part you are given.
- notes: setting, era, genre, tone, and anything that changes register
  (formal office, school, royal court, dialect, code-switching).
- characters: people the script names, or that it clearly identifies by role
  ("the inspector"). Use the name the script uses. Fields: name, gender
  (male/female/unknown), age (range or "unknown"), role, relationships,
  register. A speaker this part cannot identify is not a character: never write
  "first speaker", "the man on the phone" or "he". Another part, or the merge,
  will name them.
- terms: names, places, brands, titles, ranks and technical words that must be
  spelled the same way every time. Give each one an action:
  "transliterate" (Thai phonetic spelling in th), "translate" (Thai meaning in
  th) or "keep" (stays in English).
  Transliterate by default: names, brands and titles all end up in Thai script,
  because a Thai audience reads Thai. Use "translate" for a title with a known
  Thai name. Reserve "keep" for numbers, URLs, @handles and short letter-digit
  codes such as K9. Leave `th` empty for "keep".

Reply with one JSON object and nothing else, exactly in this shape:
{"summary": "...", "notes": "...", "characters": [{"name": "...", "gender": "male", "age": "40s", "role": "...", "relationships": "...", "register": "..."}], "terms": [{"en": "...", "action": "transliterate", "th": "..."}]}
"""

MERGE_SYSTEM_PROMPT = """\
You are given notes taken from consecutive parts of one English subtitle script.
Merge them into one story bible for a Thai translator.

- One summary of the whole work, 3-5 sentences.
- One setting and tone note for the whole work.
- One entry per character, combining what every part said. Where parts disagree,
  prefer what is shown most often or most explicitly, and keep a single register.
  Keep every Thai form the translator needs, in Thai script.
- Only keep named people. A part may have described a speaker it could not name
  ("first speaker", "the man on the phone", "he"): merge it into the named
  character whose dialogue matches, or drop it. Never output a person called
  "He", "She" or "First speaker".
- One entry per term, with one action and one spelling. Drop a term that appears
  once in passing and is not a name, place, brand, rank or technical word.
- Everything is in English except Thai suggestions, as before.
- Never invent facts that the parts do not support.

Reply with one JSON object and nothing else, in the same shape as the notes:
{"summary": "...", "notes": "...", "characters": [{"name": "...", "gender": "...", "age": "...", "role": "...", "relationships": "...", "register": "..."}], "terms": [{"en": "...", "action": "keep", "th": ""}]}
"""


def _title_lines(title: str, year: str) -> str:
    heading = f"TITLE: {title}" if title else "TITLE: unknown"
    if year:
        heading += f"\nYEAR: {year}"
    return heading


def notes_messages(title: str, year: str, script: str) -> list[dict[str, str]]:
    """The request for one part of the script."""
    return [
        {"role": "system", "content": NOTES_SYSTEM_PROMPT},
        {"role": "user", "content": f"{_title_lines(title, year)}\n\n[SCRIPT]\n{script}"},
    ]


def merge_messages(
    title: str, year: str, parts: Sequence[Mapping[str, Any]]
) -> list[dict[str, str]]:
    """The request that folds the part notes into one bible."""
    return [
        {"role": "system", "content": MERGE_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": f"{_title_lines(title, year)}\n\n[PART NOTES]\n"
            + json.dumps(list(parts), ensure_ascii=False),
        },
    ]


# --- reading the reply ------------------------------------------------------


def parse_json(text: str) -> Mapping[str, Any]:
    """Pull the JSON object out of a reply, fences and prose included."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[A-Za-z]*\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)

    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0:
        raise BibleError(f"the notes contain no JSON object: {text[:60]!r}")
    fragment = cleaned[start : end + 1] if end > start else cleaned[start:]
    try:
        data = json.loads(fragment)
    except json.JSONDecodeError as exc:
        raise BibleError(f"the notes are not valid JSON: {exc}") from exc
    if not isinstance(data, Mapping):
        raise BibleError("the notes are not a JSON object")
    return data


def _text(value: Any) -> str:
    """A model field as trimmed text. Numbers and lists are tolerated."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (list, tuple)):
        return "; ".join(part for part in (_text(item) for item in value) if part)
    if isinstance(value, Mapping):
        return ""
    return str(value).strip()


def _tables(data: Mapping[str, Any], key: str) -> list[Mapping[str, Any]]:
    """The dicts under `key`, accepting one bare object where a list was asked."""
    raw = data.get(key) or []
    if isinstance(raw, Mapping):
        raw = [raw]
    if not isinstance(raw, list):
        log.warning("Ignoring %s notes: expected a list", key)
        return []
    return [item for item in raw if isinstance(item, Mapping)]


# Names a model reaches for when it cannot tell who is speaking. They describe a
# gap in the notes, not a person, and a bible full of "He" and "First speaker"
# teaches the translator nothing.
_PLACEHOLDER_NAMES = frozenset(
    {
        "he", "she", "him", "her", "they", "them", "i", "you",
        "man", "woman", "boy", "girl", "speaker", "first speaker",
        "second speaker", "third speaker", "voice", "someone", "somebody",
        "unknown", "unknown speaker", "the man", "the woman", "the boy", "the girl",
    }
)
_PLACEHOLDER_PARTS = (
    "unnamed",
    "unknown speaker",
    "unnamed speaker",
    "speaker (",
    "off-screen",
    "offscreen",
    "voice-over",
    "voiceover",
)
# "the man on the phone" is a description; "The Inspector" is how the script
# names someone, so only these very plain ones are treated as placeholders.
_PLACEHOLDER_PREFIXES = ("the man", "the woman", "the boy", "the girl")


def is_placeholder_name(name: str) -> bool:
    """True for 'He', 'First speaker (unnamed)' and similar non-characters."""
    key = " ".join(name.casefold().split())
    return (
        key in _PLACEHOLDER_NAMES
        or any(key == prefix or key.startswith(prefix + " ") for prefix in _PLACEHOLDER_PREFIXES)
        or any(part in key for part in _PLACEHOLDER_PARTS)
    )


def character_from_mapping(data: Mapping[str, Any]) -> Character | None:
    """One character, or None when the model gave no name or only a placeholder."""
    name = _text(data.get("name"))
    if not name:
        return None
    if is_placeholder_name(name):
        log.info("Dropping unnamed speaker %r from the bible", name)
        return None
    return Character(
        name=name,
        gender=_text(data.get("gender")),
        age=_text(data.get("age")),
        role=_text(data.get("role")),
        relationships=_text(data.get("relationships") or data.get("relationship")),
        register=_text(data.get("register")),
    )


def term_from_mapping(data: Mapping[str, Any]) -> glossary.Term | None:
    """One glossary term, or None when the model gave nothing usable.

    An unknown action is repaired the way the term clearly meant it: a Thai
    spelling becomes transliterate, no spelling becomes keep. Losing a name in
    the glossary is worse than guessing which column its Thai belongs in.
    """
    english = _text(data.get("en") or data.get("term") or data.get("english"))
    if not english:
        return None

    thai = _text(data.get("th") or data.get("thai"))
    action = _text(data.get("action")).lower()
    if action not in glossary.ACTIONS:
        if action:
            log.warning("Term %r: unknown action %r, using %s", english, action,
                        "transliterate" if thai else "keep")
        action = "transliterate" if thai else "keep"

    if action != "keep" and not thai:
        log.warning("Term %r: %s needs Thai, so keeping it in English", english, action)
        action = "keep"

    return glossary.Term(en=english, action=action, th=thai)


def bible_from_mapping(
    data: Mapping[str, Any], title: str = "", year: str = ""
) -> Bible:
    """Normalise one notes object into a Bible.

    The title and year always come from the filename: the model may not know
    them, and a guessed title is more likely wrong than the file's own name.
    """
    characters: list[Character] = []
    seen: set[str] = set()
    for item in _tables(data, "characters"):
        character = character_from_mapping(item)
        if character is None or character.name.casefold() in seen:
            continue
        seen.add(character.name.casefold())
        characters.append(character)

    terms = [
        term
        for term in (term_from_mapping(item) for item in _tables(data, "terms"))
        if term is not None
    ]

    return Bible(
        title=title,
        year=year,
        summary=_text(data.get("summary")),
        notes=_text(data.get("notes") or data.get("setting")),
        characters=characters,
        terms=glossary.unique(terms),
    )


# --- talking to the model ---------------------------------------------------


def _add_usage(total: llm.Usage, extra: llm.Usage) -> llm.Usage:
    return total + extra


def _ask(
    client: llm.ChatClient,
    messages: list[dict[str, str]],
    attempts: int = MAX_JSON_ATTEMPTS,
) -> tuple[Mapping[str, Any], llm.Usage, int]:
    """Ask for one notes object, re-asking once if the reply is not JSON.

    A malformed reply is not worth a backoff: the model answered, it just
    answered in prose, so the re-ask carries the bad reply and a short reminder
    at a colder temperature. Transport errors are left to the caller.
    """
    usage = llm.Usage()
    calls = 0
    for attempt in range(1, attempts + 1):
        temperature = PREPASS_TEMPERATURE if attempt == 1 else RETRY_TEMPERATURE
        completion = client.complete(messages, temperature=temperature)
        calls += 1
        usage = _add_usage(usage, completion.usage)
        try:
            return parse_json(completion.text), usage, calls
        except BibleError as exc:
            log.warning("%s", exc)
            if attempt >= attempts:
                raise
            messages = list(messages) + [
                {"role": "assistant", "content": completion.text},
                {
                    "role": "user",
                    "content": "That was not valid JSON. Reply again with the "
                    "JSON object only, no code fences and no other text.",
                },
            ]
    raise BibleError("the model gave no usable notes")  # pragma: no cover


def build(
    client: llm.ChatClient,
    entries: Sequence[SourceEntry],
    title: str = "",
    year: str = "",
    max_chars: int = CHUNK_CHARS,
    attempts: int = MAX_JSON_ATTEMPTS,
) -> BibleResult:
    """Read `entries` and return the story bible and glossary they imply.

    One part is used as it comes back. Several parts are merged with one extra
    call, so a character who only appears late still gets a consistent register.
    """
    ordered = list(entries)
    if not ordered:
        raise BibleError("There is no text to read for the story bible")

    chunks = chunk_entries(ordered, max_chars)
    log.info(
        "Reading %d entr%s in %d part(s) for the story bible",
        len(ordered),
        "y" if len(ordered) == 1 else "ies",
        len(chunks),
    )

    result = BibleResult(bible=Bible(title=title, year=year))
    parts: list[Mapping[str, Any]] = []
    for number, chunk in enumerate(chunks, 1):
        data, usage, calls = _ask(
            client, notes_messages(title, year, render_script(chunk)), attempts
        )
        result.usage = _add_usage(result.usage, usage)
        result.calls += calls
        parts.append(data)
        log.info("Part %d/%d read (%d entries)", number, len(chunks), len(chunk))

    if len(parts) > 1:
        data, usage, calls = _ask(
            client, merge_messages(title, year, parts), attempts
        )
        result.usage = _add_usage(result.usage, usage)
        result.calls += calls
        log.info("Merged %d parts into one story bible", len(parts))
    else:
        data = parts[0]

    result.bible = bible_from_mapping(data, title=title, year=year)
    return result


def write(
    bible: Bible, bible_path: str | Path, glossary_path: str | Path
) -> tuple[Path, Path]:
    """Write both hand-editable files, atomically, as UTF-8."""
    bible_path, glossary_path = Path(bible_path), Path(glossary_path)
    srtio._atomic_write(glossary_path, bible.to_glossary_toml().encode("utf-8"))
    srtio._atomic_write(bible_path, bible.to_markdown().encode("utf-8"))
    log.info("Wrote %s and %s", bible_path.name, glossary_path.name)
    return bible_path, glossary_path


# --- reading them back ------------------------------------------------------

# The "how this file was made" note is for the human reader, not the model.
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


def load(path: str | Path) -> str:
    """Read a bible a human may have edited, as it is on disk right now.

    A missing file simply means no bible: that is a normal way to work, not an
    error. An existing file that cannot be read is an error, because silently
    translating without the notes you wrote would be worse than stopping.
    """
    path = Path(path)
    if not path.exists():
        return ""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise BibleError(f"could not read {path}: {exc}") from exc
    return _HTML_COMMENT_RE.sub("", text).strip()


@dataclass(frozen=True)
class Notes:
    """The hand-editable files beside one source, read fresh for this run."""

    bible: str = ""
    terms: list[glossary.Term] = field(default_factory=list)
    bible_path: Path | None = None
    glossary_path: Path | None = None

    @property
    def has_bible(self) -> bool:
        return bool(self.bible.strip())

    @property
    def has_glossary(self) -> bool:
        return self.glossary_path is not None


def load_notes(source: str | Path) -> Notes:
    """Read the bible and glossary beside `source`, right now.

    Nothing is kept between calls or runs, so a hand edit applies to the very
    next run with no rebuild step. A missing file is absent, not an error; an
    unreadable bible or a broken glossary raises, so the caller can stop before
    spending any money.
    """
    bible_path = srtio.sidecar_path(source, ".bible.md")
    glossary_path = srtio.sidecar_path(source, ".glossary.toml")
    return Notes(
        bible=load(bible_path),
        terms=glossary.load(glossary_path),
        bible_path=bible_path if bible_path.exists() else None,
        glossary_path=glossary_path if glossary_path.exists() else None,
    )
