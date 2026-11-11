"""Where the tool looks for config.toml and .env.

One rule for both files, so they never disagree:

  --config PATH        explicit (config only)
  ./config.toml        the folder you run in
  ~/.config/khamsub/   the user-wide defaults
  built-in defaults

The real environment always beats a .env file for the key.

`XDG_CONFIG_HOME` is honoured when set, so the user directory is
$XDG_CONFIG_HOME/khamsub on systems that use it.
"""

from __future__ import annotations

import os
from pathlib import Path

CONFIG_NAME = "config.toml"
ENV_NAME = ".env"
TOOL_DIR = "khamsub"


def current_config() -> Path:
    return Path(CONFIG_NAME)


def current_env() -> Path:
    return Path(ENV_NAME)


def user_dir() -> Path:
    """The user-wide directory: $XDG_CONFIG_HOME/khamsub or ~/.config/khamsub."""
    override = os.environ.get("XDG_CONFIG_HOME", "").strip()
    base = Path(override) if override else Path.home() / ".config"
    return base / TOOL_DIR


def user_config() -> Path:
    return user_dir() / CONFIG_NAME


def user_env() -> Path:
    return user_dir() / ENV_NAME


def env_candidates() -> list[tuple[str, Path]]:
    """(label, path) for .env, in the order they are tried."""
    return [("current folder", current_env()), ("user config", user_env())]


def short(path: str | Path) -> str:
    """$HOME becomes ~, so messages and --where stay readable."""
    path = Path(path)
    try:
        return "~/" + str(path.expanduser().relative_to(Path.home()))
    except ValueError:
        return str(path)
