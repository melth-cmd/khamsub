from __future__ import annotations

import json
from pathlib import Path

import pytest

import bible
import glossary
import llm
from batcher import SourceEntry
from conftest import FakeClient

NOTES = {
    "summary": "Somsak runs a garage; Mai comes home from school.",
    "notes": "Bangkok, present day, warm family comedy.",
    "characters": [
        {
            "name": "Somsak",
            "gender": "male",
            "age": "40s",
            "role": "owns a garage",
            "relationships": "Mai's father",
            "register": "ผม + ครับ with customers, หนู + ค่ะ with his father",
        },
        {
            "name": "Mai",
            "gender": "female",
            "age": "teen",
            "role": "student",
            "relationships": "Somsak's daughter",
            "register": "หนู + ค่ะ with her father",
        },
    ],
    "terms": [
        {"en": "Bangkok", "action": "transliterate", "th": "กรุงเทพ"},
        {"en": "Toyota", "action": "keep"},
    ],
}
NOTES_JSON = json.dumps(NOTES, ensure_ascii=False)


def entries(*texts: str) -> list[SourceEntry]:
    return [SourceEntry(number, text) for number, text in enumerate(texts, 1)]


def texts(chunks) -> list[list[str]]:
    """Just the words, so renumbering in a helper cannot hide a real mismatch."""
    return [[entry.text for entry in chunk] for chunk in chunks]


# --- the filename as a hint -------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Movie (2026).en.srt", ("Movie", "2026")),
        ("Show.S01E01.en.hi_partial.srt", ("Show S01E01", "")),
        ("The.Grand.Budapest.Hotel.2014.1080p.srt", ("The Grand Budapest Hotel", "2014")),
        ("Episode.srt", ("Episode", "")),
        ("2012.en.srt", ("2012", "2012")),
        ("My_Movie.2020.en.srt", ("My Movie", "2020")),
    ],
)
def test_title_from_filename(name, expected):
    assert bible.title_from_filename(Path(name)) == expected


# --- splitting the script ---------------------------------------------------


def test_a_short_script_is_one_part():
    chunks = bible.chunk_entries(entries("one", "two", "three"), max_chars=100)
    assert chunks == [entries("one", "two", "three")]


def test_parts_never_split_an_entry():
    chunks = bible.chunk_entries(entries("aaaaa", "bbbbb", "ccccc"), max_chars=12)
    assert texts(chunks) == [["aaaaa", "bbbbb"], ["ccccc"]]


def test_an_oversized_entry_gets_its_own_part():
    chunks = bible.chunk_entries(entries("x" * 50, "short"), max_chars=10)
    assert texts(chunks) == [["x" * 50], ["short"]]


def test_no_entries_means_no_parts():
    assert bible.chunk_entries([], max_chars=10) == []


def test_a_bad_part_size_is_rejected():
    with pytest.raises(ValueError, match="at least 1"):
        bible.chunk_entries(entries("one"), max_chars=0)


def test_render_script_is_plain_text_without_indexes():
    assert bible.render_script(entries("one", "two")) == "one\ntwo"


# --- reading the reply ------------------------------------------------------


def test_parse_json_reads_plain_json():
    assert bible.parse_json('{"summary": "x"}') == {"summary": "x"}


def test_parse_json_ignores_code_fences():
    assert bible.parse_json('```json\n{"summary": "x"}\n```') == {"summary": "x"}


def test_parse_json_ignores_polite_prose():
    reply = 'Here you go:\n{"summary": "x", "notes": "y"}\nHope this helps.'
    assert bible.parse_json(reply) == {"summary": "x", "notes": "y"}


def test_parse_json_reports_prose_without_an_object():
    with pytest.raises(bible.BibleError, match="no JSON object"):
        bible.parse_json("I would rather not.")


def test_parse_json_reports_broken_json():
    with pytest.raises(bible.BibleError, match="not valid JSON"):
        bible.parse_json('{"summary": ')


def test_parse_json_rejects_a_json_array():
    with pytest.raises(bible.BibleError, match="no JSON object"):
        bible.parse_json('["summary", "x"]')


# --- normalising the notes --------------------------------------------------


def test_character_from_mapping_reads_every_field():
    character = bible.character_from_mapping(NOTES["characters"][0])
    assert character == bible.Character(
        name="Somsak",
        gender="male",
        age="40s",
        role="owns a garage",
        relationships="Mai's father",
        register="ผม + ครับ with customers, หนู + ค่ะ with his father",
    )


def test_a_character_without_a_name_is_dropped():
    assert bible.character_from_mapping({"gender": "male"}) is None


@pytest.mark.parametrize(
    "name",
    [
        "He",
        "she",
        "First speaker",
        "First speaker (unnamed)",
        "Second speaker (unnamed; addressed as 'old man')",
        "Unknown",
        "the man on the phone",
        "Voice-over (male)",
    ],
)
def test_placeholder_names_are_not_characters(name):
    assert bible.is_placeholder_name(name)
    assert bible.character_from_mapping({"name": name}) is None


@pytest.mark.parametrize("name", ["Somsak", "Mai", "Khun Somchai", "Narrator", "The Inspector"])
def test_real_names_survive(name):
    assert not bible.is_placeholder_name(name)
    assert bible.character_from_mapping({"name": name}).name == name


def test_placeholder_speakers_are_dropped_from_the_bible():
    result = bible.bible_from_mapping(
        {"characters": [{"name": "He"}, {"name": "Somsak"}, {"name": "First speaker (unnamed)"}]}
    )
    assert [character.name for character in result.characters] == ["Somsak"]


def test_a_list_of_relationships_is_joined():
    character = bible.character_from_mapping(
        {"name": "Mai", "relationships": ["Somsak's daughter", "Noi's friend"]}
    )
    assert character.relationships == "Somsak's daughter; Noi's friend"


def test_term_from_mapping_reads_the_three_actions():
    assert bible.term_from_mapping({"en": "NASA", "action": "keep"}) == glossary.Term(
        "NASA", "keep"
    )
    assert bible.term_from_mapping(
        {"en": "Bangkok", "action": "transliterate", "th": "กรุงเทพ"}
    ) == glossary.Term("Bangkok", "transliterate", "กรุงเทพ")
    assert bible.term_from_mapping(
        {"en": "wizard", "action": "translate", "th": "พ่อมด"}
    ) == glossary.Term("wizard", "translate", "พ่อมด")


def test_an_acronym_term_is_recorded_with_its_thai_form():
    term = bible.term_from_mapping(
        {"en": "NASA", "action": "transliterate", "th": "นาซา"}
    )
    assert term == glossary.Term("NASA", "transliterate", "นาซา")


def test_an_unknown_action_with_thai_becomes_transliterate(caplog):
    with caplog.at_level("WARNING"):
        term = bible.term_from_mapping({"en": "Gandalf", "action": "romanise", "th": "แกนดัล์ฟ"})
    assert term == glossary.Term("Gandalf", "transliterate", "แกนดัล์ฟ")
    assert "unknown action" in caplog.text


def test_an_unknown_action_without_thai_becomes_keep(caplog):
    with caplog.at_level("WARNING"):
        term = bible.term_from_mapping({"en": "NASA", "action": "acronym"})
    assert term == glossary.Term("NASA", "keep")


def test_a_spelled_action_without_thai_is_kept_in_english(caplog):
    with caplog.at_level("WARNING"):
        term = bible.term_from_mapping({"en": "Gandalf", "action": "transliterate"})
    assert term == glossary.Term("Gandalf", "keep")
    assert "needs Thai" in caplog.text


def test_a_term_without_english_is_dropped():
    assert bible.term_from_mapping({"action": "keep"}) is None


def test_the_thai_field_may_be_called_thai():
    term = bible.term_from_mapping({"en": "Bangkok", "action": "translate", "thai": "กรุงเทพ"})
    assert term.th == "กรุงเทพ"


def test_bible_from_mapping_uses_the_filename_title_not_the_reply():
    data = NOTES | {"title": "Wrong Title", "year": "1999"}
    result = bible.bible_from_mapping(data, title="Movie", year="2026")
    assert (result.title, result.year) == ("Movie", "2026")


def test_bible_from_mapping_keeps_the_reply_summary_and_notes():
    result = bible.bible_from_mapping(NOTES)
    assert result.summary == NOTES["summary"]
    assert result.notes == NOTES["notes"]
    assert [character.name for character in result.characters] == ["Somsak", "Mai"]
    assert [term.en for term in result.terms] == ["Bangkok", "Toyota"]


def test_bible_from_mapping_drops_repeated_people_and_terms():
    data = {
        "characters": [{"name": "Mai"}, {"name": "mai"}],
        "terms": [
            {"en": "Gandalf", "action": "keep"},
            {"en": "gandalf", "action": "transliterate", "th": "แกนดัล์ฟ"},
        ],
    }
    result = bible.bible_from_mapping(data)
    assert len(result.characters) == 1
    assert len(result.terms) == 1
    assert result.terms[0].action == "keep"


def test_a_bare_object_is_accepted_where_a_list_was_asked_for():
    result = bible.bible_from_mapping({"characters": {"name": "Mai"}})
    assert [character.name for character in result.characters] == ["Mai"]


def test_unusable_character_notes_are_a_warning_not_a_crash(caplog):
    with caplog.at_level("WARNING"):
        result = bible.bible_from_mapping({"characters": "Mai and Somsak"})
    assert result.characters == []
    assert "expected a list" in caplog.text


def test_the_setting_alias_fills_notes():
    assert bible.bible_from_mapping({"setting": "Bangkok"}).notes == "Bangkok"


# --- the files --------------------------------------------------------------


def test_markdown_has_a_heading_summary_and_characters():
    text = bible.bible_from_mapping(NOTES, title="Show", year="2026").to_markdown()
    assert text.startswith("# Story bible: Show (2026)")
    assert "## Summary" in text and NOTES["summary"] in text
    assert "## Setting and tone" in text and NOTES["notes"] in text
    assert "### Somsak (male, 40s)" in text
    assert "- Role: owns a garage" in text
    assert "- Relationships: Mai's father" in text
    assert "- Thai: ผม + ครับ with customers" in text


def test_markdown_omits_empty_character_fields():
    text = bible.Bible(title="Episode", characters=[bible.Character("Mai")]).to_markdown()
    assert "### Mai" in text
    assert "- Role:" not in text
    assert "(" not in text.split("### Mai")[1].split("\n")[0]


def test_markdown_of_an_empty_bible_is_still_complete():
    text = bible.Bible().to_markdown()
    assert "# Story bible: Untitled" in text
    assert "(no summary)" in text
    assert "(none)" in text
    assert text.endswith("\n") and not text.endswith("\n\n")


def test_glossary_toml_round_trips_through_the_parser():
    result = bible.bible_from_mapping(NOTES, title="Show")
    assert glossary.parse(result.to_glossary_toml()) == result.terms


def test_write_creates_both_files(tmp_path):
    result = bible.bible_from_mapping(NOTES, title="Show")
    bible_path, glossary_path = bible.write(
        result, tmp_path / "Show.bible.md", tmp_path / "Show.glossary.toml"
    )
    assert bible_path.read_text(encoding="utf-8") == result.to_markdown()
    assert glossary.load(glossary_path) == result.terms
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "Show.bible.md",
        "Show.glossary.toml",
    ]


# --- prompts ----------------------------------------------------------------


def test_notes_prompt_asks_for_the_three_actions_and_json():
    prompt = bible.NOTES_SYSTEM_PROMPT
    for action in glossary.ACTIONS:
        assert action in prompt
    assert "JSON object" in prompt
    assert "Thai script" in prompt


def test_notes_prompt_records_acronyms_with_the_same_rule_as_the_translator():
    prompt = bible.NOTES_SYSTEM_PROMPT
    assert "acronyms" in prompt
    assert "AIM -> เอม" in prompt
    assert "NASA -> นาซา" in prompt


def test_merge_prompt_keeps_acronyms_as_terms():
    assert "acronym" in bible.MERGE_SYSTEM_PROMPT


def test_notes_messages_carry_title_year_and_script():
    messages = bible.notes_messages("Movie", "2026", "Hello there.")
    assert messages[0]["role"] == "system"
    user = messages[1]["content"]
    assert "TITLE: Movie" in user and "YEAR: 2026" in user
    assert "[SCRIPT]\nHello there." in user


def test_notes_messages_omit_an_unknown_year():
    user = bible.notes_messages("Movie", "", "Hello.")[1]["content"]
    assert "YEAR:" not in user


def test_merge_messages_carry_every_part():
    parts = [{"summary": "part one"}, {"summary": "part two"}]
    messages = bible.merge_messages("Movie", "", parts)
    user = messages[1]["content"]
    assert "[PART NOTES]" in user
    assert "part one" in user and "part two" in user
    assert "Merge" in messages[0]["content"]


# --- building ---------------------------------------------------------------


def test_build_reads_a_short_script_once():
    client = FakeClient([NOTES_JSON])
    result = bible.build(
        client, entries("Hello there.", "How are you?"), title="Movie", year="2026"
    )

    assert client.call_count == 1
    assert client.temperatures == [bible.PREPASS_TEMPERATURE]
    prompt = client.calls[0][1]["content"]
    assert "Hello there." in prompt and "How are you?" in prompt
    assert (result.bible.title, result.bible.year) == ("Movie", "2026")
    assert [character.name for character in result.bible.characters] == ["Somsak", "Mai"]
    assert result.calls == 1


def test_build_merges_several_parts_with_one_extra_call():
    client = FakeClient(
        [
            json.dumps({"summary": "first half", "characters": [{"name": "Mai"}]}),
            json.dumps({"summary": "second half", "characters": [{"name": "Somsak"}]}),
            NOTES_JSON,
        ]
    )
    result = bible.build(client, entries("a" * 30, "b" * 30), title="Movie", max_chars=40)

    assert client.call_count == 3
    merge = client.calls[2][1]["content"]
    assert "first half" in merge and "second half" in merge
    assert [character.name for character in result.bible.characters] == ["Somsak", "Mai"]
    assert result.calls == 3


def test_build_reasks_once_after_prose():
    client = FakeClient(["I cannot help with that.", NOTES_JSON])
    result = bible.build(client, entries("Hello."), title="Movie")

    assert client.call_count == 2
    assert client.temperatures == [bible.PREPASS_TEMPERATURE, bible.RETRY_TEMPERATURE]
    reminder = client.calls[1][-1]["content"]
    assert "not valid JSON" in reminder
    assert result.bible.summary == NOTES["summary"]


def test_build_gives_up_after_two_prose_replies():
    client = FakeClient(["prose.", "more prose."])
    with pytest.raises(bible.BibleError, match="no JSON object"):
        bible.build(client, entries("Hello."))
    assert client.call_count == 2


def test_build_needs_some_text():
    with pytest.raises(bible.BibleError, match="no text"):
        bible.build(FakeClient(), [])


def test_build_adds_up_tokens_from_every_call():
    client = FakeClient(
        [
            llm.Completion(text="prose.", usage=llm.Usage(100, 10)),
            llm.Completion(text=NOTES_JSON, usage=llm.Usage(200, 20)),
        ]
    )
    result = bible.build(client, entries("Hello."))
    assert result.usage.prompt_tokens == 300
    assert result.usage.completion_tokens == 30
    assert result.total_tokens == 330


# --- reading the files back (step 16) ---------------------------------------


def test_load_missing_bible_is_empty(tmp_path):
    assert bible.load(tmp_path / "nope.bible.md") == ""


def test_load_strips_the_reader_comment(tmp_path):
    path = tmp_path / "Movie.bible.md"
    path.write_text(
        "# Story bible\n\n<!-- built by translate.py; edit by hand -->\nSomsak uses ผม.\n",
        encoding="utf-8",
    )
    text = bible.load(path)
    assert "built by translate.py" not in text
    assert text.startswith("# Story bible")
    assert "Somsak uses ผม." in text


def test_load_accepts_a_byte_order_mark(tmp_path):
    path = tmp_path / "Movie.bible.md"
    path.write_text("# Story bible\nMai.\n", encoding="utf-8-sig")
    assert bible.load(path).startswith("# Story bible")


def test_load_reports_an_unreadable_bible(tmp_path):
    folder = tmp_path / "Movie.bible.md"
    folder.mkdir()
    with pytest.raises(bible.BibleError, match="could not read"):
        bible.load(folder)


def test_notes_know_what_they_hold():
    assert not bible.Notes().has_bible
    assert not bible.Notes().has_glossary
    assert bible.Notes(bible="Somsak").has_bible
    assert bible.Notes(glossary_path=Path("Movie.glossary.toml")).has_glossary
    assert not bible.Notes(bible="   ").has_bible


def test_load_notes_with_no_sidecars(tmp_path):
    notes = bible.load_notes(tmp_path / "Movie.en.srt")
    assert notes.bible == ""
    assert notes.terms == []
    assert notes.bible_path is None
    assert notes.glossary_path is None


def test_load_notes_reads_both_files(tmp_path):
    (tmp_path / "Movie.bible.md").write_text("# Story bible\nMai.\n", encoding="utf-8")
    (tmp_path / "Movie.glossary.toml").write_text(
        glossary.render([glossary.Term("NASA", "keep")]), encoding="utf-8"
    )

    notes = bible.load_notes(tmp_path / "Movie.en.srt")
    assert notes.has_bible and notes.has_glossary
    assert notes.bible_path == tmp_path / "Movie.bible.md"
    assert notes.glossary_path == tmp_path / "Movie.glossary.toml"
    assert notes.terms == [glossary.Term("NASA", "keep")]


def test_load_notes_returns_the_edited_file_next_time(tmp_path):
    path = tmp_path / "Movie.bible.md"
    source = tmp_path / "Movie.en.srt"
    path.write_text("Somsak\n", encoding="utf-8")
    assert bible.load_notes(source).bible == "Somsak"

    path.write_text("Mai\n", encoding="utf-8")  # no rebuild step, no caching
    assert bible.load_notes(source).bible == "Mai"


def test_load_notes_raises_for_a_broken_glossary(tmp_path):
    (tmp_path / "Movie.glossary.toml").write_text(
        '[[term]]\nen = "NASA"\naction = "shout"\nth = "นาซา"\n', encoding="utf-8"
    )
    with pytest.raises(glossary.GlossaryError, match="Movie.glossary.toml"):
        bible.load_notes(tmp_path / "Movie.en.srt")
