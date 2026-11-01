"""Chat-API client (OpenRouter by default) and the tagged wire format.

Text goes out as one line per entry:

    [12] Hello there.
    How are you?
    [13] Fine, thanks.

and comes back the same way. Anything that is not a line starting with [N] is a
continuation of the entry above it, so multi-line subtitles survive the trip.

Transport failures are translated into typed errors. Nothing here retries:
the caller (step 5) decides what is worth re-requesting. The API key is read
from the environment only, and never logged, stored or rendered.
"""

from __future__ import annotations

import logging
import math
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Mapping, Protocol, Sequence

import tags
from batcher import Batch, ContextLine, SourceEntry

log = logging.getLogger(__name__)

PROMPT_VERSION = 3
DEFAULT_KEY_ENV = "OPENROUTER_API_KEY"
DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"

# A new entry starts at "[12]"; everything else continues the one before it.
_ENTRY_LINE_RE = re.compile(r"^\[(\d+)\]\s?(.*)$")


# --- errors -----------------------------------------------------------------


class LLMError(Exception):
    """Base class for anything that went wrong talking to the model."""


class RetryableError(LLMError):
    """A failure that may go away by itself, so trying again is worth it.

    `retry_after` is the server's hint in seconds, if it sent one.
    """

    def __init__(self, message: str, retry_after: float | None = None):
        self.retry_after = retry_after
        super().__init__(message)


class TransportError(RetryableError):
    """Network failure, timeout or a 5xx from the server; worth retrying."""


class RateLimited(RetryableError):
    """HTTP 429."""


class AuthError(LLMError):
    """Missing or rejected API key; never worth retrying."""


class BadResponse(LLMError):
    """The reply could not be parsed at all."""


# --- wire format ------------------------------------------------------------


@dataclass(frozen=True)
class Issue:
    """A problem found in a reply: parsing, identity or language."""

    kind: str
    # parsing:  "duplicate" | "empty"
    # identity: "missing" | "extra"
    # language: "missing_thai" | "foreign_script" | "lost_token" | "mixed_script"
    # tags:     "tag_mismatch"
    # other:    "bad_response"
    index: int | None
    message: str


# Name kept for the parsing-only case, which reads better at the call site.
DecodeIssue = Issue


@dataclass
class Decoded:
    entries: dict[int, str] = field(default_factory=dict)
    issues: list[DecodeIssue] = field(default_factory=list)


def encode_entries(items: Sequence[SourceEntry]) -> str:
    """Render entries as tagged lines, one per entry."""
    return "\n".join(f"[{entry.index}] {entry.text}" for entry in items)


def decode_entries(text: str) -> Decoded:
    """Parse a tagged reply into {index: text}.

    Unprefixed lines continue the previous entry. Duplicate indexes keep the
    first occurrence and are reported, never merged.
    """
    decoded = Decoded()
    current: int | None = None
    buffer: list[str] = []

    def flush() -> None:
        if current is None:
            return
        content = "\n".join(buffer).strip()
        if not content:
            decoded.issues.append(
                DecodeIssue("empty", current, f"entry [{current}] came back empty")
            )
        decoded.entries[current] = content

    for line in text.splitlines():
        match = _ENTRY_LINE_RE.match(line)
        if match:
            flush()
            index = int(match.group(1))
            if index in decoded.entries:
                decoded.issues.append(
                    DecodeIssue("duplicate", index, f"entry [{index}] appeared twice")
                )
                # Keep the first copy; swallow the second so it cannot overwrite.
                current, buffer = None, []
                continue
            current, buffer = index, [match.group(2)]
        elif current is not None:
            buffer.append(line)

    flush()

    if not decoded.entries:
        raise BadResponse("reply contained no [N] tagged lines")
    return decoded


# --- language checking ------------------------------------------------------

# Scripts the model might slip into. Thai is the target; the rest are leaks
# unless the English source already contained them (an on-screen sign, a name).
_SCRIPT_RANGES: dict[str, tuple[tuple[int, int], ...]] = {
    "thai": ((0x0E00, 0x0E7F),),
    "han": ((0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xF900, 0xFAFF)),
    "kana": ((0x3040, 0x30FF),),
    "hangul": ((0x1100, 0x11FF), (0xAC00, 0xD7AF)),
    "cyrillic": ((0x0400, 0x04FF),),
}

# Words that carry no meaning to translate: "Mm-hmm", "Shh", "Ah.".
_NON_VERBAL_WORDS = {
    "mm", "hmm", "hm", "uh", "ah", "oh", "ooh", "shh", "sh", "ha", "hey",
    "wow", "argh", "ugh", "er", "erm", "eh", "huh", "tsk", "psst", "phew",
    "ow", "ouch", "mhm", "mmm", "uhh", "ahh", "ohh", "hah", "heh", "huh",
}

_STRAY_MARKUP_RE = re.compile(r"<[^>]*>|\{\\[^}]*\}|\{[^}]*\}|\[[^\]]*\]|\([^)]*\)|[♪♫♬♩]")
_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)


def scripts_in(text: str) -> set[str]:
    """Which writing systems appear in `text`."""
    found: set[str] = set()
    for character in text:
        point = ord(character)
        for name, ranges in _SCRIPT_RANGES.items():
            if any(low <= point <= high for low, high in ranges):
                found.add(name)
                break
    return found


def has_thai(text: str) -> bool:
    return "thai" in scripts_in(text)


def is_non_verbal(text: str) -> bool:
    """True for text with nothing to translate: interjections, sounds, symbols.

    Deliberately conservative: an uncertain case is treated as verbal, so a
    real line never slips through untranslated.
    """
    stripped = _STRAY_MARKUP_RE.sub(" ", text)
    words = [word.lower().strip("'") for word in _WORD_RE.findall(stripped)]
    if not words:
        return True
    return all(
        word in _NON_VERBAL_WORDS or len(set(word)) == 1 or re.fullmatch(r"(?:ha)+|(?:mm)+|(?:h+m+)|(?:a+h+)|(?:o+h+)", word)
        for word in words
    )


# --- tokens that must survive translation -----------------------------------
#
# The transliteration policy turns Latin into Thai, so the few Latin tokens that
# are not translated need protecting: links, handles, e-mail and letter-digit
# codes. A short code stays as written; a longer one has its letters
# transliterated and only its digits must come through.

_URL_RE = re.compile(r"(?:https?://|www\.)[^\s<>\"')]+", re.IGNORECASE)
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_HANDLE_RE = re.compile(r"@[A-Za-z0-9_]{2,}")
_DIGITS_RE = re.compile(r"\d+")
# Sentence punctuation after a token belongs to the sentence, not to the token:
# "See https://example.com, then" must still match when the translation writes
# Thai punctuation there. Without this a correct translation looks like a lost
# token and is retried until it falls back to English.
_TRAILING_PUNCT = ".,;:!?…"

# Letters and digits mixed in one token: K9, 21L4, Flymiamibro22.
_MIXED_RE = re.compile(
    r"(?<![A-Za-z0-9])"
    r"(?=[A-Za-z0-9]*[0-9])(?=[A-Za-z0-9]*[A-Za-z])"
    r"[A-Za-z0-9]{2,}"
    r"(?![A-Za-z0-9])"
)

# Letter-digit tokens shorter than this stay in Latin; longer ones are
# transliterated and only their digits have to survive (Flymiamibro22).
KEPT_CODE_MIN_LENGTH = 5


def _token_text(text: str) -> str:
    """Text with tags and placeholders removed, brackets and words kept."""
    return tags.strip_markup(text)


def _trim_token(token: str) -> str:
    """A matched token without the sentence punctuation that follows it."""
    return token.rstrip(_TRAILING_PUNCT)


def kept_verbatim(text: str) -> list[str]:
    """Latin tokens the translation must contain unchanged, in order."""
    cleaned = _token_text(text)
    found: list[str] = []
    for pattern in (_URL_RE, _EMAIL_RE, _HANDLE_RE):
        for match in pattern.finditer(cleaned):
            token = _trim_token(match.group(0))
            if token:
                found.append(token)
    for match in _MIXED_RE.finditer(cleaned):
        token = match.group(0)
        if len(token) < KEPT_CODE_MIN_LENGTH:
            found.append(token)
    return found


def must_keep_digits(text: str) -> list[str]:
    """Digit runs that must come through: every number, and long codes."""
    cleaned = _token_text(text)
    found = list(_DIGITS_RE.findall(cleaned))
    for match in _MIXED_RE.finditer(cleaned):
        if len(match.group(0)) >= KEPT_CODE_MIN_LENGTH:
            found.extend(_DIGITS_RE.findall(match.group(0)))
    unique: list[str] = []
    for value in found:
        if value not in unique:
            unique.append(value)
    return unique


def missing_kept(source: str, translated: str) -> list[str]:
    """Tokens or digits from `source` that `translated` lost, in order."""
    cleaned = _token_text(translated)
    missing: list[str] = []
    for token in kept_verbatim(source):
        if token not in cleaned and token not in missing:
            missing.append(token)
    for digits in must_keep_digits(source):
        if digits not in cleaned and digits not in missing:
            missing.append(digits)
    return missing


# Latin letters immediately next to Thai. Thai is written without spaces, so a
# kept token is always delimited by a space or punctuation; a Latin run that
# touches Thai is a fragment the model mangled ("ทwerp" for "Bullshit").
_THAI_CHAR_RE = re.compile(r"[\u0E00-\u0E7F]")
_LATIN_RUN_RE = re.compile(r"[A-Za-z]+(?:['\u2019][A-Za-z]+)*")


def mixed_script(text: str) -> list[str]:
    """Latin runs glued to Thai, in order, without duplicates.

    Kept tokens (links, handles, e-mail, short letter-digit codes) are blanked
    first, so a code that legitimately touches Thai is not reported.
    """
    cleaned = tags.strip_markup(text)
    for token in kept_verbatim(text):
        cleaned = cleaned.replace(token, " ")

    found: list[str] = []
    for match in _LATIN_RUN_RE.finditer(cleaned):
        before = cleaned[match.start() - 1] if match.start() else ""
        after = cleaned[match.end()] if match.end() < len(cleaned) else ""
        if not (_THAI_CHAR_RE.match(before) or _THAI_CHAR_RE.match(after)):
            continue
        if match.group(0) not in found:
            found.append(match.group(0))
    return found


def validate(batch: Batch, decoded: Decoded) -> list[Issue]:
    """Check a parsed reply against the batch that asked for it.

    Returns every problem found, so the caller can retry exactly the affected
    entries. An empty list means the reply is safe to ship.
    """
    expected = {entry.index: entry.text for entry in batch.items}
    issues: list[Issue] = list(decoded.issues)

    for index, source in expected.items():
        if index not in decoded.entries:
            issues.append(
                Issue("missing", index, f"entry [{index}] is missing from the reply")
            )
            continue

        text = decoded.entries[index]
        if not text.strip():
            continue  # decode already reported it as empty

        if not is_non_verbal(source):
            leaked = scripts_in(text) - scripts_in(source) - {"thai"}
            if leaked:
                issues.append(
                    Issue(
                        "foreign_script",
                        index,
                        f"entry [{index}] contains {'/'.join(sorted(leaked))}: {text[:40]!r}",
                    )
                )
            if not has_thai(text):
                issues.append(
                    Issue(
                        "missing_thai",
                        index,
                        f"entry [{index}] has no Thai: {text[:40]!r}",
                    )
                )
            glued = mixed_script(text)
            if glued:
                issues.append(
                    Issue(
                        "mixed_script",
                        index,
                        f"entry [{index}] glues Latin to Thai: {'/'.join(glued)}",
                    )
                )

        lost = missing_kept(source, text)
        if lost:
            issues.append(
                Issue(
                    "lost_token",
                    index,
                    f"entry [{index}] lost {'/'.join(lost)} from the translation",
                )
            )

        for tag_issue in tags.check(source, text):
            issues.append(
                Issue("tag_mismatch", index, f"entry [{index}]: {tag_issue.message}")
            )

    for index in decoded.entries:
        if index not in expected:
            issues.append(
                Issue("extra", index, f"entry [{index}] was not requested")
            )

    return issues


# --- prompt -----------------------------------------------------------------

SYSTEM_PROMPT = """\
You are a professional subtitle translator working from English into Thai.

Output only the translated lines, in the same order, each keeping its [ID] prefix.
No notes, no explanations, no romanisation, no commentary.

Rules:
- Natural, casual spoken Thai. Not literary Thai.
- Subtitles are read quickly: be concise.
- Thai subtitle convention: spaces instead of commas and periods.
- Use the story bible for pronouns, particles and register per speaker.
- Write everything in Thai script. Translate or transliterate names, nicknames, brands,
  song and film titles, speaker labels and acronyms; leave no English word untranslated.
- A speaker label keeps its colon and stays on the same line as the words it introduces:
  "MAYA: No." is one line and becomes "เมย่า: ไม่" — never put the label alone on a line.
- Keep each entry's line count exactly. If the source entry has two lines, the translation
  has two lines: never merge them, never add a line the source did not have, and never put
  a lone dash on its own line.
- Transliterate the way a Thai speaker would say it, and use a settled Thai spelling when
  one exists (Chanel -> ชาแนล, Swarovski -> สวารอฟสกี้).
- A title of another work: use its official Thai title only if you are certain of it,
  otherwise transliterate the title. Never invent an official-sounding title.
- Acronyms: spell the letters when people say the letters (AOL -> เอโอเอล, JBL -> เจบีแอล,
  FBI agent -> เอฟบีไอ เอเจนต์); read it as a word when that is how it is said
  (AIM -> เอม, NASA -> นาซา).
- Numbers stay exactly as written: 1, 2, 1,000, 06:00, 5%. Never spell a digit as a word
  (15 stays 15, so Pen15 -> เพน15, not เพนฟิฟทีน). Keep URLs, e-mail addresses and
  @handles unchanged.
- A token mixing letters and digits: keep it as written when it is shorter than 5
  characters (K191, K9, 3B); when it has 5 characters or more, transliterate its letters
  and keep its digits (Flymiamibro22 -> ฟลายมยเอมีโบร22).
- Follow the glossary exactly: a term marked "keep" stays in English; for "transliterate"
  or "translate", use the Thai given after the colon every time.
- Translate song lyrics for meaning; keep the music markers.
- Profanity: translate at equivalent strength; do not soften it.
- Idioms: translate the meaning, not the words.
- On-screen text and sound effects: translate them, keep the brackets.
- Never merge or split entries. One input ID = one output ID.
- Lines under CONTEXT are for understanding only and must not appear in your output.
- Placeholders such as {0} are formatting tags: keep every one, exactly once, in place.
"""


def _format_context(lines: Sequence[ContextLine], include_thai: bool) -> str:
    rendered = []
    for line in lines:
        rendered.append(f"[{line.index}] {line.text}")
        if include_thai and line.thai:
            rendered.append(f"TH: {line.thai}")
    return "\n".join(rendered)


# Added to the system prompt for --fix, where every line was flagged by the
# verification pass. It repeats the rules that matter for a flagged line,
# because a plain re-ask tends to produce the same mistake again.
FIX_RULES = """\
This is a correction pass. A checker flagged every line below in an earlier attempt,
so translate each of them again from scratch, with extra care:
- Every entry must be natural Thai. No other language, and no English except numbers,
  URLs, @handles and short letter-digit codes such as K9.
- Transliterate names, brands, labels and acronyms instead of keeping them in English.
- Use the glossary spelling exactly as given for every term that appears.
- Keep every [ID] exactly once, in the same order, and keep each entry's number of lines.
- Keep every placeholder such as {0} exactly once, in its place.
- Do not repeat the earlier wording: write a fresh translation of the source."""


# Added to the system prompt for --review. The same wire format comes back, so
# a suggestion is validated exactly like a translation.
REVIEW_RULES = """\
This is a review pass. The checker flagged the lines below, and their current translations
are shown with each one. For every entry, either write a better Thai line or repeat the
current one unchanged if it is already right. A current line still in English means the
earlier attempt failed: propose a Thai line for it.
- Output every [ID] exactly once, in the same order, with the same number of lines.
- Keep placeholders such as {0} exactly once, in their place.
- Thai only, except glossary words marked "keep"; use the glossary spelling exactly.
- Keep each line short enough to read at a glance."""


def build_messages(
    batch: Batch,
    bible: str = "",
    glossary: str = "",
    strict: bool = False,
    current: Mapping[int, str] | None = None,
) -> list[dict[str, str]]:
    """Build the chat messages for one batch.

    `strict` adds the correction rules used by --fix for flagged lines. `current`
    maps entry index -> the translation already made; it is shown to the model
    and switches the prompt to the review rules (--review).
    """
    sections: list[str] = []

    if bible.strip():
        sections.append("[STORY BIBLE]\n" + bible.strip())
    if glossary.strip():
        sections.append("[GLOSSARY]\n" + glossary.strip())
    if current:
        shown = [
            f"[{entry.index}] {current[entry.index]}"
            for entry in batch.items
            if entry.index in current
        ]
        if shown:
            sections.append(
                "[CURRENT TRANSLATIONS - flagged, improve or repeat]\n" + "\n".join(shown)
            )
    if batch.before:
        sections.append(
            "[CONTEXT BEFORE - do not translate]\n"
            + _format_context(batch.before, include_thai=True)
        )
    if batch.after:
        sections.append(
            "[CONTEXT AFTER - do not translate]\n"
            + _format_context(batch.after, include_thai=False)
        )
    sections.append("[TRANSLATE]\n" + encode_entries(batch.items))

    rules = REVIEW_RULES if current else (FIX_RULES if strict else "")
    system = SYSTEM_PROMPT + (f"\n\n{rules}" if rules else "")
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "\n\n".join(sections)},
    ]


# --- client -----------------------------------------------------------------


@dataclass(frozen=True)
class Usage:
    """What a call cost, as the provider reports it.

    `completion_tokens` already includes `reasoning_tokens`, so reasoning is a
    subset and must not be added on. `cost_usd` is None when the provider does
    not report a price, which is anything other than OpenRouter.
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    cached_tokens: int = 0
    cost_usd: float | None = None

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            reasoning_tokens=self.reasoning_tokens + other.reasoning_tokens,
            cached_tokens=self.cached_tokens + other.cached_tokens,
            cost_usd=_add_cost(self.cost_usd, other.cost_usd),
        )


def _add_cost(left: float | None, right: float | None) -> float | None:
    """Sum two costs, keeping whichever is known when only one is."""
    if left is None:
        return right
    if right is None:
        return left
    return left + right


def _reported(obj: object, name: str, default=None):
    """A field from a provider object, including ones the SDK did not model.

    OpenRouter adds `cost` and the `*_tokens_details` blocks. A typed SDK may
    keep them in `model_extra` rather than as attributes, so both are checked.
    """
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    value = getattr(obj, name, None)
    if value is None:
        extra = getattr(obj, "model_extra", None) or {}
        value = extra.get(name)
    return default if value is None else value


def usage_from_response(response: object) -> Usage:
    """The usage block of a reply, including reasoning tokens and the cost.

    Everything here is optional: a provider that reports only token counts
    yields a Usage with zeros and no cost, which the summary prints honestly.
    """
    usage = getattr(response, "usage", None)
    if usage is None:
        return Usage()
    completion_details = _reported(usage, "completion_tokens_details")
    prompt_details = _reported(usage, "prompt_tokens_details")
    cost = _reported(usage, "cost")
    return Usage(
        prompt_tokens=int(_reported(usage, "prompt_tokens", 0) or 0),
        completion_tokens=int(_reported(usage, "completion_tokens", 0) or 0),
        reasoning_tokens=int(_reported(completion_details, "reasoning_tokens", 0) or 0),
        cached_tokens=int(_reported(prompt_details, "cached_tokens", 0) or 0),
        cost_usd=float(cost) if cost is not None else None,
    )


@dataclass(frozen=True)
class Completion:
    text: str
    usage: Usage = field(default_factory=Usage)


class ChatClient(Protocol):
    """What the rest of the program needs from a model."""

    def complete(
        self, messages: Sequence[dict[str, str]], temperature: float | None = None
    ) -> Completion: ...


# Anything shaped like a credential. Used to keep secrets out of error text
# when someone puts a key where a variable name belongs.
_SECRET_SHAPED_RE = re.compile(r"^sk-(or-)?[A-Za-z0-9_-]{16,}$")


def looks_like_key(value: str) -> bool:
    """True if `value` looks like an API key rather than a variable name.

    Used to catch a key pasted into config.toml (key_env, extra_headers) and to
    say so without repeating it.
    """
    return bool(_SECRET_SHAPED_RE.match(value.strip()))


def load_api_key(key_env: str = DEFAULT_KEY_ENV) -> str:
    """Read the API key from the environment. Never logs it."""
    if looks_like_key(key_env):
        # The user pasted the key into key_env. Say so without repeating it.
        raise AuthError(
            "config key_env holds something that looks like an API key.\n"
            "key_env must be the NAME of the variable, e.g. \"OPENROUTER_API_KEY\".\n"
            "Put the key itself in your shell environment instead."
        )

    key = os.environ.get(key_env, "").strip()
    if not key:
        raise AuthError(
            f"{key_env} is not set. Export it in your shell, e.g.\n"
            f"  export {key_env}=your-key-here"
        )
    return key


def _retry_after(exc: Exception) -> float | None:
    """Pull Retry-After off a failed response, if the server sent a usable one.

    The header is either a number of seconds or an HTTP date. Anything that is
    not a finite, non-negative delay is ignored, so a bad header can never turn
    into a weird sleep or a crash.
    """
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if not headers:
        return None
    raw = headers.get("retry-after")
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("latin-1", "replace")

    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        seconds = None

    if seconds is None:
        try:
            when = parsedate_to_datetime(str(raw))
        except (TypeError, ValueError):
            return None
        if when is None:
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        seconds = (when - datetime.now(timezone.utc)).total_seconds()

    if not math.isfinite(seconds) or seconds < 0:
        return None
    return seconds


class OpenRouterClient:
    """An OpenAI-compatible chat API over the OpenAI SDK.

    OpenRouter by default, but base_url and key_env come from config, so any
    OpenAI-compatible endpoint (DeepSeek, Groq, a local vLLM...) works the same
    way. SDK retries are disabled: the runner owns the retry budget.
    """

    def __init__(
        self,
        model: str,
        api_key: str | None = None,
        base_url: str = DEFAULT_BASE_URL,
        temperature: float = 0.25,
        timeout_s: float = 120,
        key_env: str = DEFAULT_KEY_ENV,
        extra_headers: Mapping[str, str] | None = None,
    ):
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise LLMError("The openai package is not installed") from exc

        self.model = model
        self.temperature = temperature
        self._client = OpenAI(
            api_key=api_key or load_api_key(key_env),
            base_url=base_url,
            timeout=timeout_s,
            max_retries=0,  # retries are decided by the caller, not the SDK
            default_headers=dict(extra_headers) if extra_headers else None,
        )

    def complete(
        self, messages: Sequence[dict[str, str]], temperature: float | None = None
    ) -> Completion:
        """Send one completion request.

        `temperature` overrides the client default for this call only, which is
        how retries are made less random without rebuilding the client.
        """
        import openai

        try:
            response = self._client.chat.completions.create(
                model=self.model,
                messages=list(messages),
                temperature=self.temperature if temperature is None else temperature,
            )
        except openai.RateLimitError as exc:
            retry_after = _retry_after(exc)
            raise RateLimited(
                "rate limited by the API", retry_after=retry_after
            ) from None
        except openai.AuthenticationError as exc:
            raise AuthError(
                "the API rejected the API key (check the variable named in [llm] key_env)"
            ) from None
        except openai.APITimeoutError as exc:
            raise TransportError("request timed out") from None
        except openai.APIConnectionError as exc:
            raise TransportError(f"connection failed: {exc}") from None
        except openai.APIStatusError as exc:
            # 5xx and the "try again" 4xx codes are the gateway having a bad
            # moment, not a bad request: retry them like a timeout.
            if exc.status_code >= 500 or exc.status_code in {408, 409, 425}:
                raise TransportError(
                    f"the API returned HTTP {exc.status_code}",
                    retry_after=_retry_after(exc),
                ) from None
            raise LLMError(f"the API returned HTTP {exc.status_code}") from None

        choices = getattr(response, "choices", None) or []
        if not choices:
            raise BadResponse("reply contained no choices")
        text = choices[0].message.content or ""
        return Completion(text=text, usage=usage_from_response(response))
