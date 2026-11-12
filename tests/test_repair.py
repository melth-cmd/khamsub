from __future__ import annotations

import datetime as dt
from pathlib import Path

import srt

import repair


def cue(index: int, start: float, end: float, text: str) -> srt.Subtitle:
    return srt.Subtitle(
        index=index,
        start=dt.timedelta(seconds=start),
        end=dt.timedelta(seconds=end),
        content=text,
    )


# --- one cue ----------------------------------------------------------------


def test_spaces_are_trimmed_and_collapsed():
    fix = repair.repair_text(1, "  สวัสดี   ครับ  ")
    assert fix.text == "สวัสดี ครับ"
    assert [change.kind for change in fix.changes] == ["whitespace"]


def test_blank_lines_inside_a_cue_are_dropped():
    assert repair.repair_text(1, "หนึ่ง\n\nสอง").text == "หนึ่ง\nสอง"


def test_a_stray_digit_placeholder_is_removed():
    fix = repair.repair_text(1, "ตกลง {2}")
    assert fix.text == "ตกลง"
    assert [change.kind for change in fix.changes] == ["placeholder"]


def test_a_positioning_tag_is_not_a_stray_placeholder():
    assert repair.repair_text(1, "{\\an8}เธอ").text == "{\\an8}เธอ"


def test_a_missing_close_is_added():
    fix = repair.repair_text(1, "<i>สวัสดี")
    assert fix.text == "<i>สวัสดี</i>"
    assert [change.kind for change in fix.changes] == ["tag"]


def test_an_orphan_close_is_dropped():
    fix = repair.repair_text(1, "สวัสดี</b>")
    assert fix.text == "สวัสดี"
    assert [change.kind for change in fix.changes] == ["tag"]


def test_crossed_tags_are_reported_not_guessed():
    fix = repair.repair_text(1, "<i><b>สวัสดี</i></b>")
    assert fix.text == "<i><b>สวัสดี</i></b>"
    assert [problem.kind for problem in fix.problems] == ["tags"]


def test_more_than_one_missing_tag_is_reported():
    fix = repair.repair_text(1, "<i><b>สวัสดี")
    assert fix.text == "<i><b>สวัสดี"
    assert [problem.kind for problem in fix.problems] == ["tags"]


def test_three_lines_are_reduced_to_two_at_a_space():
    text = "ฉันอยากกินขนมก่อนนอน\nแต่ต้องแปรงฟันก่อน\nนะแม่นะ"
    fix = repair.repair_text(1, text)
    assert len(fix.text.split("\n")) == 2
    assert [change.kind for change in fix.changes] == ["wrap"]


def test_three_lines_without_a_safe_space_are_left_alone():
    text = "ก" * 30 + "\n" + "ข" * 30 + "\n" + "ค" * 30
    fix = repair.repair_text(1, text)
    assert fix.text == text
    assert [problem.kind for problem in fix.problems] == ["long_cue"]


def test_two_lines_are_not_joined():
    assert repair.repair_text(1, "หนึ่ง\nสอง").text == "หนึ่ง\nสอง"


def test_repair_text_is_idempotent():
    once = repair.repair_text(1, "  <i>หนึ่ง</i> {3}  ")
    twice = repair.repair_text(1, once.text)
    assert twice.text == once.text
    assert twice.changes == []
    assert twice.problems == []


def test_repair_cue_keeps_index_and_timestamps():
    original = cue(7, 1, 2, "  สวัสดี  ")
    fixed, _ = repair.repair_cue(original)
    assert fixed.index == 7
    assert fixed.start == original.start
    assert fixed.end == original.end
    assert fixed.content == "สวัสดี"


# --- one file ---------------------------------------------------------------


def test_a_clean_pair_has_nothing_to_do():
    english = [cue(1, 0, 2, "Hello.")]
    thai = [cue(1, 0, 2, "สวัสดี")]
    result = repair.repair_subtitles(thai, english)
    assert result.changes == []
    assert result.problems == []
    assert result.candidates == []
    assert result.alignment.ok


def test_leftover_english_and_empty_cues_are_candidates():
    english = [
        cue(1, 0, 2, "Hello."),
        cue(2, 3, 5, "How are you?"),
        cue(3, 6, 8, "I am fine."),
    ]
    thai = [
        cue(1, 0, 2, "สวัสดี"),
        cue(2, 3, 5, "How are you?"),
        cue(3, 6, 8, "<i></i>"),
    ]
    result = repair.repair_subtitles(thai, english)
    kinds = [problem.kind for problem in result.problems]
    assert "english_left" in kinds
    assert "empty" in kinds
    assert result.candidates == [2, 3]


def test_a_truncated_cue_is_a_candidate():
    english = [cue(1, 0, 2, "Tell him I'm not happy.\nI mean it.")]
    thai = [cue(1, 0, 2, "บอกเขา")]
    result = repair.repair_subtitles(thai, english)
    assert [problem.kind for problem in result.problems] == ["truncation"]
    assert result.candidates == [1]


def test_timing_problems_are_reported_and_never_changed():
    english = [cue(1, 0, 0.3, "One."), cue(2, 0.2, 2, "Two.")]
    thai = [cue(1, 0, 0.3, "หนึ่ง"), cue(2, 0.2, 2, "สอง")]
    result = repair.repair_subtitles(thai, english)
    kinds = [problem.kind for problem in result.problems]
    assert kinds.count("timing") == 2
    assert result.subtitles[0].start == thai[0].start
    assert result.subtitles[0].end == thai[0].end


def test_a_thai_only_cue_is_reported_without_a_source():
    english = [cue(1, 0, 2, "A."), cue(2, 3, 5, "B."), cue(3, 6, 8, "C.")]
    thai = [cue(1, 0, 2, "เอ"), cue(2, 3, 5, "บี"), cue(9, 10, 12, "เพิ่ม")]
    result = repair.repair_subtitles(thai, english)
    assert (9, "no_source") in [(p.index, p.kind) for p in result.problems]
    assert result.alignment.ok
    assert 9 not in result.candidates


def test_a_split_cue_is_ambiguous_and_blocks_retranslation():
    english = [cue(1, 0, 2, "A."), cue(2, 2, 4, "B.")]
    thai = [cue(1, 0, 4, "หนึ่ง")]
    result = repair.repair_subtitles(thai, english)
    assert (1, "ambiguous") in [(p.index, p.kind) for p in result.problems]
    assert (None, "alignment") in [(p.index, p.kind) for p in result.problems]
    assert result.candidates == []


def test_poor_alignment_still_reports_text_problems():
    english = [cue(1, 100, 102, "Hello.")]
    thai = [cue(1, 0, 2, "Hello.")]
    result = repair.repair_subtitles(thai, english)
    assert (None, "alignment") in [(p.index, p.kind) for p in result.problems]
    assert any(p.kind == "english_left" for p in result.problems)
    assert result.candidates == []


def test_a_crossed_tag_is_not_reported_twice():
    english = [cue(1, 0, 2, "Hello.")]
    thai = [cue(1, 0, 2, "<i><b>สวัสดี</i></b>")]
    result = repair.repair_subtitles(thai, english)
    kinds = [problem.kind for problem in result.problems]
    assert kinds.count("tags") == 1
    assert "tag_mismatch" not in kinds


def test_mixed_dash_styles_are_reported():
    english = [cue(1, 0, 2, "One."), cue(2, 3, 5, "Two.")]
    thai = [cue(1, 0, 2, "- หนึ่ง"), cue(2, 3, 5, "\u2010 สอง")]
    result = repair.repair_subtitles(thai, english)
    assert ("dashes" in [problem.kind for problem in result.problems])


def test_repair_file_reads_a_bom_and_crlf(tmp_path):
    english_path = tmp_path / "movie.en.srt"
    thai_path = tmp_path / "movie.th.srt"
    english_path.write_text(
        srt.compose([cue(1, 0, 2, "Hello.")], reindex=False, eol="\n"),
        encoding="utf-8",
    )
    body = srt.compose([cue(1, 0, 2, "สวัสดี")], reindex=False, eol="\r\n")
    thai_path.write_bytes(b"\xef\xbb\xbf" + body.encode("utf-8"))

    result = repair.repair_file(thai_path, english_path)

    assert result.encoding == "utf-8-sig"
    assert result.thai_count == 1
    assert result.english_count == 1
    assert result.thai_path == thai_path
    assert [change.kind for change in result.changes] == ["line endings", "encoding"]


# --- the report -------------------------------------------------------------


def test_report_separates_fixes_from_look_at_this():
    english = [cue(1, 0, 2, "Hello.")]
    thai = [cue(1, 0, 2, "  สวัสดี  ")]
    result = repair.repair_subtitles(thai, english)
    result.thai_path = Path("movie.th.srt")
    result.english_path = Path("movie.en.srt")

    text = repair.render_report(result)

    assert "Repair report: movie.th.srt" in text
    assert "aligned 1/1" in text
    assert "Repaired 1 change in 1 cue" in text
    assert "whitespace" in text
    assert "Needs a look: nothing" in text
    assert "Retranslation candidates: none" in text


def test_report_says_when_alignment_is_poor():
    english = [cue(1, 100, 102, "Hello.")]
    thai = [cue(1, 0, 2, "Hello.")]
    result = repair.repair_subtitles(thai, english)
    result.thai_path = Path("movie.th.srt")
    result.english_path = Path("movie.en.srt")

    text = repair.render_report(result)

    assert "text checks only, no retranslation" in text
    assert "Retranslation: disabled" in text


def test_report_caps_the_problem_list():
    result = repair.RepairResult(
        problems=[
            repair.Problem(index, "timing", "short")
            for index in range(1, repair.MAX_PROBLEM_LINES + 6)
        ]
    )
    text = repair.render_report(result)
    assert "... and 5 more" in text


def test_report_pluralises_one_change():
    result = repair.RepairResult(
        changes=[repair.Change(1, "whitespace", "trimmed")],
        thai_count=1,
    )
    assert "Repaired 1 change in 1 cue" in repair.render_report(result)


def test_has_thai_needs_a_thai_character():
    assert repair.has_thai("สวัสดี")
    assert not repair.has_thai("Hello there.")
    assert repair.has_thai("Hello สวัสดี")


def test_dry_run_report_uses_the_conditional_wording():
    result = repair.RepairResult(
        changes=[repair.Change(1, "whitespace", "trimmed")],
        thai_count=1,
    )
    text = repair.render_report(result, dry_run=True)
    assert "Would repair 1 change in 1 cue" in text
    assert "dry run; nothing written" in text


def test_refresh_audit_recomputes_text_problems():
    english = [cue(1, 0, 2, "Hello.")]
    thai = [cue(1, 0, 2, "Hello.")]
    result = repair.repair_subtitles(thai, english)
    assert result.candidates == [1]

    result.subtitles = [cue(1, 0, 2, "สวัสดี")]
    repair.refresh_audit(result)

    assert result.candidates == []
    assert not any(problem.kind == "english_left" for problem in result.problems)


def test_report_names_a_retranslation():
    from llm import Usage

    result = repair.RepairResult(
        retranslated=[2],
        retranslate_usage=Usage(10, 5),
        thai_count=2,
    )
    text = repair.render_report(result)
    assert "Retranslated 1 cue (15 tokens)" in text
