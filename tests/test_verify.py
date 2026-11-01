from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path

import pytest

import glossary
import pipeline
import srtio
import verify

from conftest import FakeClient


def term(en: str, action: str = "keep", th: str = "") -> glossary.Term:
    return glossary.Term(en, action, th)


GANDALF = ["Gandalf", "transliterate", "แกนดัล์ฟ"]
NASA = ["NASA", "keep"]
WIZARD = ["wizard", "translate", "พ่อมด"]


# --- small helpers ----------------------------------------------------------


def test_scrub_removes_tags_placeholders_and_music():
    assert verify.scrub("<i>Hello {0}</i> ♪") == "Hello  "


def test_content_length_ignores_spaces_tags_and_placeholders():
    assert verify.content_length("สวัสดี {0}<i>ครับ</i>") == len("สวัสดีครับ")


def test_content_length_of_empty_markup_is_zero():
    assert verify.content_length("  {0}<i></i>  ") == 0


def test_english_words_finds_latin_words_once():
    assert verify.english_words("สวัสดี world, World!") == ["world"]


def test_english_words_keeps_apostrophes_inside_words():
    assert verify.english_words("don't stop") == ["don't", "stop"]


def test_english_words_ignores_tags_and_placeholders():
    assert verify.english_words("<i>ok</i> {0}") == ["ok"]


def test_english_words_returns_nothing_for_thai():
    assert verify.english_words("สวัสดีครับ") == []


# --- tag mismatch -----------------------------------------------------------


def test_tag_mismatch_is_none_when_tags_match():
    assert verify.tag_mismatch("<i>One.</i>", "<i>หนึ่ง</i>") is None


def test_tag_mismatch_reports_a_missing_tag():
    problem = verify.tag_mismatch("<i>Wait.</i>", "รอ")
    assert problem is not None
    assert "missing" in problem and "<i>" in problem and "</i>" in problem


def test_tag_mismatch_reports_extra_and_invented_placeholders():
    problem = verify.tag_mismatch("Wait.", "รอ {3}")
    assert problem is not None
    assert "extra {3}" in problem


def test_tag_mismatch_shows_a_second_copy_as_extra():
    problem = verify.tag_mismatch("<i>Wait.</i>", "<i>รอ<i>")
    assert problem is not None
    assert "missing </i>" in problem
    assert "extra <i>" in problem


# --- glossary ---------------------------------------------------------------


def test_glossary_keep_present_passes():
    assert verify.glossary_flags(1, "NASA is here.", "NASA อยู่ที่นี่", [term(*NASA)]) == []


def test_glossary_keep_missing_is_flagged():
    flags = verify.glossary_flags(1, "NASA is here.", "องค์กรอยู่ที่นี่", [term(*NASA)])
    assert [flag.kind for flag in flags] == ["glossary_missing"]
    assert "NASA" in flags[0].message


def test_glossary_transliteration_present_passes():
    flags = verify.glossary_flags(
        1, "Gandalf is here.", "แกนดัล์ฟอยู่ที่นี่", [term(*GANDALF)]
    )
    assert flags == []


def test_glossary_term_left_in_english_is_inconsistent():
    flags = verify.glossary_flags(
        1, "Gandalf is here.", "Gandalf อยู่ที่นี่", [term(*GANDALF)]
    )
    assert [flag.kind for flag in flags] == ["glossary_inconsistent"]
    assert "แกนดัล์ฟ" in flags[0].message


def test_glossary_translation_absent_is_flagged_missing():
    flags = verify.glossary_flags(1, "The wizard spoke.", "เขาพูด", [term(*WIZARD)])
    assert [flag.kind for flag in flags] == ["glossary_missing"]


def test_glossary_term_not_in_the_source_is_ignored():
    assert verify.glossary_flags(1, "Hello.", "สวัสดี", [term(*GANDALF)]) == []


def test_glossary_check_reports_the_entry_it_belongs_to():
    flags = verify.glossary_flags(7, "NASA is here.", "อยู่ที่นี่", [term(*NASA)])
    assert flags[0].index == 7
    assert flags[0].source == "NASA is here."


# --- leftover English -------------------------------------------------------


def test_leftover_english_is_flagged():
    flags = verify.leftover_english(1, "Hello world.", "สวัสดี world", [])
    assert [flag.kind for flag in flags] == ["english_left"]
    assert "world" in flags[0].message


def test_leftover_english_covered_by_a_term_is_left_to_the_glossary_check():
    flags = verify.leftover_english(1, "NASA is here.", "NASA อยู่", [term(*NASA)])
    assert flags == []


def test_leftover_english_allows_a_short_letter_digit_code():
    assert verify.leftover_english(1, "K9 barks.", "K9 เห่า", []) == []


def test_leftover_english_allows_a_link_and_a_handle():
    source = "See https://example.com/x or @flymiamibro22"
    translated = "ดู https://example.com/x หรือ @flymiamibro22"
    assert verify.leftover_english(1, source, translated, []) == []


def test_leftover_english_still_flags_a_name_left_in_english():
    flags = verify.leftover_english(1, "MAYA: okay.", "MAYA: โอเค", [])
    assert [flag.kind for flag in flags] == ["english_left"]
    assert "MAYA" in flags[0].message


def test_a_lost_number_is_flagged():
    flags = verify.check_entry(1, '"Pen15"', '"เพนฟิฟทีน"')
    kinds = [flag.kind for flag in flags]
    assert kinds == ["lost_token"]
    assert "15" in flags[0].message


def test_a_kept_number_is_not_flagged():
    assert verify.check_entry(1, '"Pen15"', '"เพน15"') == []


def test_leftover_english_ignores_words_inside_tags():
    assert verify.leftover_english(1, "Hello.", "<i>สวัสดี</i>", []) == []


def test_leftover_english_lists_each_word_once():
    flags = verify.leftover_english(1, "Hello.", "สวัสดี ok OK", [])
    assert "ok" in flags[0].message
    assert "OK" not in flags[0].message


# --- latin glued to thai ----------------------------------------------------


def test_latin_glued_to_thai_is_its_own_flag():
    # South Park [841]: "Bullshit." came back as "ทwerp!".
    flags = verify.check_entry(841, "Bullshit.", "ทwerp!")
    assert [flag.kind for flag in flags] == ["mixed_script"]
    assert "werp" in flags[0].message


def test_a_glued_run_is_not_also_reported_as_leftover_english():
    flags = verify.check_entry(841, "Bullshit.", "ทwerp!")
    assert "english_left" not in [flag.kind for flag in flags]


def test_a_delimited_english_word_is_still_leftover_english():
    flags = verify.check_entry(1, "Hello world.", "สวัสดี world")
    assert [flag.kind for flag in flags] == ["english_left"]


def test_a_short_code_touching_thai_is_not_glued_latin():
    assert verify.check_entry(1, "V2", "ใช้V2") == []


# --- length -----------------------------------------------------------------


def test_a_much_too_short_translation_is_flagged():
    flags = verify.length_flags(1, "Hello there, my good friend.", "อือ")
    assert [flag.kind for flag in flags] == ["short"]


def test_a_much_too_long_translation_is_flagged():
    flags = verify.length_flags(1, "Okay.", "ฉันเข้าใจดีมากเลยจริงๆนะครับ")
    assert [flag.kind for flag in flags] == ["long"]


def test_a_normal_length_is_not_flagged():
    assert verify.length_flags(1, "How are you?", "สบายดีไหม") == []


def test_a_very_short_source_is_not_compared():
    assert verify.length_flags(1, "Hi", "สวัสดีค่ะ") == []


def test_an_empty_translation_is_not_measured():
    assert verify.length_flags(1, "Hello there.", "") == []


# --- check_entry ------------------------------------------------------------


def test_a_good_entry_has_no_flags():
    assert verify.check_entry(1, "Hello there.", "สวัสดีครับ") == []


def test_a_failed_entry_is_the_only_flag():
    flags = verify.check_entry(
        1, "Hello there.", "Hello there.", ok=False, reason="missing_thai"
    )
    assert [flag.kind for flag in flags] == ["failed"]
    assert flags[0].message == "left in English (missing_thai)"
    assert flags[0].translation == "Hello there."


def test_a_failed_entry_without_a_reason_still_says_it_is_english():
    flags = verify.check_entry(1, "Hello.", "Hello.", ok=False)
    assert flags[0].message == "left in English"


def test_an_empty_translation_is_flagged():
    assert [flag.kind for flag in verify.check_entry(1, "Hello.", " {0} ")] == ["empty"]


def test_a_non_verbal_source_is_left_alone():
    assert verify.check_entry(1, "Mm-hmm.", "Mm-hmm.") == []
    assert verify.check_entry(1, "[MUSIC PLAYING]", "[MUSIC PLAYING]") == []


def test_a_non_verbal_source_may_be_translated():
    assert verify.check_entry(1, "Mm-hmm.", "อืม") == []


def test_a_changed_line_count_is_flagged():
    flags = verify.check_entry(1, "One.\nTwo.", "หนึ่งสอง")
    assert [flag.kind for flag in flags] == ["line_count"]


def test_extra_lines_are_flagged_too():
    flags = verify.check_entry(1, "One.\nTwo.", "หนึ่ง\nสอง\nสาม")
    assert [flag.kind for flag in flags] == ["line_count"]


def test_a_lost_speaker_dash_is_flagged():
    flags = verify.check_entry(1, "- Hello.", "สวัสดี")
    assert [flag.kind for flag in flags] == ["dashes_lost"]


def test_a_kept_speaker_dash_is_not_flagged():
    assert verify.check_entry(1, "- Hello.", "- สวัสดี") == []


def test_a_dash_hidden_behind_a_tag_still_counts():
    assert verify.check_entry(1, "<i>- Hello.</i>", "<i>- สวัสดี</i>") == []


def test_an_entry_can_flag_several_things_in_kind_order():
    flags = verify.check_entry(1, "One.\nTwo.", "หนึ่ง English")
    kinds = [flag.kind for flag in flags]
    assert kinds == sorted(kinds, key=verify.KINDS.index)
    assert "line_count" in kinds and "english_left" in kinds


def test_flags_carry_their_entry_context():
    flag = verify.check_entry(1, "- Hello.", "สวัสดี")[0]
    assert flag.index == 1
    assert flag.source == "- Hello."
    assert flag.translation == "สวัสดี"


# --- verify_file ------------------------------------------------------------


@dataclass
class FakeResult:
    index: int
    text: str
    ok: bool = True
    reason: str | None = None
    rewrapped: str | None = None
    dashes_restored: bool = False


@dataclass
class FakeFile:
    results: list[FakeResult] = field(default_factory=list)
    sources: dict[int, str] = field(default_factory=dict)
    dropped: list[int] = field(default_factory=list)
    fixed: list[int] = field(default_factory=list)
    reviewed: list[int] = field(default_factory=list)
    suggestions: dict[int, str] = field(default_factory=dict)


def test_verify_file_checks_every_entry():
    translated = FakeFile(
        results=[FakeResult(1, "สวัสดี"), FakeResult(2, "ลาก่อน")],
        sources={1: "Hello.", 2: "Goodbye."},
    )
    verification = verify.verify_file(translated)
    assert verification.checked == 2
    assert verification.flags == []


def test_verify_file_skips_results_without_a_source():
    translated = FakeFile(results=[FakeResult(1, "สวัสดี")], sources={})
    verification = verify.verify_file(translated)
    assert verification.checked == 0
    assert verification.flags == []


def test_verify_file_sorts_by_entry_then_kind():
    translated = FakeFile(
        results=[
            FakeResult(2, "สอง", ok=False, reason="transport"),
            FakeResult(1, "หนึ่ง English", ok=True),
        ],
        sources={1: "One.\nTwo.", 2: "Two."},
    )
    verification = verify.verify_file(translated)
    assert verification.entries == [1, 2]
    assert verification.flags[0].index == 1
    assert [flag.kind for flag in verification.for_index(2)] == ["failed"]


def test_verification_counts_flags_by_kind():
    translated = FakeFile(
        results=[FakeResult(1, "สวัสดี"), FakeResult(2, "Hello.", ok=False)],
        sources={1: "Hello.", 2: "Goodbye."},
    )
    verification = verify.verify_file(translated)
    assert verification.by_kind == {"failed": 1}


def test_verify_file_only_uses_the_terms_it_is_given():
    translated = FakeFile(
        results=[FakeResult(1, "Gandalf อยู่ที่นี่")],
        sources={1: "Gandalf is here."},
    )
    # With no glossary there is no "keep" rule, so the name is just English.
    assert [flag.kind for flag in verify.verify_file(translated).flags] == ["english_left"]
    flagged = verify.verify_file(translated, [term(*GANDALF)])
    assert [flag.kind for flag in flagged.flags] == ["glossary_inconsistent"]


# --- end to end through the pipeline ---------------------------------------


SRT = """1
00:00:01,000 --> 00:00:02,000
<i>Wait for me.</i>
"""


def one_subtitle():
    subtitles, _ = srtio.parse_subtitles(SRT)
    return subtitles


def test_pipeline_keeps_the_source_of_every_result():
    result = pipeline.translate_subtitles(
        FakeClient(["[1] {0}รอด้วย{1}"]), one_subtitle(), sleep=lambda seconds: None
    )
    assert result.sources == {1: "<i>Wait for me.</i>"}
    assert result.results[0].text == "<i>รอด้วย</i>"


def test_pipeline_result_verifies_clean():
    result = pipeline.translate_subtitles(
        FakeClient(["[1] {0}รอด้วย{1}"]), one_subtitle(), sleep=lambda seconds: None
    )
    verification = verify.verify_file(result)
    assert verification.checked == 1
    assert verification.flags == []


def test_pipeline_failure_is_verified_as_failed():
    subtitles, _ = srtio.parse_subtitles(
        "1\n00:00:01,000 --> 00:00:02,000\nHello.\n"
    )
    result = pipeline.translate_subtitles(
        FakeClient(["[1] 等等"]),
        subtitles,
        max_retries=0,
        sleep=lambda seconds: None,
    )
    verification = verify.verify_file(result)
    assert verification.by_kind == {"failed": 1}
    assert "missing_thai" in verification.flags[0].message


def test_pipeline_strip_hi_source_is_the_cleaned_text():
    subtitles, _ = srtio.parse_subtitles(
        "1\n00:00:01,000 --> 00:00:02,000\nHello. [door slams]\n"
    )
    result = pipeline.translate_subtitles(
        FakeClient(["[1] สวัสดี"]),
        subtitles,
        strip_hi=True,
        sleep=lambda seconds: None,
    )
    assert result.sources == {1: "Hello."}


def test_pipeline_keeps_sources_for_cached_entries(tmp_path):
    source = tmp_path / "movie.en.srt"
    source.write_text(SRT, encoding="utf-8")

    first = pipeline.translate_file(
        FakeClient(["[1] {0}รอด้วย{1}"]), source, sleep=lambda seconds: None
    )
    assert first[0].sources == {1: "<i>Wait for me.</i>"}

    # Nothing queued: the second run must be served entirely from the cache.
    second = pipeline.translate_file(FakeClient(), source, sleep=lambda seconds: None)
    assert second[0].sources == {1: "<i>Wait for me.</i>"}
    assert verify.verify_file(second[0]).flags == []


# --- report.txt -------------------------------------------------------------

NOW = dt.datetime(2026, 10, 28, 19, 30)


def entry(index: int, text: str, **kwargs) -> FakeResult:
    return FakeResult(index, text, **kwargs)


def translated_file(
    *results: FakeResult,
    sources: dict[int, str] | None = None,
    dropped: list[int] | None = None,
    fixed: list[int] | None = None,
    reviewed: list[int] | None = None,
    suggestions: dict[int, str] | None = None,
) -> FakeFile:
    return FakeFile(
        results=list(results),
        sources=sources or {},
        dropped=dropped or [],
        fixed=fixed or [],
        reviewed=reviewed or [],
        suggestions=suggestions or {},
    )


def test_report_path_drops_the_en_tag():
    assert verify.report_path("Movie (2026).en.srt") == Path("Movie (2026).report.txt")


def test_report_path_without_a_language_tag():
    assert verify.report_path("Movie.srt") == Path("Movie.report.txt")


def test_write_report_writes_utf8(tmp_path):
    target = verify.write_report(tmp_path / "report.txt", "สวัสดี\n")
    assert target.read_text(encoding="utf-8") == "สวัสดี\n"


def test_write_report_creates_parent_directory(tmp_path):
    target = verify.write_report(tmp_path / "a" / "b" / "report.txt", "hi")
    assert target.exists()


def test_a_clean_report_says_nothing_was_flagged():
    translated = translated_file(entry(1, "สวัสดี"), sources={1: "Hello."})
    verification = verify.verify_file(translated)
    text = verify.render_report(translated, verification, now=NOW)
    assert "Checked 1 entry; nothing flagged." in text
    assert "Dropped before translation: none" in text
    assert "Layout repairs: none (no multi-line entries)" in text
    assert "Each flag" not in text


def test_the_report_names_the_file_the_output_and_the_model():
    translated = translated_file(entry(1, "สวัสดี"), sources={1: "Hello."})
    text = verify.render_report(
        translated,
        verify.verify_file(translated),
        source_name="Movie.en.srt",
        target_name="Movie.th.srt",
        model="test/model",
        now=NOW,
    )
    assert "source:  Movie.en.srt" in text
    assert "output:  Movie.th.srt" in text
    assert "model:   test/model" in text
    assert "created: 2026-10-28 19:30" in text


def test_the_report_leaves_out_an_empty_model_line():
    translated = translated_file(entry(1, "สวัสดี"), sources={1: "Hello."})
    text = verify.render_report(translated, verify.verify_file(translated), now=NOW)
    assert "model:" not in text


def test_each_flag_gets_a_block_with_reason_original_and_translation():
    translated = translated_file(
        entry(1, "สวัสดี world"), sources={1: "Hello world."}
    )
    text = verify.render_report(
        translated, verify.verify_file(translated), now=NOW
    )
    assert "[1] english_left" in text
    assert "reason: English left in the translation: world" in text
    assert "original:\n    Hello world." in text
    assert "translation:\n    สวัสดี world" in text


def test_a_multi_line_original_keeps_its_lines_in_the_report():
    translated = translated_file(entry(1, "หนึ่งสอง"), sources={1: "One.\nTwo."})
    text = verify.render_report(translated, verify.verify_file(translated), now=NOW)
    assert "original:\n    One.\n    Two." in text


def test_the_report_counts_flags_by_kind_in_a_stable_order():
    translated = translated_file(
        entry(1, "หนึ่ง English"),
        entry(2, "สวัสดี", ok=False, reason="transport"),
        sources={1: "One.\nTwo.", 2: "Hello."},
    )
    text = verify.render_report(translated, verify.verify_file(translated), now=NOW)
    assert "Checked 2 entries, flagged 2 entries." in text
    assert "Flags: failed 1, line_count 1, english_left 1" in text


def test_the_report_mentions_dropped_entries():
    translated = translated_file(
        entry(1, "สวัสดี"), sources={1: "Hello."}, dropped=[2, 3]
    )
    text = verify.render_report(translated, verify.verify_file(translated), now=NOW)
    assert "Dropped before translation: 2 entries" in text


def test_one_dropped_entry_is_singular():
    translated = translated_file(
        entry(1, "สวัสดี"), sources={1: "Hello."}, dropped=[2]
    )
    text = verify.render_report(translated, verify.verify_file(translated), now=NOW)
    assert "Dropped before translation: 1 entry " in text


def test_the_report_mentions_entries_it_could_not_verify():
    translated = translated_file(entry(1, "สวัสดี"), sources={})
    text = verify.render_report(translated, verify.verify_file(translated), now=NOW)
    assert "Not verified: 1 entry" in text


def test_the_report_ends_with_a_newline():
    translated = translated_file(entry(1, "สวัสดี"), sources={1: "Hello."})
    text = verify.render_report(translated, verify.verify_file(translated), now=NOW)
    assert text.endswith("\n")


# --- layout summary ---------------------------------------------------------


def test_layout_summary_reports_the_merge_rate():
    translated = translated_file(entry(1, "หนึ่ง\nสอง"), sources={1: "One.\nTwo."})
    summary = verify.layout_summary(translated)
    assert summary == "Layout repairs: 0 of 1 multi-line entry re-split (0%)"


def test_layout_summary_reports_dash_and_proportional_splits():
    first = entry(1, "- หนึ่ง\n- สอง")
    first.rewrapped = "dash"
    first.dashes_restored = True
    second = entry(2, "หนึ่ง\nสอง")
    second.rewrapped = "proportional"
    translated = translated_file(
        first, second, sources={1: "- One.\n- Two.", 2: "Three.\nFour."}
    )
    summary = verify.layout_summary(translated)
    assert "2 of 2 multi-line entries re-split" in summary
    assert "(100%, 1 dash, 1 proportional)" in summary
    assert "1 speaker dash put back" in summary


def test_layout_summary_counts_entries_left_merged():
    merged = FakeResult(1, "อยากไปพบครูแนะแนวที่โรงเรียนไหมล่ะ?")
    merged.rewrapped = "unrepaired"
    translated = FakeFile(results=[merged], sources={1: "One.\nTwo."})
    summary = verify.layout_summary(translated)
    assert summary == (
        "Layout repairs: 0 of 1 multi-line entry re-split (0%), "
        "1 entry left merged (no safe break point)"
    )


def test_layout_summary_counts_extra_lines():
    extra = entry(1, "หนึ่ง\nสอง\nสาม")
    extra.rewrapped = "extra_lines"
    translated = translated_file(extra, sources={1: "One.\nTwo."})
    assert "0 of 1 multi-line entry re-split (0%)" in verify.layout_summary(translated)
    assert "1 entry with extra lines" in verify.layout_summary(translated)


def test_layout_summary_handles_a_plain_file():
    translated = translated_file(entry(1, "สวัสดี"), sources={1: "Hello."})
    assert verify.layout_summary(translated) == (
        "Layout repairs: none (no multi-line entries)"
    )


@pytest.mark.parametrize(
    "count, word, plural, expected",
    [
        (0, "entry", "entries", "0 entries"),
        (1, "entry", "entries", "1 entry"),
        (2, "entry", "entries", "2 entries"),
        (1, "speaker dash", "speaker dashes", "1 speaker dash"),
        (3, "speaker dash", "speaker dashes", "3 speaker dashes"),
    ],
)
def test_plural(count, word, plural, expected):
    assert verify._plural(count, word, plural) == expected


# --- the fix pass in the report --------------------------------------------


def test_the_report_notes_a_successful_fix_pass():
    translated = translated_file(
        entry(1, "หนึ่ง"), entry(2, "สวัสดี"), sources={1: "One.", 2: "Hello."}, fixed=[1]
    )
    text = verify.render_report(
        translated, verify.verify_file(translated), now=NOW
    )
    assert "Fix pass: re-translated 1 entry (all clean now)" in text


def test_the_report_notes_a_fix_that_left_flags():
    translated = translated_file(
        entry(1, "สวัสดี world"), sources={1: "Hello world."}, fixed=[1]
    )
    text = verify.render_report(
        translated, verify.verify_file(translated), now=NOW
    )
    assert "Fix pass: re-translated 1 entry (0 clean, 1 still flagged)" in text


def test_no_fix_pass_means_no_fix_line():
    translated = translated_file(entry(1, "สวัสดี"), sources={1: "Hello."})
    text = verify.render_report(
        translated, verify.verify_file(translated), now=NOW
    )
    assert "Fix pass" not in text


# --- review suggestions in the report --------------------------------------


def test_suggestions_are_listed_near_the_top():
    translated = translated_file(
        entry(2, "ไปmarketนะ"), sources={2: "To the market."},
        reviewed=[2], suggestions={2: "ไปตลาดนะ"},
    )
    text = verify.render_report(
        translated, verify.verify_file(translated), now=NOW
    )
    assert "Suggestions (from --review, nothing applied):" in text
    assert "  [2] ไปตลาดนะ" in text
    assert text.index("Suggestions") < text.index("Dropped before translation")


def test_suggestions_are_sorted_by_entry():
    translated = translated_file(
        entry(1, "สวัสดี world"), entry(3, "ไปmarketนะ"),
        sources={1: "Hello world.", 3: "To the market."},
        reviewed=[1, 3], suggestions={3: "ไปตลาดนะ", 1: "สวัสดี"},
    )
    text = verify.render_report(
        translated, verify.verify_file(translated), now=NOW
    )
    assert text.index("  [1] สวัสดี") < text.index("  [3] ไปตลาดนะ")


def test_a_multi_line_suggestion_stays_on_one_line():
    translated = translated_file(
        entry(1, "หนึ่ง"), sources={1: "One.\nTwo."},
        reviewed=[1], suggestions={1: "หนึ่ง\nสอง"},
    )
    text = verify.render_report(
        translated, verify.verify_file(translated), now=NOW
    )
    assert "  [1] หนึ่ง / สอง" in text


def test_a_review_that_agreed_says_so():
    translated = translated_file(
        entry(1, "สวัสดี world"), sources={1: "Hello world."}, reviewed=[1]
    )
    text = verify.render_report(
        translated, verify.verify_file(translated), now=NOW
    )
    assert "Suggestions (from --review): none, it agreed with every flagged line" in text


def test_no_review_means_no_suggestion_section():
    translated = translated_file(entry(1, "สวัสดี"), sources={1: "Hello."})
    text = verify.render_report(
        translated, verify.verify_file(translated), now=NOW
    )
    assert "Suggestions" not in text


def test_one_line_collapses_blank_lines_too():
    assert verify._one_line("  one\n\n two \n") == "one / two"
