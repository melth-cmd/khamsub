from __future__ import annotations

import json

import pytest

from khamsub import cache
from khamsub import llm


def make(tmp_path, name="Movie.cache.json", **kwargs) -> cache.Cache:
    return cache.Cache(tmp_path / name, model=kwargs.pop("model", "m/x"), **kwargs)


# --- round trip -------------------------------------------------------------


def test_missing_file_loads_empty(tmp_path):
    store = make(tmp_path).load()
    assert len(store) == 0


def test_put_then_save_then_load(tmp_path):
    store = make(tmp_path)
    store.put(1, "Hello.", "สวัสดี")
    store.save()

    reloaded = make(tmp_path).load()
    assert reloaded.get(1, "Hello.") == "สวัสดี"
    assert len(reloaded) == 1


def test_saved_file_is_readable_json_with_thai_intact(tmp_path):
    store = make(tmp_path)
    store.put(1, "Hello.", "สวัสดี")
    store.save()

    data = json.loads((tmp_path / "Movie.cache.json").read_text(encoding="utf-8"))
    assert data["version"] == cache.FORMAT_VERSION
    assert data["model"] == "m/x"
    assert data["prompt_version"] == llm.PROMPT_VERSION
    assert data["entries"]["1"] == {
        "key": cache.entry_key("Hello.", model="m/x"),
        "source": "Hello.",
        "text": "สวัสดี",
    }
    assert "สวัสดี" in (tmp_path / "Movie.cache.json").read_text(encoding="utf-8")


def test_save_without_changes_does_not_rewrite(tmp_path):
    path = tmp_path / "Movie.cache.json"
    store = make(tmp_path)
    store.put(1, "Hello.", "สวัสดี")
    store.save()
    first = path.stat().st_mtime_ns

    store.save()  # nothing changed
    assert path.stat().st_mtime_ns == first


def test_save_leaves_no_temp_files(tmp_path):
    store = make(tmp_path)
    store.put(1, "Hello.", "สวัสดี")
    store.save()
    assert [p.name for p in tmp_path.iterdir()] == ["Movie.cache.json"]


# --- validity ---------------------------------------------------------------


def test_get_miss_returns_none(tmp_path):
    assert make(tmp_path).load().get(9, "anything") is None


def test_changed_source_invalidates_the_entry(tmp_path):
    store = make(tmp_path)
    store.put(1, "Hello.", "สวัสดี")
    store.save()
    assert make(tmp_path).load().get(1, "Hello, friend.") is None


def test_different_model_makes_every_entry_stale(tmp_path, caplog):
    store = make(tmp_path, model="m/one")
    store.put(1, "Hello.", "สวัสดี")
    store.save()

    with caplog.at_level("INFO"):
        other = make(tmp_path, model="m/two").load()
    assert len(other) == 0
    assert other.get(1, "Hello.") is None
    assert "stale" in caplog.text


def test_going_back_to_the_old_model_hits_again(tmp_path):
    store = make(tmp_path, model="m/one")
    store.put(1, "Hello.", "สวัสดี")
    store.save()

    assert make(tmp_path, model="m/two").load().get(1, "Hello.") is None
    assert make(tmp_path, model="m/one").load().get(1, "Hello.") == "สวัสดี"


def test_different_prompt_version_makes_every_entry_stale(tmp_path):
    store = make(tmp_path, prompt_version=1)
    store.put(1, "Hello.", "สวัสดี")
    store.save()
    assert len(make(tmp_path, prompt_version=2).load()) == 0


# --- damaged files ----------------------------------------------------------


def test_corrupt_json_is_a_warning_not_a_crash(tmp_path, caplog):
    (tmp_path / "Movie.cache.json").write_text("{not json", encoding="utf-8")
    with caplog.at_level("WARNING"):
        store = make(tmp_path).load()
    assert len(store) == 0
    assert "unreadable cache" in caplog.text


def test_unknown_format_version_is_ignored(tmp_path, caplog):
    (tmp_path / "Movie.cache.json").write_text('{"version": 99}', encoding="utf-8")
    with caplog.at_level("WARNING"):
        store = make(tmp_path).load()
    assert len(store) == 0
    assert "unknown format" in caplog.text


def test_bad_entry_values_are_skipped(tmp_path):
    payload = {
        "version": cache.FORMAT_VERSION,
        "model": "m/x",
        "prompt_version": llm.PROMPT_VERSION,
        "entries": {
            "1": {"key": cache.entry_key("a", model="m/x"), "source": "a", "text": "b"},
            "2": {"key": "x", "source": 5},
            "3": {"source": "c", "text": "d"},  # no key: cannot be trusted
            "x": {},
        },
    }
    (tmp_path / "Movie.cache.json").write_text(json.dumps(payload), encoding="utf-8")
    store = make(tmp_path).load()
    assert len(store) == 1
    assert store.get(1, "a") == "b"


def test_wrong_key_for_the_loaded_text_is_not_used(tmp_path):
    payload = {
        "version": cache.FORMAT_VERSION,
        "model": "m/x",
        "prompt_version": llm.PROMPT_VERSION,
        "entries": {"1": {"key": "tampered", "source": "Hello.", "text": "สวัสดี"}},
    }
    (tmp_path / "Movie.cache.json").write_text(json.dumps(payload), encoding="utf-8")
    store = make(tmp_path).load()
    assert len(store) == 0
    assert store.get(1, "Hello.") is None


def test_contains(tmp_path):
    store = make(tmp_path)
    store.put(1, "Hello.", "สวัสดี")
    assert 1 in store
    assert 2 not in store


# --- per-entry keys ---------------------------------------------------------


def test_entry_key_is_stable_and_short(tmp_path):
    assert cache.entry_key("Hello.") == cache.entry_key("Hello.")
    assert len(cache.entry_key("Hello.")) == cache.KEY_CHARS


@pytest.mark.parametrize(
    "other",
    [
        {"source": "Hello!"},
        {"model": "m/two"},
        {"prompt_version": llm.PROMPT_VERSION + 1},
        {"glossary": "Gandalf = แกนดัล์ฟ"},
    ],
)
def test_entry_key_changes_with_every_component(other):
    base = dict(source="Hello.", model="m/x", prompt_version=llm.PROMPT_VERSION, glossary="")
    changed = base | other
    assert cache.entry_key(base["source"], base["model"], base["prompt_version"], base["glossary"]) != cache.entry_key(
        changed["source"], changed["model"], changed["prompt_version"], changed["glossary"]
    )


def test_entry_key_does_not_depend_on_the_index(tmp_path):
    store = make(tmp_path)
    assert store.key_for(1, "Hello.") == store.key_for(7, "Hello.")


def test_stored_key_matches_key_for(tmp_path):
    store = make(tmp_path)
    assert store.key_for(1, "Hello.") == cache.entry_key("Hello.", model="m/x")
    store.put(1, "Hello.", "สวัสดี")
    assert store._entries[1].key == store.key_for(1, "Hello.")


def test_glossary_fingerprint_is_part_of_the_key(tmp_path):
    terms = {1: "Gandalf = แกนดัล์ฟ", 2: ""}

    def glossary(index, source):
        return terms[index]

    store = make(tmp_path, glossary=glossary)
    store.put(1, "Gandalf speaks.", "แกนดัล์ฟพูด")
    store.put(2, "Hello.", "สวัสดี")
    store.save()

    assert make(tmp_path, glossary=glossary).load().get(1, "Gandalf speaks.") == "แกนดัล์ฟพูด"


def test_editing_one_glossary_term_only_drops_the_lines_it_touches(tmp_path):
    terms = {1: "Gandalf = แกนดัล์ฟ", 2: ""}

    def glossary(index, source):
        return terms[index]

    store = make(tmp_path, glossary=glossary)
    store.put(1, "Gandalf speaks.", "แกนดัล์ฟพูด")
    store.put(2, "Hello.", "สวัสดี")
    store.save()

    terms[1] = "Gandalf = กันดาล์ฟ"  # one term re-spelled by hand
    reopened = make(tmp_path, glossary=glossary).load()
    assert reopened.get(1, "Gandalf speaks.") is None
    assert reopened.get(2, "Hello.") == "สวัสดี"


def test_empty_glossary_callable_matches_the_default(tmp_path):
    store = make(tmp_path)
    store.put(1, "Hello.", "สวัสดี")
    store.save()

    with_empty = make(tmp_path, glossary=cache.no_glossary).load()
    assert with_empty.get(1, "Hello.") == "สวัสดี"


def test_glossary_becoming_relevant_drops_that_line(tmp_path):
    store = make(tmp_path)
    store.put(1, "Hello.", "สวัสดี")
    store.save()

    def glossary(index, source):
        return "Hello = สวัสดีจ้า" if index == 1 else ""

    assert make(tmp_path, glossary=glossary).load().get(1, "Hello.") is None


def test_stale_entries_are_not_saved_again(tmp_path):
    store = make(tmp_path)
    store.put(1, "Hello.", "สวัสดี")
    store.save()

    reopened = make(tmp_path, model="m/two").load()
    assert len(reopened) == 0
    reopened.put(2, "Bye.", "บาย")
    reopened.save()

    data = json.loads((tmp_path / "Movie.cache.json").read_text(encoding="utf-8"))
    assert sorted(data["entries"]) == ["2"]
    assert data["model"] == "m/two"
