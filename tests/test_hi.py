from __future__ import annotations

from khamsub import hi


# --- strip_hi ---------------------------------------------------------------


def test_strips_trailing_description():
    result = hi.strip_hi("I'm home. [door slams]")
    assert result.text == "I'm home."
    assert result.removed == ("[door slams]",)
    assert result.changed
    assert not result.dropped


def test_strips_leading_description():
    result = hi.strip_hi("[MUSIC PLAYING] We meet again.")
    assert result.text == "We meet again."


def test_strips_parenthesised_description():
    result = hi.strip_hi("(laughs) That's funny.")
    assert result.text == "That's funny."
    assert result.removed == ("(laughs)",)


def test_entry_of_only_description_is_dropped():
    result = hi.strip_hi("[door slams]")
    assert result.text == ""
    assert result.dropped


def test_multiple_descriptions():
    result = hi.strip_hi("[sighs] I don't know. (long pause)")
    assert result.text == "I don't know."
    assert len(result.removed) == 2


def test_removes_line_holding_only_a_dash_and_description():
    result = hi.strip_hi("- [door slams]\n- I'm home.")
    assert result.text == "- I'm home."


def test_keeps_dialogue_on_other_lines():
    result = hi.strip_hi("JOHN: [chuckles]\nLet's go.")
    assert result.text == "JOHN:\nLet's go."


def test_leaves_text_without_descriptions_identical():
    # No descriptions, so not even the double space is tidied away.
    text = "Hello  there, no brackets here."
    result = hi.strip_hi(text)
    assert result.text == text
    assert result.removed == ()
    assert not result.changed


def test_keeps_music_markers():
    result = hi.strip_hi("♪ la la ♪ [door slams]")
    assert result.text == "♪ la la ♪"


def test_keeps_speaker_labels():
    result = hi.strip_hi("[car horn] MAN: Get in.")
    assert result.text == "MAN: Get in."


def test_unbalanced_bracket_is_left_alone():
    text = "[door slams I'm home."
    result = hi.strip_hi(text)
    assert result.text == text
    assert result.removed == ()


def test_bracket_does_not_span_lines():
    # A stray '[' on one line must not eat the next line's text.
    text = "[oops\nI'm home."
    assert hi.strip_hi(text).text == text


def test_collapses_spaces_left_behind():
    result = hi.strip_hi("Wait [pause] here.")
    assert result.text == "Wait here."


def test_empty_input_is_dropped():
    result = hi.strip_hi("")
    assert result.text == ""
    assert result.dropped


def test_punctuation_only_after_strip_is_dropped():
    result = hi.strip_hi("[car horn] ...")
    assert result.dropped


def test_thai_text_is_not_dropped():
    assert not hi.strip_hi("สวัสดี [door slams]").dropped


def test_dropped_and_changed_are_independent():
    # Nothing removed, nothing left to translate: still dropped, never changed.
    result = hi.strip_hi("...")
    assert not result.changed
    assert result.dropped


# --- capitalised brackets are names, not cues -------------------------------


def test_keeps_character_name():
    result = hi.strip_hi("[Maya] <i>Oh, my God, Na.</i>")
    assert result.text == "[Maya] <i>Oh, my God, Na.</i>"
    assert result.removed == ()


def test_keeps_name_with_abbreviation():
    result = hi.strip_hi("[Mr. O] Sit down.")
    assert result.text == "[Mr. O] Sit down."


def test_keeps_name_with_description():
    result = hi.strip_hi("[Yuki speaking Japanese]")
    assert result.text == "[Yuki speaking Japanese]"
    assert not result.dropped


def test_keeps_name_and_removes_cue_in_the_same_line():
    result = hi.strip_hi("[Maya] [door slams] I'm home.")
    assert result.text == "[Maya] I'm home."
    assert result.removed == ("[door slams]",)


def test_all_caps_cue_is_still_removed():
    result = hi.strip_hi("[MUSIC PLAYING] We meet again.")
    assert result.text == "We meet again."


def test_lowercase_cue_is_removed():
    result = hi.strip_hi("[school bell rings] Class begins.")
    assert result.text == "Class begins."


def test_name_only_entry_is_not_dropped():
    result = hi.strip_hi("[Maya]")
    assert result.text == "[Maya]"
    assert not result.dropped
