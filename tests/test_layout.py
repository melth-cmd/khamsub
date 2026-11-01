from __future__ import annotations

import pytest

import layout


# --- no work needed ---------------------------------------------------------


def test_single_line_source_is_untouched():
    result = layout.rewrap("Hello.", "สวัสดี")
    assert result.text == "สวัสดี"
    assert result.method is None
    assert not result.rewrapped


def test_correct_line_count_is_untouched():
    result = layout.rewrap("One.\nTwo.", "หนึ่ง\nสอง")
    assert result.text == "หนึ่ง\nสอง"
    assert result.method is None


def test_more_lines_than_source_are_reported_not_reflowed():
    result = layout.rewrap("One.\nTwo.", "หนึ่ง\nสอง\nสาม")
    assert result.text == "หนึ่ง\nสอง\nสาม"
    assert result.method == "extra_lines"
    assert not result.rewrapped


# --- rule 1: dashes ---------------------------------------------------------


def test_dash_speakers_are_split_at_the_dash():
    source = "-Like a Sam?\n-Oh, my God, he wishes."
    translated = "-แบบแซมเหรอ? -โอ๊ย พระเจ้า ฝันไปเถอะ"
    result = layout.rewrap(source, translated)
    assert result.text == "-แบบแซมเหรอ?\n-โอ๊ย พระเจ้า ฝันไปเถอะ"
    assert result.method == "dash"


def test_dash_rule_handles_three_speakers():
    source = "-A\n-B\n-C"
    result = layout.rewrap(source, "-หนึ่ง -สอง -สาม")
    assert result.text == "-หนึ่ง\n-สอง\n-สาม"
    assert result.method == "dash"


def test_hyphenated_words_are_not_split_points():
    source = "-Well-known fact.\n-Is it?"
    # No " -" boundary (the hyphen is inside a word), so the only split is the turn.
    result = layout.rewrap(source, "ข้อเท็จจริงที่รู้กันดี -จริงเหรอ")
    assert result.text == "-ข้อเท็จจริงที่รู้กันดี\n-จริงเหรอ"
    assert result.method == "dash"
    assert result.dashes_restored  # the model dropped the first speaker's dash


def test_dash_followed_by_space_is_not_a_boundary():
    source = "-One\n-Two"
    # "word - next" has a space after the dash, so it is a dash, not a turn.
    result = layout.rewrap(source, "หนึ่ง - สอง")
    assert result.method == "proportional"


def test_one_dashed_source_line_is_enough_for_the_dash_rule():
    # "But short?" / "-Yeah..." in the sample: only the second line is a turn.
    source = "Just a sentence.\n-Not a speaker turn."
    result = layout.rewrap(source, "ประโยคหนึ่ง -อีกอย่าง")
    assert result.text == "ประโยคหนึ่ง\n-อีกอย่าง"
    assert result.method == "dash"


def test_dash_rule_is_skipped_when_no_source_line_is_dashed():
    result = layout.rewrap("One.\nTwo.", "หนึ่ง -สอง")
    assert result.method == "proportional"


def test_en_and_em_dashes_are_speaker_turns():
    for dash in ("\u2013", "\u2014"):
        source = f"{dash}Hello.\n{dash}Hi."
        result = layout.rewrap(source, f"{dash}สวัสดี {dash}หวัดดี")
        assert result.text == f"{dash}สวัสดี\n{dash}หวัดดี"
        assert result.method == "dash"


def test_dash_hidden_behind_a_placeholder_still_counts():
    # <i>-Swear on your life.</i> / -Swear.  from the sample.
    source = "{0}-Swear on your life.{1}\n-Swear."
    result = layout.rewrap(source, "{0}-สาบานเลย{1} -สาบาน")
    assert result.text == "{0}-สาบานเลย{1}\n-สาบาน"
    assert result.method == "dash"


# --- restoring dashes the model dropped -------------------------------------


def test_lost_dash_is_restored_after_a_proportional_split():
    source = "-Hello there.\n-Hi."
    result = layout.rewrap(source, "สวัสดีครับ หวัดดี")
    assert result.text == "-สวัสดีครับ\n-หวัดดี"
    assert result.method == "proportional"
    assert result.dashes_restored


def test_lost_dash_is_restored_when_the_line_count_is_already_right():
    source = "-Hello.\n-Hi."
    result = layout.rewrap(source, "สวัสดี\nหวัดดี")
    assert result.text == "-สวัสดี\n-หวัดดี"
    assert result.method is None
    assert result.dashes_restored


def test_dash_style_follows_the_source_line():
    source = "-Hello.\n– Hi."  # first line tight, second spaced, en dash
    result = layout.rewrap(source, "สวัสดี หวัดดี")
    assert result.text == "-สวัสดี\n– หวัดดี"


def test_dash_goes_after_a_leading_placeholder():
    source = "{0}-Hello.{1}\n{2}-Hi.{3}"
    result = layout.rewrap(source, "{0}สวัสดี{1} {2}หวัดดี{3}")
    assert result.text == "{0}-สวัสดี{1}\n{2}-หวัดดี{3}"


def test_dash_hidden_behind_html_tags_counts_when_layout_runs_after_restore():
    # The pipeline restores <i> before layout, so the tag is back in the text.
    source = "{0}-Swear on your life.{1}\n-Swear."
    result = layout.rewrap(source, "<i>สาบานเลย</i> สาบาน")
    assert result.text == "<i>-สาบานเลย</i>\n-สาบาน"


def test_dash_is_not_inserted_outside_the_html_tag():
    source = "-Hello.\n-Hi."
    result = layout.rewrap(source, "<i>สวัสดี</i>\n<i>หวัดดี</i>")
    assert result.text == "<i>-สวัสดี</i>\n<i>-หวัดดี</i>"


def test_dashes_are_not_invented_when_only_some_source_lines_have_one():
    source = "Just a sentence.\n-Not a speaker turn."
    result = layout.rewrap(source, "ประโยคหนึ่ง อีกอย่าง")
    assert result.text == "ประโยคหนึ่ง\nอีกอย่าง"
    assert not result.dashes_restored


def test_extra_lines_never_restore_dashes():
    source = "-One.\n-Two."
    result = layout.rewrap(source, "หนึ่ง\nสอง\nสาม")
    assert result.method == "extra_lines"
    assert not result.dashes_restored


def test_dash_rule_skips_when_count_mismatches():
    source = "-A\n-B\n-C"
    result = layout.rewrap(source, "-หนึ่ง -สอง")
    assert result.method != "dash"
    # Two breaks are needed but only one space exists, so it stays merged.
    assert result.method == "unrepaired"
    assert result.text == "-หนึ่ง -สอง"


# --- rule 2: proportional ---------------------------------------------------


def test_proportional_uses_the_source_break_fraction():
    source = "One two three.\nFour five."
    translated = "หนึ่ง สอง สาม สี่ ห้า"
    result = layout.rewrap(source, translated)
    assert result.method == "proportional"
    # The first source line is 60% of the entry, so the split lands late.
    assert result.text == "หนึ่ง สอง สาม\nสี่ ห้า"


def test_proportional_splits_three_lines():
    source = "One.\nTwo.\nThree."
    result = layout.rewrap(source, "หนึ่ง สอง สาม สี่")
    assert result.text.count("\n") == 2
    assert result.method == "proportional"


def test_no_space_at_all_leaves_the_entry_merged():
    # The real film case [189]: Thai with no space, so a character offset would
    # cut a word in half. It is left on one line and reported instead.
    source = "How would you like to go\nsee the school counselor?"
    translated = "อยากไปพบครูแนะแนวที่โรงเรียนไหมล่ะ?"
    result = layout.rewrap(source, translated)
    assert result.text == translated
    assert result.method == "unrepaired"
    assert not result.rewrapped


def test_a_space_at_the_edge_of_the_text_is_not_used():
    # A lone speaker dash has a space after it; splitting there would give a
    # one-character line and put everything else on the other.
    source = "You are late to the party\nwhen you are good at it."
    translated = "‐ มาสายปาร์ตี้ฉันได้เก่งจริงนะพวกเธอ"
    result = layout.rewrap(source, translated)
    assert result.text == translated
    assert result.method == "unrepaired"


def test_an_unbalanced_space_is_not_used():
    source = "Short.\nA much longer second line."
    translated = "สั้น แต่ว่าบรรทัดที่สองยาวกว่ามากทีเดียวนะ"
    result = layout.rewrap(source, translated)
    assert result.text == translated
    assert result.method == "unrepaired"


def test_the_second_real_film_case_is_left_merged():
    # Film case [204]: 44 Thai characters with no space at all.
    source = "We heard them from Mr. Garrison\na few times before."
    translated = "เราเคยได้ยินจากคุณการ์ริสันมาสองสามครั้งแล้ว"
    result = layout.rewrap(source, translated)
    assert result.text == translated
    assert result.method == "unrepaired"


def test_a_line_may_not_start_with_a_combining_mark():
    result = layout.rewrap("One.\nTwo.", "หนึ่ง ัสอง")
    assert result.text == "หนึ่ง ัสอง"
    assert result.method == "unrepaired"


def test_a_line_may_not_end_with_a_leading_vowel():
    result = layout.rewrap("One.\nTwo.", "ไปเ กลับบ้าน")
    assert result.text == "ไปเ กลับบ้าน"
    assert result.method == "unrepaired"


def test_a_line_may_end_with_the_word_ko():
    # "ก็" ends in ็ by convention, so it is a legal end of line.
    result = layout.rewrap("One.\nTwo.", "เราก็ ไป")
    assert result.method == "proportional"
    assert result.text.split("\n")[0].endswith("ก็")


def test_proportional_never_returns_empty_pieces():
    source = "A.\nB."
    result = layout.rewrap(source, "ก")
    assert result.text.strip()
    assert "\n\n" not in result.text


def test_trailing_whitespace_is_trimmed_from_pieces():
    source = "One.\nTwo."
    result = layout.rewrap(source, "หนึ่ง  สอง")
    assert result.text == "หนึ่ง\nสอง"


# --- blank lines and unbreakable spans --------------------------------------


def test_blank_lines_do_not_count_as_lines():
    assert layout.source_line_count("One.\n\nTwo.") == 2


def test_entry_with_a_blank_line_is_rewrapped_to_its_real_line_count():
    result = layout.rewrap("One.\n\nTwo.", "หนึ่ง สอง")
    assert result.text == "หนึ่ง\nสอง"
    assert result.method == "proportional"


def test_split_does_not_land_inside_a_bracket():
    source = "Here is a sign.\n[SIGN: closed] Come back tomorrow."
    translated = "นี่คือป้าย [SIGN: closed] กลับมาพรุ่งนี้"
    result = layout.rewrap(source, translated)
    assert result.method == "proportional"
    for line in result.text.split("\n"):
        assert line.count("[") == line.count("]")


def test_split_does_not_land_inside_a_placeholder_pair():
    source = "A long first half here.\nShort."
    translated = "{0}ครึ่งแรกยาวมาก{1} {2}สั้น{3}"
    result = layout.rewrap(source, translated)
    for line in result.text.split("\n"):
        assert line.count("{") == line.count("}")


def test_split_does_not_land_inside_a_music_marker():
    source = "A long first line of a song.\nLa la la."
    translated = "\u266a ลา ลา ลา \u266a ลา ลา ลา"
    result = layout.rewrap(source, translated)
    lines = result.text.split("\n")
    assert len(lines) == 2
    assert any(line.count("\u266a") == 2 for line in lines)


# --- helpers ----------------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [("One.", 1), ("One.\nTwo.", 2), ("A\nB\nC", 3), ("A\n\n\nB", 2)],
)
def test_source_line_count(text, expected):
    assert layout.source_line_count(text) == expected


@pytest.mark.parametrize(
    "text, expected",
    [("One.", 1), ("One.\nTwo.", 2), ("A\n\n\nB", 2), ("  \n", 0)],
)
def test_line_count_is_the_non_blank_count(text, expected):
    assert layout.line_count(text) == expected


@pytest.mark.parametrize(
    "text, expected",
    [
        ("- Hello.", 1),
        ("- Hello.\n- Hi.", 2),
        ("\u2013 Hello.\n\u2014 Hi.", 2),
        ("<i>- Hello.</i>", 1),
        ("{0}- Hello.", 1),
        ("Hello.\nHi.", 0),
        ("well-known.", 0),
    ],
)
def test_dash_count_counts_speaker_dashes_only(text, expected):
    assert layout.dash_count(text) == expected


# --- source normalisation (#11) ---------------------------------------------


def test_a_wrapped_continuation_is_joined_to_its_speaker():
    source = "- Wait, what?\n- It means the ugliest girl\nin school."
    assert layout.normalise_source(source) == (
        "- Wait, what?\n- It means the ugliest girl in school."
    )


def test_a_label_line_does_not_swallow_the_next_speaker():
    source = "MAYA: No.\n- He'll probably\nlook at me later."
    assert layout.normalise_source(source) == (
        "MAYA: No.\n- He'll probably look at me later."
    )


def test_an_undashed_cue_is_left_exactly_as_written():
    source = "One.\nTwo.\nThree."
    assert layout.normalise_source(source) == source


def test_dashed_lines_are_never_joined():
    source = "- One.\n- Two.\n- Three."
    assert layout.normalise_source(source) == source


def test_a_group_that_does_not_fit_keeps_its_own_wrap():
    source = "- " + "a" * 40 + "\nb."
    assert layout.normalise_source(source) == source


def test_tags_survive_the_join():
    source = "<i>- Wait, what?\n- It means girl\nin school.</i>"
    assert layout.normalise_source(source) == (
        "<i>- Wait, what?\n- It means girl in school.</i>"
    )
