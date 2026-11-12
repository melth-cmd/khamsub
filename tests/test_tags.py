from __future__ import annotations

import pytest

from khamsub import tags
from khamsub.tags import TagMismatch


# --- protect ----------------------------------------------------------------


def test_protect_replaces_html_tags_in_order():
    protected = tags.protect("<i>Hello</i> there")
    assert protected.text == "{0}Hello{1} there"
    assert [t.raw for t in protected.tags] == ["<i>", "</i>"]
    assert [t.kind for t in protected.tags] == ["html", "html"]


def test_protect_leaves_plain_text_alone():
    protected = tags.protect("Hello there.")
    assert protected.text == "Hello there."
    assert len(protected) == 0
    assert protected.counts == {}


def test_protect_handles_ass_overrides():
    protected = tags.protect("{\\an8}Top of screen")
    assert protected.text == "{0}Top of screen"
    assert protected.tags[0].kind == "ass"


def test_protect_handles_ass_with_arguments():
    protected = tags.protect("{\\pos(320,240)}Hi")
    assert protected.tags[0].raw == "{\\pos(320,240)}"


def test_protect_handles_music_markers():
    protected = tags.protect("♪ la la ♪")
    assert protected.text == "{0} la la {1}"
    assert {t.kind for t in protected.tags} == {"music"}


def test_protect_escapes_literal_braces_so_placeholders_stay_unique():
    # The source already reads {0}; our own placeholder numbering must not clash.
    protected = tags.protect("<i>{0}</i>")
    assert protected.text == "{0}{1}{2}"
    assert [t.raw for t in protected.tags] == ["<i>", "{0}", "</i>"]
    assert protected.tags[1].kind == "literal"


def test_protect_keeps_sound_effects_and_dashes():
    text = "- [door slams] I'm home."
    protected = tags.protect(text)
    assert protected.text == text
    assert len(protected) == 0


def test_protect_is_idempotent_for_tags_only_line():
    protected = tags.protect("<i>[door slams]</i>")
    assert protected.text == "{0}[door slams]{1}"


def test_protect_empty_string():
    protected = tags.protect("")
    assert protected.text == ""
    assert len(protected) == 0


def test_protect_multiline_entry():
    protected = tags.protect("<i>Hello</i>\n<i>again</i>")
    assert protected.text == "{0}Hello{1}\n{2}again{3}"


def test_counts_tracks_repeated_tags():
    protected = tags.protect("<i>a</i> <i>b</i>")
    assert protected.counts["<i>"] == 2
    assert protected.counts["</i>"] == 2


# --- check ------------------------------------------------------------------


def test_check_clean_translation_has_no_issues():
    assert tags.check("<i>Hello</i>", "{0}สวัสดี{1}") == []


def test_check_reports_missing_placeholder():
    issues = tags.check("<i>Hello</i>", "สวัสดี{1}")
    assert [i.kind for i in issues] == ["missing"]
    assert issues[0].index == 0
    assert issues[0].tag == "<i>"


def test_check_reports_duplicate_placeholder():
    issues = tags.check("<i>Hello</i>", "{0}สวัสดี{0}{1}")
    assert [i.kind for i in issues] == ["duplicate"]


def test_check_reports_unknown_placeholder():
    issues = tags.check("<i>Hello</i>", "{0}สวัสดี{1}{7}")
    assert [i.kind for i in issues] == ["unknown"]
    assert issues[0].index == 7


def test_check_ignores_reordering():
    # Thai word order can differ; only counts matter, not position.
    assert tags.check("<i>Hello</i>", "{1}สวัสดี{0}") == []


# --- restore ----------------------------------------------------------------


def test_restore_roundtrip():
    protected = tags.protect("<i>Fine, thanks.</i>")
    assert tags.restore("{0}สบายดี ขอบคุณ{1}", protected) == "<i>สบายดี ขอบคุณ</i>"


def test_restore_tolerates_spaces_in_placeholders():
    protected = tags.protect("<i>Hi</i>")
    assert tags.restore("{ 0 }ไฮ{ 1 }", protected) == "<i>ไฮ</i>"


def test_restore_puts_literal_braces_back():
    protected = tags.protect("<i>{0}</i>")
    assert tags.restore("{0}{1}{2}", protected) == "<i>{0}</i>"


def test_restore_raises_on_mismatch():
    protected = tags.protect("<i>Hi</i>")
    with pytest.raises(TagMismatch) as excinfo:
        tags.restore("ไฮ{1}", protected)
    assert excinfo.value.issues[0].kind == "missing"
    assert "missing" in str(excinfo.value)


def test_restore_non_strict_logs_and_continues(caplog):
    protected = tags.protect("<i>Hi</i>")
    with caplog.at_level("WARNING"):
        result = tags.restore("ไฮ{1}", protected, strict=False)
    assert result == "ไฮ</i>"
    assert "Tag mismatch" in caplog.text


def test_restore_non_strict_leaves_unknown_placeholder():
    protected = tags.protect("<i>Hi</i>")
    result = tags.restore("{0}ไฮ{1}{9}", protected, strict=False)
    assert result == "<i>ไฮ</i>{9}"


def test_restore_no_tags_is_identity():
    protected = tags.protect("Hello")
    assert tags.restore("สวัสดี", protected) == "สวัสดี"


def test_strip_markup_removes_tags_placeholders_and_music():
    assert tags.strip_markup("<i>Hello {0}</i> ♪") == "Hello  "


def test_strip_markup_leaves_plain_text_alone():
    assert tags.strip_markup("Hello [door slams] - Hi.") == "Hello [door slams] - Hi."
