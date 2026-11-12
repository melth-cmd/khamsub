"""--repair at the command-line boundary: names, backup, write, pre-flight."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
import srt

import config
import srtio
import translate


def repair_pair(
    tmp_path: Path,
    thai_name: str = "movie.th.srt",
    source_name: str = "movie.en.srt",
    thai_text: str = "  สวัสดี  ",
    source_text: str = "Hello.",
) -> tuple[Path, Path]:
    thai = tmp_path / thai_name
    source = tmp_path / source_name
    start, end = dt.timedelta(seconds=1), dt.timedelta(seconds=2)
    srtio.write_srt(
        thai, [srt.Subtitle(index=1, start=start, end=end, content=thai_text)]
    )
    srtio.write_srt(
        source, [srt.Subtitle(index=1, start=start, end=end, content=source_text)]
    )
    return thai, source


def test_repair_writes_in_place_and_keeps_a_backup(tmp_path, capsys):
    thai, _ = repair_pair(tmp_path)
    original = thai.read_bytes()

    status = translate.main(["--repair", str(thai)])

    assert status == 0
    backup = Path(str(thai) + ".bak")
    assert backup.read_bytes() == original
    reloaded = srtio.load_srt(thai)
    assert reloaded.subtitles[0].content == "สวัสดี"
    assert reloaded.subtitles[0].index == 1
    assert reloaded.subtitles[0].start == dt.timedelta(seconds=1)
    assert reloaded.subtitles[0].end == dt.timedelta(seconds=2)
    report = thai.with_suffix(".repair-report.txt")
    assert report.exists()
    assert "Repaired" in report.read_text(encoding="utf-8")
    assert "repaired; backup" in capsys.readouterr().out


def test_repair_needs_no_config_and_no_key(tmp_path, monkeypatch):
    thai, _ = repair_pair(tmp_path)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    def boom(*args, **kwargs):
        raise AssertionError("--repair must not load config or build a client")

    monkeypatch.setattr(config, "build_client", boom)
    monkeypatch.setattr(config, "load_settings", boom)

    assert translate.main(["--repair", str(thai)]) == 0


def test_repair_dry_run_writes_nothing(tmp_path, capsys):
    thai, _ = repair_pair(tmp_path)
    original = thai.read_bytes()

    status = translate.main(["--repair", str(thai), "--dry-run"])

    assert status == 0
    assert thai.read_bytes() == original
    assert not Path(str(thai) + ".bak").exists()
    assert not thai.with_suffix(".repair-report.txt").exists()
    out = capsys.readouterr().out
    assert "Would repair" in out
    assert "dry run; nothing written" in out


def test_repair_refuses_an_existing_backup(tmp_path, capsys):
    thai, _ = repair_pair(tmp_path)
    original = thai.read_bytes()
    backup = Path(str(thai) + ".bak")
    backup.write_text("old", encoding="utf-8")

    status = translate.main(["--repair", str(thai)])

    assert status == 2
    assert "pass --force" in capsys.readouterr().err
    assert thai.read_bytes() == original
    assert backup.read_text(encoding="utf-8") == "old"


def test_repair_force_overwrites_the_backup(tmp_path):
    thai, _ = repair_pair(tmp_path)
    original = thai.read_bytes()
    backup = Path(str(thai) + ".bak")
    backup.write_text("old", encoding="utf-8")

    assert translate.main(["--repair", str(thai), "--force"]) == 0
    assert backup.read_bytes() == original


def test_repair_finds_the_hi_counterpart(tmp_path):
    thai, _ = repair_pair(tmp_path, "movie.th.hi.srt", "movie.en.hi.srt")

    assert translate.main(["--repair", str(thai)]) == 0
    assert thai.with_suffix(".repair-report.txt").exists()
    assert Path(str(thai) + ".bak").exists()


def test_repair_refuses_a_mixed_hi_pair(tmp_path, capsys):
    thai, source = repair_pair(tmp_path, "movie.th.hi.srt", "movie.en.srt")
    original = thai.read_bytes()

    status = translate.main(["--repair", str(thai), "--source", str(source)])

    assert status == 2
    assert "#21" in capsys.readouterr().err
    assert thai.read_bytes() == original


def test_repair_requires_the_counterpart(tmp_path, capsys):
    thai = tmp_path / "movie.th.srt"
    srtio.write_srt(
        thai,
        [
            srt.Subtitle(
                index=1,
                start=dt.timedelta(seconds=1),
                end=dt.timedelta(seconds=2),
                content="สวัสดี",
            )
        ],
    )

    status = translate.main(["--repair", str(thai)])

    assert status == 2
    err = capsys.readouterr().err
    assert "English source not found" in err
    assert "movie.en.srt" in err


def test_repair_refuses_a_file_without_thai(tmp_path, capsys):
    thai, _ = repair_pair(tmp_path, thai_text="Hello.")

    status = translate.main(["--repair", str(thai)])

    assert status == 2
    assert "no Thai text" in capsys.readouterr().err


@pytest.mark.parametrize(
    "extra",
    [
        ["--fix"],
        ["--review"],
        ["--bible-only"],
        ["--limit", "1"],
        ["--range", "1-2"],
        ["--output", "x.srt"],
        ["--config", "x.toml"],
        ["--where"],
        ["--init-config"],
    ],
)
def test_repair_rejects_translation_flags(tmp_path, extra):
    thai, _ = repair_pair(tmp_path)
    assert translate.main(["--repair", str(thai), *extra]) == 2


def test_source_is_only_for_repair(tmp_path, capsys):
    thai, source = repair_pair(tmp_path)
    status = translate.main(["--source", str(source), str(thai)])
    assert status == 2
    assert "--repair" in capsys.readouterr().err


def test_source_with_several_inputs_is_refused(tmp_path, capsys):
    one, source = repair_pair(tmp_path, "one.th.srt", "one.en.srt")
    two, _ = repair_pair(tmp_path, "two.th.srt", "two.en.srt")
    status = translate.main(["--repair", str(one), str(two), "--source", str(source)])
    assert status == 2
    assert "one input file" in capsys.readouterr().err


def test_repair_preflight_stops_everything(tmp_path, capsys):
    good, _ = repair_pair(tmp_path, "good.th.srt", "good.en.srt")
    bad, _ = repair_pair(
        tmp_path, "bad.th.srt", "bad.en.srt", thai_text="No Thai here."
    )
    good_before = good.read_bytes()

    status = translate.main(["--repair", str(good), str(bad)])

    assert status == 2
    assert good.read_bytes() == good_before
    assert not Path(str(good) + ".bak").exists()


def test_repair_reports_problems_but_exits_zero(tmp_path, capsys):
    thai, _ = repair_pair(tmp_path, thai_text="สวัสดี Dad")

    status = translate.main(["--repair", str(thai)])

    assert status == 0
    out = capsys.readouterr().out
    assert "Needs a look" in out
    assert "english_left" in out
    assert "Retranslation candidates: 1" in out


def test_repair_duplicate_input_is_refused(tmp_path, capsys):
    thai, _ = repair_pair(tmp_path)
    status = translate.main(["--repair", str(thai), str(thai)])
    assert status == 2
    assert "listed twice" in capsys.readouterr().err


def test_repair_missing_input_is_refused(tmp_path, capsys):
    status = translate.main(["--repair", str(tmp_path / "ghost.th.srt")])
    assert status == 2
    assert "not found" in capsys.readouterr().err


def test_repair_directory_is_refused(tmp_path, capsys):
    status = translate.main(["--repair", str(tmp_path)])
    assert status == 2
    assert "is a directory" in capsys.readouterr().err


def test_repair_refuses_hi_partial_for_now(tmp_path, capsys):
    thai, _ = repair_pair(tmp_path, "movie.th.hi_partial.srt", "movie.en.hi_partial.srt")
    status = translate.main(["--repair", str(thai)])
    assert status == 2
    assert "#21" in capsys.readouterr().err


def test_repair_dry_run_works_with_an_existing_backup(tmp_path):
    thai, _ = repair_pair(tmp_path)
    Path(str(thai) + ".bak").write_text("old", encoding="utf-8")
    assert translate.main(["--repair", str(thai), "--dry-run"]) == 0
