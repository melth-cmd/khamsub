"""Loading, validating and applying config.toml.

Every setting has a built-in default, so a missing file is not fatal. Values are
type-checked and range-checked on load: a typo should fail immediately with a
clear message, not four hundred subtitles into a paid run.

The API key is not a setting. It is read from the environment named by
[llm] key_env (or ./.env when that is empty), at the moment the client is
built, and never stored.
"""

from __future__ import annotations

import codecs
import logging
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import llm

log = logging.getLogger(__name__)

DEFAULT_PATH = Path(__file__).with_name("config.toml")
IMPLEMENTED_WIRE_FORMATS = ("tagged",)


class ConfigError(Exception):
    """The configuration is missing, malformed or out of range."""


@dataclass(frozen=True)
class LLMSettings:
    base_url: str = llm.DEFAULT_BASE_URL
    key_env: str = llm.DEFAULT_KEY_ENV
    model: str = ""
    temperature: float = 0.25
    timeout_s: float = 120.0
    max_retries: int = 3
    wire_format: str = "tagged"
    input_usd_per_mtok: float = 0.0
    output_usd_per_mtok: float = 0.0
    # Extra request headers, for OpenAI-compatible providers that ask for them.
    extra_headers: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class BatchSettings:
    size: int = 60
    context_before: int = 4
    context_after: int = 2


@dataclass(frozen=True)
class OutputSettings:
    encoding: str = "utf-8"
    bilingual: bool = False
    strip_en_suffix: bool = True


@dataclass(frozen=True)
class CleanupSettings:
    strip_hi: bool = False


@dataclass(frozen=True)
class Settings:
    llm: LLMSettings = field(default_factory=LLMSettings)
    batch: BatchSettings = field(default_factory=BatchSettings)
    output: OutputSettings = field(default_factory=OutputSettings)
    cleanup: CleanupSettings = field(default_factory=CleanupSettings)
    path: Path | None = None


# --- value coercion ---------------------------------------------------------


def _table(data: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = data.get(name, {})
    if not isinstance(value, Mapping):
        raise ConfigError(f"[{name}] must be a table")
    return value


def _check_keys(section: Mapping[str, Any], known: set[str], name: str) -> None:
    for key in sorted(set(section) - known):
        log.warning("Ignoring unknown setting [%s] %s", name, key)


def _string(section: Mapping[str, Any], key: str, default: str, name: str) -> str:
    value = section.get(key, default)
    if not isinstance(value, str):
        raise ConfigError(f"[{name}] {key} must be a string, got {value!r}")
    return value


def _number(
    section: Mapping[str, Any],
    key: str,
    default: float,
    name: str,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    value = section.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"[{name}] {key} must be a number, got {value!r}")
    if minimum is not None and value < minimum:
        raise ConfigError(f"[{name}] {key} must be at least {minimum}, got {value!r}")
    if maximum is not None and value > maximum:
        raise ConfigError(f"[{name}] {key} must be at most {maximum}, got {value!r}")
    return float(value)


def _integer(
    section: Mapping[str, Any],
    key: str,
    default: int,
    name: str,
    minimum: int | None = None,
) -> int:
    value = section.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"[{name}] {key} must be a whole number, got {value!r}")
    if minimum is not None and value < minimum:
        raise ConfigError(f"[{name}] {key} must be at least {minimum}, got {value!r}")
    return value


def _boolean(section: Mapping[str, Any], key: str, default: bool, name: str) -> bool:
    value = section.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"[{name}] {key} must be true or false, got {value!r}")
    return value


def _headers(section: Mapping[str, Any], key: str, name: str) -> dict[str, str]:
    """An optional table of extra request headers for another provider.

    The key itself stays in the environment: an Authorization header or a
    key-shaped value is refused here, and never repeated in the message.
    """
    value = section.get(key, {})
    if not isinstance(value, Mapping):
        raise ConfigError(
            f"[{name}] {key} must be a table of header = \"value\" pairs"
        )

    headers: dict[str, str] = {}
    for header, content in value.items():
        if not isinstance(header, str) or not header.strip():
            raise ConfigError(f"[{name}] {key} needs a non-empty header name")
        if not isinstance(content, str):
            raise ConfigError(
                f"[{name}] {key}.{header} must be a string, got {content!r}"
            )
        if header.strip().casefold() == "authorization":
            raise ConfigError(
                f"[{name}] {key} must not set Authorization; the key comes from "
                "the environment variable named in key_env"
            )
        if llm.looks_like_key(content):
            raise ConfigError(
                f"[{name}] {key}.{header} looks like an API key; keys belong in "
                "the environment variable named in key_env"
            )
        headers[header.strip()] = content
    return headers


# --- parsing ----------------------------------------------------------------


def settings_from_dict(data: Mapping[str, Any], path: Path | None = None) -> Settings:
    """Build validated Settings from a parsed config.toml mapping."""
    llm_section = _table(data, "llm")
    _check_keys(
        llm_section,
        {
            "base_url", "key_env", "model", "temperature", "timeout_s",
            "max_retries", "wire_format", "input_usd_per_mtok", "output_usd_per_mtok",
            "extra_headers",
        },
        "llm",
    )
    wire_format = _string(llm_section, "wire_format", "tagged", "llm")
    if wire_format not in IMPLEMENTED_WIRE_FORMATS:
        raise ConfigError(
            f"[llm] wire_format {wire_format!r} is not implemented yet; "
            f"use one of {', '.join(IMPLEMENTED_WIRE_FORMATS)}"
        )

    llm_settings = LLMSettings(
        base_url=_string(llm_section, "base_url", llm.DEFAULT_BASE_URL, "llm"),
        key_env=_string(llm_section, "key_env", llm.DEFAULT_KEY_ENV, "llm"),
        model=_string(llm_section, "model", "", "llm"),
        temperature=_number(llm_section, "temperature", 0.25, "llm", 0.0, 2.0),
        timeout_s=_number(llm_section, "timeout_s", 120.0, "llm", 1.0),
        max_retries=_integer(llm_section, "max_retries", 3, "llm", 0),
        wire_format=wire_format,
        input_usd_per_mtok=_number(llm_section, "input_usd_per_mtok", 0.0, "llm", 0.0),
        output_usd_per_mtok=_number(llm_section, "output_usd_per_mtok", 0.0, "llm", 0.0),
        extra_headers=_headers(llm_section, "extra_headers", "llm"),
    )

    batch_section = _table(data, "batch")
    _check_keys(batch_section, {"size", "context_before", "context_after"}, "batch")
    batch_settings = BatchSettings(
        size=_integer(batch_section, "size", 60, "batch", 1),
        context_before=_integer(batch_section, "context_before", 4, "batch", 0),
        context_after=_integer(batch_section, "context_after", 2, "batch", 0),
    )

    output_section = _table(data, "output")
    _check_keys(output_section, {"encoding", "bilingual", "strip_en_suffix"}, "output")
    encoding = _string(output_section, "encoding", "utf-8", "output")
    try:
        codecs.lookup(encoding)
    except LookupError as exc:
        raise ConfigError(f"[output] encoding {encoding!r} is not a known codec") from exc

    output_settings = OutputSettings(
        encoding=encoding,
        bilingual=_boolean(output_section, "bilingual", False, "output"),
        strip_en_suffix=_boolean(output_section, "strip_en_suffix", True, "output"),
    )

    cleanup_section = _table(data, "cleanup")
    _check_keys(cleanup_section, {"strip_hi"}, "cleanup")
    cleanup_settings = CleanupSettings(
        strip_hi=_boolean(cleanup_section, "strip_hi", False, "cleanup"),
    )

    _check_keys(data, {"llm", "batch", "output", "cleanup"}, "root")
    return Settings(
        llm=llm_settings,
        batch=batch_settings,
        output=output_settings,
        cleanup=cleanup_settings,
        path=path,
    )


def load_settings(path: str | Path | None = None) -> Settings:
    """Read config.toml. With no path, fall back to built-in defaults."""
    explicit = path is not None
    config_path = Path(path) if explicit else DEFAULT_PATH

    if not config_path.exists():
        if explicit:
            raise ConfigError(f"Config file not found: {config_path}")
        log.warning("No config.toml found, using built-in defaults")
        return Settings()

    try:
        with config_path.open("rb") as handle:
            data = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{config_path}: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"Could not read {config_path}: {exc}") from exc

    return settings_from_dict(data, path=config_path)


def build_client(settings: Settings, api_key: str | None = None) -> llm.OpenRouterClient:
    """Create the chat client from settings, reading the key from the env or .env.

    The API is OpenRouter by default; any OpenAI-compatible endpoint works by
    changing base_url, key_env and model.
    """
    if not settings.llm.model:
        raise ConfigError("[llm] model is empty; set a model in config.toml")
    return llm.OpenRouterClient(
        model=settings.llm.model,
        api_key=api_key,
        base_url=settings.llm.base_url,
        temperature=settings.llm.temperature,
        timeout_s=settings.llm.timeout_s,
        key_env=settings.llm.key_env,
        extra_headers=settings.llm.extra_headers,
    )
