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
import paths

log = logging.getLogger(__name__)

# The current folder is checked first; --init-config writes the user-wide file.
DEFAULT_PATH = paths.current_config()
# The shipped example, copied by --init-config. After packaging this is package
# data next to config.py, so it travels with the wheel.
TEMPLATE_PATH = Path(__file__).with_name("config.toml")
IMPLEMENTED_WIRE_FORMATS = ("tagged",)

# Used only if the template file is missing from an install.
MINIMAL_CONFIG = """\
[llm]
model = "deepseek/deepseek-v4.1-flash-1017"
"""

ENV_TEMPLATE = """\
# Put your API key after the equals sign.
# The real environment wins if both are set.
OPENROUTER_API_KEY=
"""


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


def candidates(explicit: str | Path | None = None) -> list[tuple[str, Path]]:
    """(label, path) for config.toml, in the order they are tried."""
    rows: list[tuple[str, Path]] = []
    if explicit is not None:
        rows.append(("--config", Path(explicit)))
    rows.append(("current folder", DEFAULT_PATH))
    rows.append(("user config", paths.user_config()))
    return rows


def locate(explicit: str | Path | None = None) -> tuple[Path | None, str]:
    """The config file that would be used, and which rule chose it.

    Returns (None, "built-in defaults") when no file exists. An explicit path
    is returned even if it is missing, so the caller can report that.
    """
    if explicit is not None:
        return Path(explicit), "--config"
    for label, path in candidates():
        if path.exists():
            return path, label
    return None, "built-in defaults"


def key_env_from(path: str | Path | None) -> str:
    """[llm] key_env from a config file, without validating the rest.

    Used by --where: a broken config should still show which key name it would
    have used, not stop the report.
    """
    if path is None:
        return llm.DEFAULT_KEY_ENV
    try:
        with Path(path).open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return llm.DEFAULT_KEY_ENV
    section = data.get("llm")
    if isinstance(section, Mapping):
        value = section.get("key_env")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return llm.DEFAULT_KEY_ENV


def load_settings(path: str | Path | None = None) -> Settings:
    """Read config.toml: --config, current folder, user dir, then defaults.

    The first file that exists wins. Falling back to the user-wide file or to
    the built-in defaults is announced once, because a run with the wrong model
    or batch size is expensive to discover later.
    """
    explicit = path is not None
    config_path, chosen_by = locate(path)

    if config_path is None:
        log.warning(
            "no config.toml in the current folder or in %s; using built-in "
            "defaults (run `khamsub --init-config` to create it)",
            paths.short(paths.user_dir()),
        )
        return Settings()

    if not config_path.exists():
        raise ConfigError(f"Config file not found: {config_path}")

    if not explicit:
        if chosen_by == "user config":
            log.warning(
                "no config.toml in the current folder; using %s",
                paths.short(config_path),
            )
        else:
            log.debug("using config %s", paths.short(config_path))

    try:
        with config_path.open("rb") as handle:
            data = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{config_path}: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"Could not read {config_path}: {exc}") from exc

    return settings_from_dict(data, path=config_path)


def template_text() -> str:
    """The shipped example config, or a minimal one if it is missing."""
    try:
        return TEMPLATE_PATH.read_text(encoding="utf-8")
    except OSError:
        return MINIMAL_CONFIG


def init_config() -> list[tuple[Path, str]]:
    """Write the user-wide config.toml and .env. Never overwrites.

    Returns (path, status) for the CLI to print. The directory is private and
    the .env is 0600, because it will hold the key.
    """
    directory = paths.user_dir()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)

    written: list[tuple[Path, str]] = []

    config_path = paths.user_config()
    if config_path.exists():
        written.append((config_path, "left as is"))
    else:
        config_path.write_text(template_text(), encoding="utf-8")
        written.append((config_path, "created"))

    env_path = paths.user_env()
    if env_path.exists():
        written.append((env_path, "left as is"))
    else:
        env_path.write_text(ENV_TEMPLATE, encoding="utf-8")
        env_path.chmod(0o600)
        written.append((env_path, "created (0600)"))

    return written


def build_client(settings: Settings, api_key: str | None = None) -> llm.OpenRouterClient:
    """Create the chat client from settings, reading the key from the env or .env.

    The API is OpenRouter by default; any OpenAI-compatible endpoint works by
    changing base_url, key_env and model.
    """
    if not settings.llm.model:
        if settings.path is not None:
            raise ConfigError(
                f"[llm] model is empty. Set [llm] model in {paths.short(settings.path)}."
            )
        raise ConfigError(
            "[llm] model is empty and no config.toml was found. Run "
            f"`khamsub --init-config` to create {paths.short(paths.user_config())}, "
            "or put a config.toml in the current folder."
        )
    return llm.OpenRouterClient(
        model=settings.llm.model,
        api_key=api_key,
        base_url=settings.llm.base_url,
        temperature=settings.llm.temperature,
        timeout_s=settings.llm.timeout_s,
        key_env=settings.llm.key_env,
        extra_headers=settings.llm.extra_headers,
    )
