from __future__ import annotations

from pathlib import Path

import pytest

import config
import llm


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


# --- defaults ---------------------------------------------------------------


def test_empty_mapping_gives_documented_defaults():
    settings = config.settings_from_dict({})
    assert settings.llm.temperature == 0.25
    assert settings.llm.max_retries == 3
    assert settings.batch.size == 60
    assert settings.batch.context_before == 4
    assert settings.output.encoding == "utf-8"
    assert settings.cleanup.strip_hi is False


def test_missing_default_file_is_not_fatal(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DEFAULT_PATH", tmp_path / "nope.toml")
    assert config.load_settings().batch.size == 60


def test_missing_explicit_file_raises(tmp_path):
    with pytest.raises(config.ConfigError, match="not found"):
        config.load_settings(tmp_path / "nope.toml")


def test_the_shipped_config_loads():
    settings = config.load_settings()
    assert settings.llm.model == "deepseek/deepseek-v4.1-flash-1017"
    assert settings.llm.key_env == "OPENROUTER_API_KEY"


# --- parsing ----------------------------------------------------------------


def test_values_are_read_from_the_file(tmp_path):
    path = write(
        tmp_path,
        """
[llm]
model = "m/x"
temperature = 0.5
max_retries = 1
[batch]
size = 10
context_before = 0
[output]
encoding = "utf-8-sig"
strip_en_suffix = false
[cleanup]
strip_hi = true
""",
    )
    settings = config.load_settings(path)
    assert settings.llm.model == "m/x"
    assert settings.llm.temperature == 0.5
    assert settings.batch.size == 10
    assert settings.batch.context_before == 0
    assert settings.output.encoding == "utf-8-sig"
    assert settings.output.strip_en_suffix is False
    assert settings.cleanup.strip_hi is True
    assert settings.path == path


def test_partial_file_keeps_defaults_for_the_rest(tmp_path):
    settings = config.load_settings(write(tmp_path, '[llm]\nmodel = "m/x"\n'))
    assert settings.batch.size == 60
    assert settings.llm.model == "m/x"


def test_unknown_key_is_ignored_with_a_warning(tmp_path, caplog):
    with caplog.at_level("WARNING"):
        config.load_settings(write(tmp_path, "[batch]\nsize = 30\nsiz = 4\n"))
    assert "siz" in caplog.text


def test_unknown_section_is_ignored_with_a_warning(tmp_path, caplog):
    with caplog.at_level("WARNING"):
        config.load_settings(write(tmp_path, "[nonsense]\nx = 1\n"))
    assert "nonsense" in caplog.text


def test_broken_toml_raises_with_location(tmp_path):
    with pytest.raises(config.ConfigError) as excinfo:
        config.load_settings(write(tmp_path, "[llm\nmodel = 1\n"))
    assert "config.toml" in str(excinfo.value)


# --- validation -------------------------------------------------------------


@pytest.mark.parametrize(
    "body, message",
    [
        ('[llm]\ntemperature = "hot"', "must be a number"),
        ("[llm]\ntemperature = 5", "at most 2"),
        ("[llm]\ntemperature = -1", "at least 0"),
        ("[llm]\nmax_retries = -2", "at least 0"),
        ("[llm]\nmodel = 5", "must be a string"),
        ("[batch]\nsize = 0", "at least 1"),
        ("[batch]\nsize = 2.5", "whole number"),
        ("[batch]\ncontext_after = -1", "at least 0"),
        ('[output]\nencoding = "utf-99"', "not a known codec"),
        ('[output]\nbilingual = "yes"', "true or false"),
        ('[llm]\nwire_format = "json"', "not implemented"),
        ('llm = "not a table"', "must be a table"),
    ],
)
def test_bad_values_are_rejected(tmp_path, body, message):
    with pytest.raises(config.ConfigError, match=message):
        config.load_settings(write(tmp_path, body + "\n"))


def test_boolean_is_not_accepted_as_a_number(tmp_path):
    with pytest.raises(config.ConfigError, match="must be a number"):
        config.load_settings(write(tmp_path, "[llm]\ntemperature = true\n"))


# --- client -----------------------------------------------------------------


def test_build_client_requires_a_model():
    with pytest.raises(config.ConfigError, match="model is empty"):
        config.build_client(config.Settings())


def test_build_client_requires_the_key(monkeypatch, tmp_path):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    settings = config.settings_from_dict({"llm": {"model": "m/x"}})
    with pytest.raises(llm.AuthError):
        config.build_client(settings)


def test_build_client_uses_settings(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    settings = config.settings_from_dict(
        {"llm": {"model": "m/x", "temperature": 0.4, "base_url": "https://example.test/v1"}}
    )
    client = config.build_client(settings)
    assert client.model == "m/x"
    assert client.temperature == 0.4


# --- extra headers (other providers, step 22) -------------------------------


def test_extra_headers_default_to_empty():
    settings = config.settings_from_dict({"llm": {"model": "m/x"}})
    assert settings.llm.extra_headers == {}


def test_extra_headers_are_read_from_a_table():
    settings = config.settings_from_dict(
        {"llm": {"model": "m/x", "extra_headers": {"HTTP-Referer": "https://me.test"}}}
    )
    assert settings.llm.extra_headers == {"HTTP-Referer": "https://me.test"}


def test_extra_headers_must_be_a_table():
    with pytest.raises(config.ConfigError, match="must be a table"):
        config.settings_from_dict({"llm": {"model": "m/x", "extra_headers": "nope"}})


def test_extra_header_values_must_be_strings():
    with pytest.raises(config.ConfigError, match="must be a string"):
        config.settings_from_dict(
            {"llm": {"model": "m/x", "extra_headers": {"X-Count": 3}}}
        )


def test_extra_headers_may_not_set_authorization():
    with pytest.raises(config.ConfigError, match="must not set Authorization"):
        config.settings_from_dict(
            {"llm": {"model": "m/x", "extra_headers": {"Authorization": "Bearer thing"}}}
        )


def test_a_key_shaped_header_is_rejected_without_echoing_it():
    secret = "sk-" + "a" * 40
    with pytest.raises(config.ConfigError) as excinfo:
        config.settings_from_dict(
            {"llm": {"model": "m/x", "extra_headers": {"X-Key": secret}}}
        )
    assert "looks like an API key" in str(excinfo.value)
    assert secret not in str(excinfo.value)


def test_build_client_passes_the_extra_headers(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    captured: dict = {}

    class FakeClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(llm, "OpenRouterClient", FakeClient)
    settings = config.settings_from_dict(
        {"llm": {"model": "m/x", "extra_headers": {"HTTP-Referer": "https://me.test"}}}
    )
    config.build_client(settings)
    assert captured["extra_headers"] == {"HTTP-Referer": "https://me.test"}
    assert captured["base_url"] == llm.DEFAULT_BASE_URL
