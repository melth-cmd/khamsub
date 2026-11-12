from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
import srt

import srtio

FIXTURES = Path(__file__).parent / "fixtures"


def make_subtitle(index: int, text: str, start_s: int = 1) -> srt.Subtitle:
    start = dt.timedelta(seconds=start_s)
    return srt.Subtitle(index=index, start=start, end=start + dt.timedelta(seconds=2), content=text)


# --- decode_bytes -----------------------------------------------------------


def test_decode_bytes_strips_bom():
    text, encoding = srtio.decode_bytes(b"\xef\xbb\xbfHello\n")
    assert encoding == "utf-8-sig"
    assert text == "Hello\n"


def test_decode_bytes_plain_utf8():
    text, encoding = srtio.decode_bytes("สวัสดี\n".encode("utf-8"))
    assert encoding == "utf-8-sig"
    assert text == "สวัสดี\n"


def test_decode_bytes_falls_back_when_detection_disabled(monkeypatch):
    monkeypatch.setattr(srtio, "_detect_encoding", lambda data: None)
    raw = "café\n".encode("cp1252")
    text, encoding = srtio.decode_bytes(raw, encodings=("cp1252",))
    assert encoding == "cp1252"
    assert text == "café\n"


def test_decode_bytes_uses_detection_when_available():
    raw = (FIXTURES / "windows.srt").read_bytes()
    text, encoding = srtio.decode_bytes(raw)
    # The exact label varies by detector version; the text must survive intact.
    assert encoding != "utf-8-sig"
    assert "café" in text


def test_decode_bytes_ignores_unusable_detection(monkeypatch):
    monkeypatch.setattr(srtio, "_detect_encoding", lambda data: "not-a-codec")
    text, _ = srtio.decode_bytes(b"plain ascii", encodings=("utf-8",))
    assert text == "plain ascii"


def test_decode_bytes_raises_when_nothing_fits(monkeypatch):
    monkeypatch.setattr(srtio, "_detect_encoding", lambda data: None)
    with pytest.raises(UnicodeDecodeError):
        srtio.decode_bytes("café".encode("cp1252"), encodings=("ascii",))


# --- read_text --------------------------------------------------------------


def test_read_text_returns_text_and_encoding():
    text, encoding = srtio.read_text(FIXTURES / "simple.srt")
    assert encoding == "utf-8-sig"
    assert text.startswith("1\n00:00:01,000")


def test_read_text_handles_bom_fixture():
    text, _ = srtio.read_text(FIXTURES / "bom.srt")
    assert text.startswith("1\n")
    assert "\ufeff" not in text


# --- parse_subtitles --------------------------------------------------------


def test_parse_subtitles_basic():
    text = (FIXTURES / "simple.srt").read_text(encoding="utf-8")
    subtitles, skipped = srtio.parse_subtitles(text)
    assert skipped == 0
    assert [s.index for s in subtitles] == [1, 2, 3]
    assert subtitles[0].content == "Hello there."
    assert subtitles[1].start == dt.timedelta(seconds=4)


def test_parse_subtitles_skips_malformed_block(caplog):
    text = (FIXTURES / "malformed.srt").read_text(encoding="utf-8")
    with caplog.at_level("WARNING"):
        subtitles, skipped = srtio.parse_subtitles(text)
    assert skipped == 1
    assert [s.content for s in subtitles] == ["Good line.", "Another good line."]
    assert "Skipped 1 malformed" in caplog.text


def test_parse_subtitles_empty_text():
    assert srtio.parse_subtitles("") == ([], 0)


# --- load_srt ---------------------------------------------------------------


def test_load_srt_end_to_end():
    loaded = srtio.load_srt(FIXTURES / "simple.srt")
    assert isinstance(loaded, srtio.SubtitleFile)
    assert len(loaded) == 3
    assert loaded.encoding == "utf-8-sig"
    assert loaded.skipped == 0


def test_load_srt_empty_file():
    loaded = srtio.load_srt(FIXTURES / "empty.srt")
    assert len(loaded) == 0
    assert loaded.skipped == 0


# --- compose_subtitles ------------------------------------------------------


def test_compose_preserves_indexes_and_timestamps():
    subtitles = [make_subtitle(7, "Hello there."), make_subtitle(9, "How are you?", start_s=5)]
    text = srtio.compose_subtitles(subtitles)
    reparsed = list(srt.parse(text))
    assert [s.index for s in reparsed] == [7, 9]
    assert reparsed[0].start == dt.timedelta(seconds=1)
    assert reparsed[1].end == dt.timedelta(seconds=7)


def test_compose_handles_empty_list():
    assert srtio.compose_subtitles([]) == ""


# --- write_srt --------------------------------------------------------------


def test_write_srt_utf8_without_bom(tmp_path):
    target = tmp_path / "out.srt"
    srtio.write_srt(target, [make_subtitle(1, "สวัสดี")])
    raw = target.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    assert "สวัสดี" in raw.decode("utf-8")


def test_write_srt_utf8_sig_has_bom(tmp_path):
    target = tmp_path / "out.srt"
    srtio.write_srt(target, [make_subtitle(1, "สวัสดี")], encoding="utf-8-sig")
    assert target.read_bytes().startswith(b"\xef\xbb\xbf")


def test_write_srt_creates_parent_directory(tmp_path):
    target = tmp_path / "nested" / "deeper" / "out.srt"
    srtio.write_srt(target, [make_subtitle(1, "Hi")])
    assert target.exists()


def test_write_srt_leaves_no_temp_files(tmp_path):
    srtio.write_srt(tmp_path / "out.srt", [make_subtitle(1, "Hi")])
    assert [p.name for p in tmp_path.iterdir()] == ["out.srt"]


def test_write_text_writes_utf8(tmp_path):
    target = srtio.write_text(tmp_path / "report.txt", "สวัสดี\n")
    assert target == tmp_path / "report.txt"
    assert target.read_text(encoding="utf-8") == "สวัสดี\n"


def test_write_text_creates_parent_directory(tmp_path):
    target = tmp_path / "nested" / "report.txt"
    srtio.write_text(target, "hi")
    assert target.exists()


def test_write_text_replaces_an_existing_file(tmp_path):
    target = tmp_path / "report.txt"
    srtio.write_text(target, "old")
    srtio.write_text(target, "new")
    assert target.read_text(encoding="utf-8") == "new"


def test_write_text_leaves_no_temp_files(tmp_path):
    srtio.write_text(tmp_path / "report.txt", "hi")
    assert [p.name for p in tmp_path.iterdir()] == ["report.txt"]


def test_write_srt_roundtrip(tmp_path):
    original = srtio.load_srt(FIXTURES / "simple.srt")
    target = tmp_path / "out.srt"
    srtio.write_srt(target, original.subtitles)
    reloaded = srtio.load_srt(target)
    assert [s.content for s in reloaded.subtitles] == [
        s.content for s in original.subtitles
    ]
    assert [s.start for s in reloaded.subtitles] == [
        s.start for s in original.subtitles
    ]


# --- --repair source names -------------------------------------------------


def test_english_counterpart_maps_the_basic_pair():
    assert srtio.english_counterpart("Movie.th.srt") == Path("Movie.en.srt")


def test_english_counterpart_keeps_the_hi_and_partial_suffixes():
    assert srtio.english_counterpart("Show.S01E01.th.hi.srt") == Path(
        "Show.S01E01.en.hi.srt"
    )
    assert srtio.english_counterpart("Show.th.hi_partial.srt") == Path(
        "Show.en.hi_partial.srt"
    )


def test_english_counterpart_needs_a_th_token():
    assert srtio.english_counterpart("Movie.srt") is None


def test_language_tail_distinguishes_hi_partial_and_plain():
    assert srtio.language_tail("Movie.th.srt") == ""
    assert srtio.language_tail("Movie.en.srt") == ""
    assert srtio.language_tail("Movie.th.hi.srt") == "hi"
    assert srtio.language_tail("Movie.en.hi.srt") == "hi"
    assert srtio.language_tail("Movie.th.hi_partial.srt") == "hi_partial"
    assert srtio.language_tail("Movie.srt") == ""
