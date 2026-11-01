from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
import srt

import glossary
import pipeline
import srtio
from batcher import SourceEntry
from conftest import FakeClient

FIXTURES = Path(__file__).parent / "fixtures"


def subtitle(index: int, content: str, start_s: int = 1) -> srt.Subtitle:
    start = dt.timedelta(seconds=start_s)
    return srt.Subtitle(index=index, start=start, end=start + dt.timedelta(seconds=2), content=content)


def run(client, subtitles, **kwargs):
    kwargs.setdefault("sleep", lambda seconds: None)
    return pipeline.translate_subtitles(client, subtitles, **kwargs)


# --- output_path_for --------------------------------------------------------


def test_output_path_strips_en_suffix():
    assert srtio.output_path_for("Movie (2026).en.srt") == Path("Movie (2026).th.srt")


def test_output_path_without_en_suffix():
    assert srtio.output_path_for("Movie (2026).srt") == Path("Movie (2026).th.srt")


def test_output_path_is_case_insensitive():
    assert srtio.output_path_for("Movie.EN.SRT") == Path("Movie.th.srt")


def test_output_path_keeps_directory():
    assert srtio.output_path_for("/films/a.en.srt") == Path("/films/a.th.srt")


def test_output_path_for_other_extension():
    assert srtio.output_path_for("notes.txt") == Path("notes.th.srt")


# --- prepare ----------------------------------------------------------------


def test_prepare_protects_tags():
    prepared = pipeline.prepare([subtitle(1, "<i>Hello.</i>")])
    assert prepared.entries == [SourceEntry(1, "{0}Hello.{1}")]
    assert prepared.dropped == []


def test_prepare_drops_hi_only_entries_when_stripping():
    prepared = pipeline.prepare([subtitle(1, "[door slams]"), subtitle(2, "Hello.")], strip_hi=True)
    assert [e.index for e in prepared.entries] == [2]
    assert prepared.dropped == [1]


def test_prepare_keeps_hi_entries_by_default():
    prepared = pipeline.prepare([subtitle(1, "[door slams]")])
    assert [e.index for e in prepared.entries] == [1]


def test_prepare_drops_entries_with_nothing_after_tag_stripping():
    prepared = pipeline.prepare([subtitle(1, "<i></i>")])
    assert prepared.entries == []
    assert prepared.dropped == [1]


# --- end to end -------------------------------------------------------------


def test_translates_a_file_and_keeps_timestamps(tmp_path):
    from conftest import FakeClient

    client = FakeClient(["[1] สวัสดี\n[2] สบายดีไหม\n[3] {0}สบายดี ขอบคุณ{1}"])
    loaded = srtio.load_srt(FIXTURES / "simple.srt")
    result = run(client, loaded.subtitles)

    assert [s.content for s in result.subtitles] == [
        "สวัสดี",
        "สบายดีไหม",
        "<i>สบายดี ขอบคุณ</i>",
    ]
    assert [s.index for s in result.subtitles] == [1, 2, 3]
    assert [s.start for s in result.subtitles] == [s.start for s in loaded.subtitles]
    assert result.failures == []


def test_end_to_end_write_and_read_back(tmp_path):
    from conftest import FakeClient

    target = tmp_path / "out.srt"
    client = FakeClient(["[1] สวัสดี\n[2] สบายดีไหม\n[3] {0}สบายดี{1}"])
    result, path = pipeline.translate_file(
        client, FIXTURES / "simple.srt", target, sleep=lambda s: None
    )
    assert path == target

    reloaded = srtio.load_srt(target)
    assert [s.content for s in reloaded.subtitles][0] == "สวัสดี"


def test_tags_are_restored_even_for_failed_entries():
    from conftest import FakeClient

    client = FakeClient(["[1] 你好"] * 4)
    result = run(client, [subtitle(1, "<i>Hello.</i>")], max_retries=3)

    assert result.failures[0].ok is False
    assert result.subtitles[0].content == "<i>Hello.</i>"  # English kept, tags intact


def test_hi_dropped_entries_disappear_from_the_output():
    from conftest import FakeClient

    client = FakeClient(["[2] สวัสดี"])
    subtitles = [subtitle(1, "[door slams]"), subtitle(2, "Hello.")]
    result = run(client, subtitles, strip_hi=True)

    assert [s.index for s in result.subtitles] == [2]
    assert result.dropped == [1]


def test_nothing_to_translate_makes_no_calls():
    from conftest import FakeClient

    client = FakeClient()
    result = run(client, [subtitle(1, "[door slams]")], strip_hi=True)
    assert client.call_count == 0
    assert result.subtitles == []


def test_usage_is_summed_across_batches():
    from conftest import FakeClient
    from llm import Completion, Usage

    client = FakeClient(
        [Completion("[1] หนึ่ง", Usage(10, 5)), Completion("[2] สอง", Usage(3, 2))]
    )
    subtitles = [subtitle(1, "One."), subtitle(2, "Two.")]
    result = run(client, subtitles, size=1)

    assert client.call_count == 2
    assert result.usage.prompt_tokens == 13
    assert result.usage.completion_tokens == 7


def test_second_batch_context_carries_thai():
    from conftest import FakeClient

    client = FakeClient(["[1] หนึ่ง", "[2] สอง"])
    subtitles = [subtitle(1, "One."), subtitle(2, "Two.")]
    run(client, subtitles, size=1)

    second_prompt = client.calls[1][1]["content"]
    assert "TH: หนึ่ง" in second_prompt


def test_results_are_sorted_by_index():
    from conftest import FakeClient

    client = FakeClient(["[2] สอง\n[1] หนึ่ง"])
    result = run(client, [subtitle(1, "One."), subtitle(2, "Two.")])
    assert [r.index for r in result.results] == [1, 2]


def test_output_encoding_with_bom(tmp_path):
    from conftest import FakeClient

    target = tmp_path / "out.srt"
    client = FakeClient(["[1] สวัสดี\n[2] สอง\n[3] {0}สาม{1}"])
    pipeline.translate_file(
        client, FIXTURES / "simple.srt", target, encoding="utf-8-sig", sleep=lambda s: None
    )
    assert target.read_bytes().startswith(b"\xef\xbb\xbf")


# --- multi-line entries, dashes ---------------------------------------------


def test_carriage_returns_are_normalised_before_batching():
    prepared = pipeline.prepare([subtitle(1, "One.\r\nTwo.\rThree.")])
    assert prepared.entries == [SourceEntry(1, "One.\nTwo.\nThree.")]


def test_two_line_entry_keeps_two_lines_end_to_end():
    from conftest import FakeClient

    subtitles = [subtitle(1, "-Hello there.\n-Hi.")]
    client = FakeClient(["[1] สวัสดีครับ\nหวัดดี"])
    result = run(client, subtitles)

    assert result.subtitles[0].content == "-สวัสดีครับ\n-หวัดดี"
    assert result.restored_dashes[0].index == 1
    assert result.restored_dashes[0].dashes_restored is True


def test_restored_dashes_are_reported_from_cache_too(tmp_path):
    from conftest import FakeClient

    source = tmp_path / "dash.en.srt"
    source.write_text(
        "1\n00:00:01,000 --> 00:00:03,000\n-Hello.\n-Hi.\n\n",
        encoding="utf-8",
    )
    first = FakeClient(["[1] สวัสดี\nหวัดดี"])  # both dashes dropped
    pipeline.translate_file(first, source, sleep=lambda s: None)

    second = FakeClient()
    result, _ = pipeline.translate_file(second, source, sleep=lambda s: None)
    assert second.call_count == 0
    assert result.restored_dashes[0].from_cache is True
    assert result.subtitles[0].content == "-สวัสดี\n-หวัดดี"


def test_edge_case_file_survives_a_merging_model(tmp_path):
    """Real shapes from a HI-tagged release: turns, tags over a dash, music, signs."""
    from conftest import FakeClient

    # Every entry merged into one line and the speaker dashes dropped.
    replies = [
        "[1] แบบแซมเหรอ โอ๊ย พระเจ้า ฝันไปเถอะ\n"
        "[2] {0}สาบานเลย{1} สาบาน\n"
        "[3] แต่ว่าสั้นเหรอ -เออ ดูดีไหมล่ะ\n"
        "[4] {0} ปล่อยไป {1} ปล่อยไป\n"
        "[5] [SIGN: CLOSED] กลับมาพรุ่งนี้ ขอบคุณ"
    ]
    result, _ = pipeline.translate_file(
        FakeClient(replies), FIXTURES / "edge_cases.srt", tmp_path / "out.srt",
        sleep=lambda s: None,
    )

    contents = {s.index: s.content for s in result.subtitles}
    assert contents[1] == "-แบบแซมเหรอ\n-โอ๊ย พระเจ้า ฝันไปเถอะ"
    assert contents[2] == "<i>-สาบานเลย</i>\n-สาบาน"
    assert contents[3] == "แต่ว่าสั้นเหรอ\n-เออ ดูดีไหมล่ะ"
    assert contents[4].count("\n") == 1 and contents[4].startswith("\u266a")
    assert contents[5].split("\n")[0].count("[") == contents[5].split("\n")[0].count("]")
    assert len(result.restored_dashes) == 2  # entries 1 and 2
    assert result.failures == []


def test_entry_with_a_blank_line_is_rewrapped():
    from conftest import FakeClient

    client = FakeClient(["[1] หนึ่ง สอง"])
    result = run(client, [subtitle(1, "One.\n\nTwo.")])
    assert result.subtitles[0].content == "หนึ่ง\nสอง"


def test_failure_list_is_exposed_for_the_report():
    from conftest import FakeClient

    client = FakeClient(["[1] สวัสดี\n[2] 你好"] * 2)
    result = run(client, [subtitle(1, "One."), subtitle(2, "Two.")], max_retries=1)
    assert [f.index for f in result.failures] == [2]
    assert result.failures[0].reason in {"foreign_script", "missing_thai"}


# --- cache / resume ---------------------------------------------------------


REPLIES = "[1] สวัสดี\n[2] สบายดีไหม\n[3] {0}สบายดี{1}"


def copied_fixture(tmp_path, name="movie.en.srt") -> Path:
    target = tmp_path / name
    target.write_bytes((FIXTURES / "simple.srt").read_bytes())
    return target


def test_cache_file_is_written_next_to_the_source(tmp_path):
    from conftest import FakeClient

    source = copied_fixture(tmp_path)
    pipeline.translate_file(FakeClient([REPLIES]), source, sleep=lambda s: None)
    assert (tmp_path / "movie.cache.json").exists()
    assert (tmp_path / "movie.th.srt").exists()


def test_second_run_makes_no_calls(tmp_path):
    from conftest import FakeClient

    source = copied_fixture(tmp_path)
    first = FakeClient([REPLIES])
    pipeline.translate_file(first, source, sleep=lambda s: None)
    assert first.call_count == 1

    second = FakeClient()  # would explode if anything were requested
    result, _ = pipeline.translate_file(second, source, sleep=lambda s: None)
    assert second.call_count == 0
    assert [s.content for s in result.subtitles] == [
        "สวัสดี",
        "สบายดีไหม",
        "<i>สบายดี</i>",
    ]
    assert all(entry.from_cache for entry in result.results)


def test_cache_can_be_disabled(tmp_path):
    from conftest import FakeClient

    source = copied_fixture(tmp_path)
    pipeline.translate_file(FakeClient([REPLIES]), source, use_cache=False, sleep=lambda s: None)
    assert not (tmp_path / "movie.cache.json").exists()


def test_only_uncached_entries_are_requested(tmp_path):
    from conftest import FakeClient
    import cache as cache_module

    source = copied_fixture(tmp_path)
    store = cache_module.Cache(tmp_path / "movie.cache.json", model="m/x")
    store.put(1, "Hello there.", "สวัสดี")
    store.put(2, "How are you?", "สบายดีไหม")
    store.save()

    client = FakeClient(["[3] {0}สบายดี{1}"])
    result, _ = pipeline.translate_file(client, source, model="m/x", sleep=lambda s: None)

    assert client.call_count == 1
    prompt = client.calls[0][1]["content"]
    assert "[TRANSLATE]\n[3] {0}Fine, thanks.{1}" in prompt
    assert "[1] Hello there." not in prompt.split("[TRANSLATE]")[1]
    assert result.failures == []


def test_cached_thai_is_used_as_context(tmp_path):
    from conftest import FakeClient
    import cache as cache_module

    source = copied_fixture(tmp_path)
    store = cache_module.Cache(tmp_path / "movie.cache.json", model="m/x")
    store.put(1, "Hello there.", "สวัสดี")
    store.save()

    client = FakeClient(["[2] สบายดีไหม\n[3] {0}สบายดี{1}"])
    pipeline.translate_file(client, source, model="m/x", sleep=lambda s: None, size=5)
    prompt = client.calls[0][1]["content"]
    assert "TH: สวัสดี" in prompt


def test_failed_entries_are_not_cached(tmp_path):
    from conftest import FakeClient
    import cache as cache_module

    source = copied_fixture(tmp_path)
    client = FakeClient(["[1] สวัสดี\n[2] 你好\n[3] {0}สบายดี{1}"])
    client.queue("[2] 你好", "[2] 你好", "[2] 你好")
    pipeline.translate_file(client, source, model="m/x", sleep=lambda s: None)

    store = cache_module.Cache(tmp_path / "movie.cache.json", model="m/x").load()
    assert 1 in store and 3 in store
    assert 2 not in store


def test_changed_source_line_is_retranslated(tmp_path):
    from conftest import FakeClient

    source = copied_fixture(tmp_path)
    pipeline.translate_file(FakeClient([REPLIES]), source, sleep=lambda s: None)

    edited = source.read_text(encoding="utf-8").replace("How are you?", "Where were you?")
    source.write_text(edited, encoding="utf-8")

    client = FakeClient(["[2] ไปไหนมา"])
    result, _ = pipeline.translate_file(client, source, sleep=lambda s: None)
    assert client.call_count == 1
    assert result.translated[2] == "ไปไหนมา"
    assert result.translated[1] == "สวัสดี"


# --- estimate (dry run) -----------------------------------------------------


def test_estimate_counts_entries_and_batches():
    loaded = srtio.load_srt(FIXTURES / "simple.srt")
    estimate = pipeline.estimate(loaded.subtitles, size=2)
    assert estimate.entries == 3
    assert estimate.batches == 2
    assert estimate.total_tokens > 0


def test_estimate_counts_the_bible_once_per_batch():
    loaded = srtio.load_srt(FIXTURES / "simple.srt")
    text = "Somsak uses ผม + ครับ with strangers."
    without = pipeline.estimate(loaded.subtitles, size=1, bible="")
    with_bible = pipeline.estimate(loaded.subtitles, size=1, bible=text)

    assert with_bible.batches == 3
    per_batch = len(text) + pipeline.BIBLE_OVERHEAD_CHARS
    expected = 3 * per_batch / pipeline.CHARS_PER_PROMPT_TOKEN
    assert with_bible.prompt_tokens - without.prompt_tokens == pytest.approx(expected, abs=1)


def test_an_empty_bible_changes_the_estimate_not_at_all():
    loaded = srtio.load_srt(FIXTURES / "simple.srt")
    assert (
        pipeline.estimate(loaded.subtitles, bible="  \n").prompt_tokens
        == pipeline.estimate(loaded.subtitles).prompt_tokens
    )


def test_estimate_honours_a_range():
    loaded = srtio.load_srt(FIXTURES / "simple.srt")
    estimate = pipeline.estimate(loaded.subtitles, range_="2-3")
    assert estimate.entries == 2
    assert estimate.batches == 1


def test_estimate_counts_dropped_entries():
    subtitles = [subtitle(1, "[door slams]"), subtitle(2, "Hello.")]
    estimate = pipeline.estimate(subtitles, strip_hi=True)
    assert estimate.entries == 1
    assert estimate.dropped == 1


def test_estimate_cost_uses_the_configured_prices():
    loaded = srtio.load_srt(FIXTURES / "simple.srt")
    estimate = pipeline.estimate(
        loaded.subtitles, input_usd_per_mtok=1.0, output_usd_per_mtok=1.0
    )
    expected = (estimate.prompt_tokens + estimate.completion_tokens) / 1e6
    assert estimate.cost_usd == pytest.approx(expected)


def test_estimate_without_prices_is_free():
    loaded = srtio.load_srt(FIXTURES / "simple.srt")
    assert pipeline.estimate(loaded.subtitles).cost_usd == 0.0


def test_estimate_of_nothing_is_zero():
    estimate = pipeline.estimate([])
    assert (estimate.entries, estimate.batches, estimate.total_tokens) == (0, 0, 0)


# --- line-break repair (A + D) ----------------------------------------------


def test_merged_model_output_is_rewrapped():
    from conftest import FakeClient

    client = FakeClient(["[1] หนึ่ง สอง"])
    result = run(client, [subtitle(1, "One.\nTwo.")])
    assert result.subtitles[0].content.count("\n") == 1
    assert result.results[0].rewrapped == "proportional"
    assert result.failures == []


def test_merged_output_without_a_space_is_reported_not_cut():
    from conftest import FakeClient

    client = FakeClient(["[1] อยากไปพบครูแนะแนวที่โรงเรียนไหมล่ะ?"])
    result = run(client, [subtitle(1, "One.\nTwo.")])
    assert result.subtitles[0].content == "อยากไปพบครูแนะแนวที่โรงเรียนไหมล่ะ?"
    assert result.results[0].rewrapped == "unrepaired"
    assert [r.index for r in result.merged] == [1]
    assert result.rewrapped == []
    assert result.failures == []


def test_dash_speakers_are_split_at_the_dash():
    from conftest import FakeClient

    client = FakeClient(["[1] -หนึ่ง -สอง"])
    result = run(client, [subtitle(1, "-One\n-Two")])
    assert result.subtitles[0].content == "-หนึ่ง\n-สอง"
    assert result.results[0].rewrapped == "dash"


def test_cached_merged_entry_is_rewrapped_on_read(tmp_path):
    from conftest import FakeClient
    import cache as cache_module

    store = cache_module.Cache(tmp_path / "c.cache.json", model="m")
    store.put(1, "One.\nTwo.", "หนึ่ง สอง")  # merged by an older run
    client = FakeClient()  # nothing may be requested
    result = run(client, [subtitle(1, "One.\nTwo.")], cache=store)

    assert client.call_count == 0
    assert result.subtitles[0].content == "หนึ่ง\nสอง"
    assert [r.index for r in result.rewrapped] == [1]


def test_cache_stores_the_raw_model_output(tmp_path):
    from conftest import FakeClient
    import json
    import cache as cache_module

    store = cache_module.Cache(tmp_path / "c.cache.json", model="m")
    run(FakeClient(["[1] หนึ่ง สอง"]), [subtitle(1, "One.\nTwo.")], cache=store)
    store.save()

    data = json.loads((tmp_path / "c.cache.json").read_text(encoding="utf-8"))
    assert data["entries"]["1"]["text"] == "หนึ่ง สอง"  # un-rewrapped


# --- per-batch glossary and cache keys (step 17) ----------------------------

THAI = "แกนดัล์ฟ"


def gandalf(spelling: str = THAI) -> "glossary.Term":
    return glossary.Term("Gandalf", "transliterate", spelling)


def test_only_terms_in_the_batch_are_sent():
    from conftest import FakeClient

    subtitles = [subtitle(1, "Gandalf speaks."), subtitle(2, "Hello.")]
    client = FakeClient(["[1] แกนดัล์ฟพูด", "[2] สวัสดี"])
    run(
        client,
        subtitles,
        size=1,
        context_before=0,
        context_after=0,
        terms=[gandalf(), glossary.Term("wizard", "translate", "พ่อมด")],
    )

    first = client.calls[0][1]["content"]
    assert f"[GLOSSARY]\nGandalf = transliterate: {THAI}" in first
    assert "wizard" not in first
    assert "[GLOSSARY]" not in client.calls[1][1]["content"]


def test_a_term_only_in_a_context_line_is_not_sent():
    from conftest import FakeClient

    subtitles = [
        subtitle(1, "Gandalf speaks."),
        subtitle(2, "Hello."),
        subtitle(3, "Goodbye."),
    ]
    client = FakeClient(["[2] สวัสดี"])
    run(
        client,
        subtitles,
        range_="2-2",
        context_before=1,
        context_after=1,
        terms=[gandalf()],
    )

    prompt = client.calls[0][1]["content"]
    assert "Gandalf speaks." in prompt  # still shown as context
    assert "[GLOSSARY]" not in prompt


def test_batch_terms_and_glossary_for_batch_use_only_the_items():
    from batcher import SourceEntry

    batch = pipeline.batcher.make_batches(
        [SourceEntry(1, "Gandalf"), SourceEntry(2, "Hi")], size=1
    )[0]
    assert pipeline.batch_terms([gandalf()], batch) == [gandalf()]
    assert pipeline.glossary_for_batch([gandalf()], batch) == (
        f"Gandalf = transliterate: {THAI}"
    )
    assert pipeline.glossary_for_batch([], batch) == ""


def make_source(tmp_path, *lines: str) -> Path:
    source = tmp_path / "movie.en.srt"
    srtio.write_srt(
        source, [subtitle(number, text, start_s=number * 3) for number, text in enumerate(lines, 1)]
    )
    return source


def test_editing_a_term_retranslates_only_the_lines_it_touches(tmp_path):
    from conftest import FakeClient

    source = make_source(tmp_path, "Gandalf speaks.", "Hello.", "Goodbye.")
    first = FakeClient(["[1] แกนดัล์ฟพูด\n[2] สวัสดี\n[3] ลาก่อน"])
    pipeline.translate_file(
        first, source, terms=[gandalf()], context_before=0, context_after=0,
        sleep=lambda seconds: None,
    )

    second = FakeClient(["[1] กันดาล์ฟพูด"])
    pipeline.translate_file(
        second, source, terms=[gandalf("กันดาล์ฟ")], context_before=0, context_after=0,
        sleep=lambda seconds: None,
    )

    assert second.call_count == 1
    prompt = second.calls[0][1]["content"]
    assert "[TRANSLATE]\n[1] Gandalf speaks." in prompt
    assert "Hello." not in prompt and "Goodbye." not in prompt
    assert "กันดาล์ฟ" in prompt


def test_unchanged_notes_still_hit_the_cache(tmp_path):
    from conftest import FakeClient

    source = make_source(tmp_path, "Gandalf speaks.", "Hello.")
    pipeline.translate_file(
        FakeClient(["[1] แกนดัล์ฟพูด\n[2] สวัสดี"]), source, terms=[gandalf()],
        sleep=lambda seconds: None,
    )

    second = FakeClient()
    result, _ = pipeline.translate_file(
        second, source, terms=[gandalf()], sleep=lambda seconds: None
    )
    assert second.call_count == 0
    assert all(entry.from_cache for entry in result.results)


def test_editing_the_bible_retranslates_every_line(tmp_path):
    from conftest import FakeClient

    source = make_source(tmp_path, "Hello.", "Goodbye.")
    pipeline.translate_file(
        FakeClient(["[1] สวัสดี\n[2] ลาก่อน"]), source,
        bible="Somsak is the father.", sleep=lambda seconds: None,
    )

    second = FakeClient(["[1] สวัสดีจ้ะ\n[2] ลาก่อนนะ"])
    pipeline.translate_file(
        second, source, bible="Somsak is the mother.", sleep=lambda seconds: None
    )
    assert second.call_count == 1
    prompt = second.calls[0][1]["content"]
    assert "[1] Hello." in prompt and "[2] Goodbye." in prompt
    assert "Somsak is the mother." in prompt


def test_estimate_counts_the_batch_glossary():
    loaded = srtio.load_srt(FIXTURES / "simple.srt")
    term = glossary.Term("Hello", "translate", "สวัสดี")
    without = pipeline.estimate(loaded.subtitles, size=1)
    with_term = pipeline.estimate(loaded.subtitles, size=1, terms=[term])

    expected = (
        len(glossary.for_prompt([term])) + pipeline.GLOSSARY_OVERHEAD_CHARS
    ) / pipeline.CHARS_PER_PROMPT_TOKEN
    assert with_term.prompt_tokens - without.prompt_tokens == pytest.approx(expected, abs=1)


def test_a_term_that_never_appears_changes_nothing_in_the_estimate():
    loaded = srtio.load_srt(FIXTURES / "simple.srt")
    absent = glossary.Term("Zeus", "keep")
    assert (
        pipeline.estimate(loaded.subtitles, terms=[absent]).prompt_tokens
        == pipeline.estimate(loaded.subtitles).prompt_tokens
    )


# --- --fix (step 20) --------------------------------------------------------


def test_fix_retranslates_only_the_flagged_entries(tmp_path):
    source = make_source(tmp_path, "Hello there.", "To the market.", "See you.")
    client = FakeClient(
        ["[1] สวัสดี\n[2] ไป market นะ\n[3] เจอกัน", "[2] ไปตลาดนะ"]
    )
    result, target = pipeline.translate_file(
        client, source, fix=True, context_before=0, context_after=0,
        sleep=lambda seconds: None,
    )

    assert client.call_count == 2
    fix_prompt = client.calls[1][1]["content"]
    assert "[TRANSLATE]\n[2] To the market." in fix_prompt
    assert "Hello there." not in fix_prompt
    assert "correction pass" in client.calls[1][0]["content"]
    assert result.fixed == [2]
    assert [s.content for s in srtio.load_srt(target).subtitles] == [
        "สวัสดี", "ไปตลาดนะ", "เจอกัน"
    ]


def test_fix_does_nothing_when_nothing_is_flagged(tmp_path):
    source = make_source(tmp_path, "Hello there.")
    client = FakeClient(["[1] สวัสดี"])
    result, _ = pipeline.translate_file(
        client, source, fix=True, sleep=lambda seconds: None
    )

    assert client.call_count == 1
    assert result.fixed == []


def test_a_failed_fix_keeps_the_earlier_translation(tmp_path):
    source = make_source(tmp_path, "To the market.")
    client = FakeClient(["[1] ไป market นะ", "[1] 你好"])
    result, target = pipeline.translate_file(
        client, source, fix=True, max_retries=0, sleep=lambda seconds: None
    )

    assert client.call_count == 2
    assert result.fixed == [1]
    assert result.results[0].text == "ไป market นะ"
    assert srtio.load_srt(target).subtitles[0].content == "ไป market นะ"


def test_the_fix_pass_sees_the_first_pass_as_context(tmp_path):
    source = make_source(tmp_path, "Hello there.", "To the market.", "See you.")
    client = FakeClient(
        ["[1] สวัสดี\n[2] ไป market นะ\n[3] เจอกัน", "[2] ไปตลาดนะ"]
    )
    pipeline.translate_file(
        client, source, fix=True, context_before=1, context_after=1,
        sleep=lambda seconds: None,
    )

    fix_prompt = client.calls[1][1]["content"]
    # Before-context carries the first pass's Thai; after-context stays English.
    assert "TH: สวัสดี" in fix_prompt
    assert "[3] See you." in fix_prompt
    assert "TH: เจอกัน" not in fix_prompt


def test_fixed_text_is_cached(tmp_path):
    source = make_source(tmp_path, "To the market.")
    pipeline.translate_file(
        FakeClient(["[1] ไป market นะ", "[1] ไปตลาดนะ"]),
        source,
        fix=True,
        sleep=lambda seconds: None,
    )

    # Nothing queued: the fixed line must come back from the cache, and a clean
    # file leaves nothing for a second fix pass to do.
    second = FakeClient()
    result, target = pipeline.translate_file(
        second, source, fix=True, sleep=lambda seconds: None
    )

    assert second.call_count == 0
    assert result.fixed == []
    assert srtio.load_srt(target).subtitles[0].content == "ไปตลาดนะ"


def test_fix_works_without_a_cache(tmp_path):
    source = make_source(tmp_path, "To the market.")
    client = FakeClient(["[1] ไป market นะ", "[1] ไปตลาดนะ"])
    result, target = pipeline.translate_file(
        client, source, fix=True, use_cache=False, sleep=lambda seconds: None
    )

    assert client.call_count == 2
    assert result.fixed == [1]
    assert srtio.load_srt(target).subtitles[0].content == "ไปตลาดนะ"


def test_ignore_cache_requests_a_cached_entry_again(tmp_path):
    source = make_source(tmp_path, "Hello there.")
    pipeline.translate_file(
        FakeClient(["[1] สวัสดี"]), source, sleep=lambda seconds: None
    )

    second = FakeClient(["[1] สวัสดีครับ"])
    result, _ = pipeline.translate_file(
        second, source, ignore_cache=True, sleep=lambda seconds: None
    )

    assert second.call_count == 1
    assert result.results[0].text == "สวัสดีครับ"
    assert result.results[0].from_cache is False


def test_seed_is_used_as_context_only():
    subtitles = [subtitle(1, "Hello."), subtitle(2, "Bye.", start_s=3)]
    client = FakeClient(["[2] ลาก่อน"])
    result = pipeline.translate_subtitles(
        client,
        subtitles,
        indexes={2},
        ignore_cache=True,
        seed={1: "สวัสดี"},
        context_before=1,
        context_after=0,
        sleep=lambda seconds: None,
    )

    prompt = client.calls[0][1]["content"]
    assert "[TRANSLATE]\n[2] Bye." in prompt
    assert "TH: สวัสดี" in prompt
    assert [entry.index for entry in result.results] == [2]
    assert result.results[0].from_cache is False


def test_the_fix_pass_does_not_seed_english_as_context(tmp_path):
    source = make_source(tmp_path, "Hello there.", "To the market.")
    # Entry 1 fails (Chinese, no retries), entry 2 is flagged for English.
    client = FakeClient(["[1] 你好\n[2] ไป market นะ", "[2] ไปตลาดนะ"])
    pipeline.translate_file(
        client, source, fix=True, max_retries=0, context_before=1, context_after=0,
        sleep=lambda seconds: None,
    )

    fix_prompt = client.calls[1][1]["content"]
    assert "[1] Hello there." in fix_prompt
    assert "TH: Hello there." not in fix_prompt


# --- --review (step 21) -----------------------------------------------------


FIRST_PASS = "[1] สวัสดี\n[2] ไป market นะ\n[3] เจอกัน"


def test_review_suggests_without_changing_the_file(tmp_path):
    source = make_source(tmp_path, "Hello there.", "To the market.", "See you.")
    client = FakeClient([FIRST_PASS, "[2] ไปตลาดนะ"])
    result, target = pipeline.translate_file(
        client, source, review=True, context_before=0, context_after=0,
        sleep=lambda seconds: None,
    )

    assert client.call_count == 2
    prompt = client.calls[1][1]["content"]
    assert "[2] ไป market นะ" in prompt  # the current translation is shown
    assert "[TRANSLATE]\n[2] To the market." in prompt
    assert "Hello there." not in prompt  # only the flagged entry is sent
    assert "review pass" in client.calls[1][0]["content"]
    assert result.reviewed == [2]
    assert result.suggestions == {2: "ไปตลาดนะ"}
    assert [s.content for s in srtio.load_srt(target).subtitles][1] == "ไป market นะ"


def test_review_suggestions_are_not_cached(tmp_path):
    source = make_source(tmp_path, "To the market.")
    pipeline.translate_file(
        FakeClient(["[1] ไป market นะ", "[1] ไปตลาดนะ"]),
        source,
        review=True,
        sleep=lambda seconds: None,
    )

    # Advice is not a translation of record: a later run without --review keeps
    # what the cache holds, and makes no call.
    second = FakeClient()
    result, _ = pipeline.translate_file(second, source, sleep=lambda seconds: None)
    assert second.call_count == 0
    assert result.results[0].text == "ไป market นะ"


def test_a_review_that_repeats_the_line_is_not_a_suggestion(tmp_path):
    source = make_source(tmp_path, "To the market.")
    client = FakeClient(["[1] ไป market นะ", "[1] ไป market นะ"])
    result, _ = pipeline.translate_file(
        client, source, review=True, sleep=lambda seconds: None
    )
    assert result.reviewed == [1]
    assert result.suggestions == {}


def test_review_does_nothing_when_nothing_is_flagged(tmp_path):
    source = make_source(tmp_path, "Hello there.")
    client = FakeClient(["[1] สวัสดี"])
    result, _ = pipeline.translate_file(
        client, source, review=True, sleep=lambda seconds: None
    )
    assert client.call_count == 1
    assert result.reviewed == []
    assert result.suggestions == {}


def test_a_failed_review_reply_gives_no_suggestion(tmp_path):
    source = make_source(tmp_path, "To the market.")
    client = FakeClient(["[1] ไป market นะ", "[1] 你好"])
    result, target = pipeline.translate_file(
        client, source, review=True, max_retries=0, sleep=lambda seconds: None
    )
    assert client.call_count == 2
    assert result.suggestions == {}
    assert srtio.load_srt(target).subtitles[0].content == "ไป market นะ"


def test_fix_runs_before_review(tmp_path):
    source = make_source(tmp_path, "To the market.")
    client = FakeClient(["[1] ไป market นะ", "[1] ไปตลาดนะ"])
    result, _ = pipeline.translate_file(
        client, source, fix=True, review=True, sleep=lambda seconds: None
    )

    # The fix cleaned the only flagged line, so review had nothing to do.
    assert client.call_count == 2
    assert result.fixed == [1]
    assert result.reviewed == []
    assert result.suggestions == {}


def test_review_sees_the_lines_fix_could_not_repair(tmp_path):
    source = make_source(tmp_path, "To the market.")
    client = FakeClient(["[1] ไป market นะ", "[1] 你好", "[1] ไปตลาดนะ"])
    result, target = pipeline.translate_file(
        client, source, fix=True, review=True, max_retries=0, sleep=lambda seconds: None
    )

    assert client.call_count == 3
    assert result.fixed == [1]
    assert result.reviewed == [1]
    assert result.suggestions == {1: "ไปตลาดนะ"}
    # The failed fix kept the earlier text; review only advises.
    assert srtio.load_srt(target).subtitles[0].content == "ไป market นะ"


def test_review_works_without_a_cache(tmp_path):
    source = make_source(tmp_path, "To the market.")
    client = FakeClient(["[1] ไป market นะ", "[1] ไปตลาดนะ"])
    result, _ = pipeline.translate_file(
        client, source, review=True, use_cache=False, sleep=lambda seconds: None
    )
    assert result.suggestions == {1: "ไปตลาดนะ"}
