from __future__ import annotations

import dataclasses

import pytest

from khamsub import glossary

THAI = "แกนดัล์ฟ"


# --- the term itself --------------------------------------------------------


def test_actions_are_the_three_agreed():
    assert glossary.ACTIONS == ("keep", "transliterate", "translate")


def test_term_trims_its_fields():
    assert glossary.Term("  Gandalf ", " Transliterate ", f" {THAI} ") == glossary.Term(
        "Gandalf", "transliterate", THAI
    )


def test_keep_drops_the_thai():
    term = glossary.Term("NASA", "keep", "นาซา")
    assert term.th == ""


def test_empty_english_is_rejected():
    with pytest.raises(glossary.GlossaryError, match="`en`"):
        glossary.Term("   ", "keep")


def test_unknown_action_is_rejected():
    with pytest.raises(glossary.GlossaryError, match="unknown action"):
        glossary.Term("Gandalf", "romanise", THAI)


@pytest.mark.parametrize("action", ["transliterate", "translate"])
def test_spelled_actions_need_thai(action):
    with pytest.raises(glossary.GlossaryError, match="needs the Thai spelling"):
        glossary.Term("Gandalf", action, " ")


# --- writing ----------------------------------------------------------------


def test_render_round_trips():
    terms = [
        glossary.Term("Gandalf", "transliterate", THAI),
        glossary.Term("NASA", "keep"),
        glossary.Term("wizard", "translate", "พ่อมด"),
    ]
    assert glossary.parse(glossary.render(terms, title="Movie")) == terms


def test_render_explains_the_actions():
    text = glossary.render([glossary.Term("NASA", "keep")])
    assert "# Glossary" in text
    for action in glossary.ACTIONS:
        assert action in text


def test_render_names_the_work_and_keeps_thai_readable():
    text = glossary.render([glossary.Term("Gandalf", "transliterate", THAI)], title="Movie")
    assert text.startswith("# Glossary for Movie")
    assert THAI in text  # not \u0e41... escapes
    assert "\\u" not in text


def test_render_escapes_quotes_and_backslashes():
    awkward = glossary.Term('The "Hub" \\ base', "translate", 'ศูนย์ "กลาง"')
    assert glossary.parse(glossary.render([awkward])) == [awkward]


def test_empty_glossary_renders_a_valid_empty_file():
    text = glossary.render()
    assert "(no terms found)" in text
    assert glossary.parse(text) == []


# --- reading ----------------------------------------------------------------


def test_parse_reads_a_hand_written_file():
    text = """
# my notes
[[term]]
en = "Gandalf"
action = "transliterate"
th = "แกนดัล์ฟ"

[[term]]
en = "NASA"
action = "keep"
"""
    assert glossary.parse(text) == [
        glossary.Term("Gandalf", "transliterate", THAI),
        glossary.Term("NASA", "keep"),
    ]


def test_parse_reports_bad_toml():
    with pytest.raises(glossary.GlossaryError, match="not valid TOML"):
        glossary.parse("[[term]\nen =")


def test_parse_rejects_a_single_term_table():
    with pytest.raises(glossary.GlossaryError, match="\\[\\[term\\]\\]"):
        glossary.parse('[term]\nen = "NASA"\naction = "keep"\n')


def test_parse_reports_a_missing_action():
    with pytest.raises(glossary.GlossaryError, match="has no `action`"):
        glossary.parse('[[term]]\nen = "NASA"\n')


def test_parse_reports_a_field_of_the_wrong_type():
    with pytest.raises(glossary.GlossaryError, match="must be a string"):
        glossary.parse('[[term]]\nen = 5\naction = "keep"\n')


def test_parse_rejects_the_same_word_twice():
    text = """
[[term]]
en = "Gandalf"
action = "keep"

[[term]]
en = "gandalf"
action = "transliterate"
th = "แกนดัล์ฟ"
"""
    with pytest.raises(glossary.GlossaryError, match="repeats 'gandalf'"):
        glossary.parse(text)


def test_parse_warns_about_unknown_fields(caplog):
    text = '[[term]]\nen = "NASA"\naction = "keep"\nnote = "agency"\n'
    with caplog.at_level("WARNING"):
        terms = glossary.parse(text)
    assert terms == [glossary.Term("NASA", "keep")]
    assert "note" in caplog.text


# --- loading ----------------------------------------------------------------


def test_load_missing_file_is_empty(tmp_path):
    assert glossary.load(tmp_path / "nope.glossary.toml") == []


def test_load_reads_the_file(tmp_path):
    path = tmp_path / "Movie.glossary.toml"
    path.write_text(glossary.render([glossary.Term("NASA", "keep")]), encoding="utf-8")
    assert glossary.load(path) == [glossary.Term("NASA", "keep")]


def test_load_accepts_a_byte_order_mark(tmp_path):
    path = tmp_path / "Movie.glossary.toml"
    path.write_text(glossary.render([glossary.Term("NASA", "keep")]), encoding="utf-8-sig")
    assert glossary.load(path) == [glossary.Term("NASA", "keep")]


def test_load_names_the_file_when_a_term_is_bad(tmp_path):
    path = tmp_path / "Movie.glossary.toml"
    path.write_text('[[term]]\nen = "NASA"\naction = "shout"\nth = "x"\n', encoding="utf-8")
    with pytest.raises(glossary.GlossaryError, match="Movie.glossary.toml"):
        glossary.load(path)


# --- helper -----------------------------------------------------------------


def test_unique_keeps_the_first_spelling():
    terms = [
        glossary.Term("Gandalf", "transliterate", THAI),
        glossary.Term("gandalf", "transliterate", "กันดาล์ฟ"),
        glossary.Term("NASA", "keep"),
    ]
    assert glossary.unique(terms) == [terms[0], terms[2]]


# --- what one batch needs (step 17) -----------------------------------------


def three_terms() -> list[glossary.Term]:
    return [
        glossary.Term("Gandalf", "transliterate", THAI),
        glossary.Term("NASA", "keep"),
        glossary.Term("wizard", "translate", "พ่อมด"),
    ]


def test_matching_finds_the_words_in_the_text():
    found = glossary.matching(three_terms(), "Gandalf met a wizard.")
    assert [term.en for term in found] == ["Gandalf", "wizard"]


def test_matching_ignores_case():
    assert [term.en for term in glossary.matching(three_terms(), "gandalf and nasa")] == [
        "Gandalf",
        "NASA",
    ]


def test_matching_respects_word_boundaries():
    terms = [glossary.Term("Mai", "keep"), glossary.Term("He", "keep")]
    assert glossary.matching(terms, "The maid in the theatre.") == []


def test_matching_still_finds_a_name_before_an_apostrophe():
    found = glossary.matching([glossary.Term("Gandalf", "keep")], "Gandalf's hat")
    assert [term.en for term in found] == ["Gandalf"]


def test_matching_handles_a_multi_word_term():
    term = glossary.Term("Somsak's Garage", "translate", "อู่สมศักดิ์")
    assert glossary.matching([term], "Meet me at Somsak's Garage.") == [term]


def test_matching_keeps_glossary_order_not_text_order():
    found = glossary.matching(three_terms(), "wizard, NASA, Gandalf")
    assert [term.en for term in found] == ["Gandalf", "NASA", "wizard"]


def test_matching_nothing_is_empty():
    assert glossary.matching([], "anything at all") == []
    assert glossary.matching(three_terms(), "") == []


def test_for_prompt_renders_each_action():
    assert glossary.for_prompt(three_terms()) == (
        f"Gandalf = transliterate: {THAI}\nNASA = keep\nwizard = translate: พ่อมด"
    )


def test_for_prompt_of_no_terms_is_empty():
    assert glossary.for_prompt([]) == ""


def test_fingerprint_is_stable_and_short():
    assert glossary.fingerprint(three_terms()) == glossary.fingerprint(three_terms())
    assert len(glossary.fingerprint(three_terms())) == glossary.FINGERPRINT_CHARS


def test_fingerprint_does_not_depend_on_term_order():
    reversed_terms = list(reversed(three_terms()))
    assert glossary.fingerprint(three_terms()) == glossary.fingerprint(reversed_terms)


@pytest.mark.parametrize(
    "change",
    [
        {"en": "Gandalf the Grey"},
        {"th": "กันดาล์ฟ"},
        {"action": "translate"},
    ],
)
def test_fingerprint_changes_when_a_term_changes(change):
    base = three_terms()
    changed = [dataclasses.replace(base[0], **change), *base[1:]]
    assert glossary.fingerprint(base) != glossary.fingerprint(changed)


def test_fingerprint_changes_with_the_bible():
    assert glossary.fingerprint(three_terms(), "Somsak") != glossary.fingerprint(three_terms())
    assert glossary.fingerprint([], "Somsak") != glossary.fingerprint([])


def test_fingerprint_without_notes_matches_the_old_empty_key():
    from khamsub import cache

    assert glossary.fingerprint([]) == ""
    assert glossary.fingerprint([], "  \n") == ""
    assert glossary.fingerprint([]) == cache.no_glossary(1, "Hello.")
