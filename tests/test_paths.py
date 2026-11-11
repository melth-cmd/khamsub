from __future__ import annotations

from pathlib import Path

import paths


def test_xdg_config_home_is_honoured(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert paths.user_dir() == tmp_path / "khamsub"


def test_home_dot_config_is_the_fallback(monkeypatch, tmp_path):
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert paths.user_dir() == tmp_path / ".config" / "khamsub"


def test_an_empty_xdg_value_is_ignored(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", "   ")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert paths.user_dir() == tmp_path / ".config" / "khamsub"


def test_current_files_are_in_the_working_folder():
    assert paths.current_config() == Path("config.toml")
    assert paths.current_env() == Path(".env")


def test_env_candidates_are_the_folder_then_the_user_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    rows = paths.env_candidates()
    assert [label for label, _ in rows] == ["current folder", "user config"]
    assert rows[0][1] == Path(".env")
    assert rows[1][1] == tmp_path / "khamsub" / ".env"


def test_short_replaces_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert paths.short(tmp_path / ".config" / "khamsub") == "~/.config/khamsub"


def test_short_leaves_other_paths_alone(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert paths.short("config.toml") == "config.toml"
