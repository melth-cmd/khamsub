from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest

import bible
import config
import glossary
import srtio
import translate
import verify
from conftest import FakeClient

FIXTURES = Path(__file__).parent / "fixtures"
REPLIES = "[1] สวัสดี\n[2] สบายดีไหม\n[3] {0}สบายดี{1}"


@pytest.fixture
def project(tmp_path) -> Path:
    shutil.copy(FIXTURES / "simple.srt", tmp_path / "sample.en.srt")
    (tmp_path / "config.toml").write_text('[llm]\nmodel = "test/model"\n', encoding="utf-8")
    return tmp_path


@pytest.fixture
def fake(monkeypatch) -> FakeClient:
    client = FakeClient()
    monkeypatch.setattr(config, "build_client", lambda settings, api_key=None: client)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    return client


def test_translates_one_file(project, fake, capsys):
    fake.queue(REPLIES)
    status = translate.main(
        [str(project / "sample.en.srt"), "--config", str(project / "config.toml")]
    )

    assert status == 0
    output = project / "sample.th.srt"
    assert output.exists()
    assert srtio.load_srt(output).subtitles[0].content == "สวัสดี"
    assert "sample.en.srt -> sample.th.srt" in capsys.readouterr().out


def test_missing_config_is_an_error(project, capsys):
    status = translate.main([str(project / "sample.en.srt"), "--config", str(project / "nope.toml")])
    assert status == 2
    assert "config error" in capsys.readouterr().err


def test_missing_input_file_is_an_error(project, fake, capsys):
    status = translate.main(
        [str(project / "ghost.srt"), "--config", str(project / "config.toml")]
    )
    assert status == 2
    assert "not found" in capsys.readouterr().err


def test_missing_api_key_is_an_error(project, monkeypatch, capsys):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(config, "OpenRouterClient", None, raising=False)
    status = translate.main(
        [str(project / "sample.en.srt"), "--config", str(project / "config.toml")]
    )
    assert status == 2
    assert "OPENROUTER_API_KEY" in capsys.readouterr().err


def test_multiple_files(project, fake):
    shutil.copy(FIXTURES / "simple.srt", project / "second.en.srt")
    fake.queue(REPLIES).queue(REPLIES)
    status = translate.main(
        [
            str(project / "sample.en.srt"),
            str(project / "second.en.srt"),
            "--config",
            str(project / "config.toml"),
        ]
    )
    assert status == 0
    assert (project / "sample.th.srt").exists()
    assert (project / "second.th.srt").exists()


def test_output_flag_requires_a_single_file(project, fake, capsys):
    status = translate.main(
        [
            str(project / "sample.en.srt"),
            str(project / "sample.en.srt"),
            "--output",
            str(project / "x.srt"),
            "--config",
            str(project / "config.toml"),
        ]
    )
    assert status == 2
    assert "--output" in capsys.readouterr().err


def test_output_flag_writes_where_asked(project, fake):
    target = project / "custom.srt"
    fake.queue(REPLIES)
    status = translate.main(
        [
            str(project / "sample.en.srt"),
            "--output",
            str(target),
            "--config",
            str(project / "config.toml"),
        ]
    )
    assert status == 0
    assert target.exists()


def test_failed_entries_are_reported_but_run_succeeds(project, fake, capsys):
    fake.queue("[1] สวัสดี\n[2] 你好\n[3] {0}สบายดี{1}")  # entry 2 leaks Chinese
    fake.queue("[2] 你好")
    fake.queue("[2] 你好")
    fake.queue("[2] 你好")
    status = translate.main(
        [str(project / "sample.en.srt"), "--config", str(project / "config.toml")]
    )
    captured = capsys.readouterr()
    assert status == 0
    assert "FAILED" in captured.out
    assert "left in English" in captured.err


def test_config_controls_output_naming(project, fake):
    (project / "config.toml").write_text(
        '[llm]\nmodel = "test/model"\n[output]\nstrip_en_suffix = false\n', encoding="utf-8"
    )
    fake.queue(REPLIES)
    translate.main([str(project / "sample.en.srt"), "--config", str(project / "config.toml")])
    assert (project / "sample.en.th.srt").exists()


def test_verbose_flag_logs_batches(project, fake, caplog):
    fake.queue(REPLIES)
    translate.main(
        [str(project / "sample.en.srt"), "--config", str(project / "config.toml"), "-v"]
    )


# --- multi-file batch processing (step 14) -----------------------------------


def copy(project: Path, name: str) -> Path:
    shutil.copy(FIXTURES / "simple.srt", project / name)
    return project / name


def run(project, fake, *args) -> int:
    return translate.main([*map(str, args), "--config", str(project / "config.toml")])


def test_colliding_outputs_are_refused_before_any_call(project, fake, capsys):
    status = run(project, fake, copy(project, "movie.en.srt"), copy(project, "movie.srt"))
    err = capsys.readouterr().err
    assert status == 2
    assert "would both write movie.th.srt" in err
    assert fake.call_count == 0
    assert not (project / "movie.th.srt").exists()


def test_duplicate_input_is_refused(project, fake, capsys):
    source = copy(project, "sample.en.srt")
    status = run(project, fake, source, source)
    assert status == 2
    assert "given more than once" in capsys.readouterr().err
    assert fake.call_count == 0


def test_duplicate_detected_through_a_symlink(project, fake, capsys):
    source = copy(project, "sample.en.srt")
    alias = project / "alias.en.srt"
    alias.symlink_to(source)
    status = run(project, fake, source, alias)
    assert status == 2
    assert "given more than once" in capsys.readouterr().err


def test_existing_translation_is_refused(project, fake, capsys):
    already = copy(project, "done.th.srt")
    status = run(project, fake, already)
    err = capsys.readouterr().err
    assert status == 2
    assert "looks like an existing translation" in err
    assert "--force" in err
    assert fake.call_count == 0


def test_force_accepts_an_existing_translation(project, fake):
    already = copy(project, "done.th.srt")
    fake.queue(REPLIES)
    status = translate.main(
        [str(already), "--force", "--config", str(project / "config.toml")]
    )
    assert status == 0
    assert fake.call_count == 1
    assert (project / "done.th.th.srt").exists()


def test_a_directory_is_not_an_input_file(project, fake, capsys):
    folder = project / "subs"
    folder.mkdir()
    status = run(project, fake, folder)
    assert status == 2
    assert "is not a file" in capsys.readouterr().err


def test_every_problem_is_reported_at_once(project, fake, capsys):
    folder = project / "subs"
    folder.mkdir()
    status = run(project, fake, folder, project / "ghost.srt", copy(project, "done.th.srt"))
    err = capsys.readouterr().err
    assert status == 2
    assert "is not a file" in err
    assert "not found" in err
    assert "looks like an existing translation" in err


def test_one_bad_file_aborts_the_whole_run(project, fake):
    fake.queue(REPLIES)
    status = run(project, fake, copy(project, "sample.en.srt"), project / "ghost.srt")
    assert status == 2
    assert fake.call_count == 0
    assert not (project / "sample.th.srt").exists()


def test_output_flag_may_not_overwrite_its_input(project, fake, capsys):
    source = copy(project, "sample.en.srt")
    status = translate.main(
        [str(source), "--output", str(source), "--config", str(project / "config.toml")]
    )
    assert status == 2
    assert "overwritten by its own output" in capsys.readouterr().err


def test_dry_run_checks_the_inputs_too(project, fake, capsys):
    status = translate.main(
        [str(copy(project, "done.th.srt")), "--dry-run", "--config", str(project / "config.toml")]
    )
    assert status == 2
    assert "looks like an existing translation" in capsys.readouterr().err


def test_multiple_files_get_progress_headers_and_a_total(project, fake, capsys):
    fake.queue(REPLIES).queue(REPLIES)
    status = run(project, fake, copy(project, "a.en.srt"), copy(project, "b.en.srt"))
    captured = capsys.readouterr()

    assert status == 0
    assert "[1/2] a.en.srt" in captured.err
    assert "[2/2] b.en.srt" in captured.err
    assert "total: 2 files  2 translated  6 entries  0 tokens" in captured.out


def test_single_file_gets_no_header_or_total(project, fake, capsys):
    fake.queue(REPLIES)
    run(project, fake, copy(project, "sample.en.srt"))
    captured = capsys.readouterr()
    assert "[1/1]" not in captured.err
    assert "total:" not in captured.out


def test_a_crashed_file_does_not_stop_the_rest(project, fake, capsys):
    import llm

    fake.queue(llm.LLMError("gateway exploded")).queue(REPLIES)
    status = run(project, fake, copy(project, "a.en.srt"), copy(project, "b.en.srt"))
    captured = capsys.readouterr()

    assert status == 1
    assert "a.en.srt: gateway exploded" in captured.err
    assert (project / "b.th.srt").exists()
    assert "1 FAILED" in captured.out


def test_leftover_english_does_not_change_the_exit_status(project, fake, capsys):
    fake.queue("[1] สวัสดี\n[2] 你好\n[3] {0}สบายดี{1}")
    for _ in range(3):
        fake.queue("[2] 你好")  # entry 2 never recovers
    fake.queue(REPLIES).queue(REPLIES)  # the retry loop and the second file
    status = run(project, fake, copy(project, "a.en.srt"), copy(project, "b.en.srt"))
    assert status == 0
    assert "total: 2 files  2 translated  6 entries  1 left in English  0 tokens" in capsys.readouterr().out


def test_limit_with_several_files_warns(project, fake, capsys):
    fake.queue("[1] สวัสดี").queue("[1] สวัสดี")
    run(project, fake, copy(project, "a.en.srt"), copy(project, "b.en.srt"), "--limit", "1")
    assert "--limit/--range apply to every file" in capsys.readouterr().err


# --- context (step 9) -------------------------------------------------------


def test_context_counts_come_from_config(project, fake):
    (project / "config.toml").write_text(
        '[llm]\nmodel = "test/model"\n[batch]\nsize = 1\ncontext_before = 1\ncontext_after = 1\n',
        encoding="utf-8",
    )
    fake.queue("[1] สวัสดี").queue("[2] สบายดีไหม").queue("[3] {0}สบายดี{1}")
    status = translate.main(
        [str(project / "sample.en.srt"), "--config", str(project / "config.toml")]
    )
    assert status == 0
    assert fake.call_count == 3  # size = 1

    first = fake.calls[0][1]["content"]
    second = fake.calls[1][1]["content"]

    assert "[CONTEXT BEFORE - do not translate]" not in first  # nothing behind it
    assert "[CONTEXT BEFORE - do not translate]\n[1] Hello there.\nTH: สวัสดี" in second

    after_block = second.split("[CONTEXT AFTER - do not translate]")[1].split("[TRANSLATE]")[0]
    assert "Fine, thanks." in after_block  # next line, English only
    assert "TH:" not in after_block


def test_context_is_not_part_of_the_output(project, fake):
    (project / "config.toml").write_text(
        '[llm]\nmodel = "test/model"\n[batch]\nsize = 1\ncontext_before = 2\n',
        encoding="utf-8",
    )
    fake.queue("[1] สวัสดี").queue("[2] สบายดีไหม").queue("[3] {0}สบายดี{1}")
    translate.main([str(project / "sample.en.srt"), "--config", str(project / "config.toml")])
    output = srtio.load_srt(project / "sample.th.srt")
    assert [s.index for s in output.subtitles] == [1, 2, 3]
    assert [s.content for s in output.subtitles] == ["สวัสดี", "สบายดีไหม", "<i>สบายดี</i>"]


# --- limit / range / dry-run (step 10) --------------------------------------


def test_limit_translates_only_the_first_n(project, fake):
    fake.queue("[1] สวัสดี")
    status = translate.main(
        [str(project / "sample.en.srt"), "--config", str(project / "config.toml"), "--limit", "1"]
    )
    assert status == 0
    assert fake.call_count == 1
    output = srtio.load_srt(project / "sample.th.srt")
    assert [s.index for s in output.subtitles] == [1]


def test_range_writes_only_that_slice(project, fake):
    fake.queue("[2] สบายดีไหม")
    status = translate.main(
        [str(project / "sample.en.srt"), "--config", str(project / "config.toml"),
         "--range", "2-2"]
    )
    assert status == 0
    assert fake.call_count == 1
    output = srtio.load_srt(project / "sample.th.srt")
    assert [s.index for s in output.subtitles] == [2]
    assert output.subtitles[0].content == "สบายดีไหม"


def test_range_still_sees_context_outside_the_slice(project, fake):
    fake.queue("[2] สบายดีไหม")
    translate.main(
        [str(project / "sample.en.srt"), "--config", str(project / "config.toml"),
         "--range", "2-2"]
    )
    prompt = fake.calls[0][1]["content"]
    assert "[1] Hello there." in prompt      # before
    assert "Fine, thanks." in prompt          # after


def test_empty_range_writes_an_empty_file(project, fake):
    status = translate.main(
        [str(project / "sample.en.srt"), "--config", str(project / "config.toml"),
         "--range", "90-99"]
    )
    assert status == 0
    assert fake.call_count == 0


def test_bad_range_is_rejected(project, fake, capsys):
    status = translate.main(
        [str(project / "sample.en.srt"), "--config", str(project / "config.toml"),
         "--range", "9-2"]
    )
    assert status == 2
    assert "after its end" in capsys.readouterr().err


def test_negative_limit_is_rejected(project, fake, capsys):
    status = translate.main(
        [str(project / "sample.en.srt"), "--config", str(project / "config.toml"),
         "--limit", "-1"]
    )
    assert status == 2
    assert "negative" in capsys.readouterr().err


def test_dry_run_says_one_entry_for_one_entry(project, fake, capsys):
    shutil.copy(FIXTURES / "simple.srt", project / "one.en.srt")
    translate.main(
        [str(project / "one.en.srt"), "--limit", "1", "--dry-run",
         "--config", str(project / "config.toml")]
    )
    out = capsys.readouterr().out
    assert "1 entry in 1 batch" in out
    assert "1 entries" not in out


def test_dry_run_needs_no_key_and_builds_no_client(project, monkeypatch, capsys):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    def boom(*args, **kwargs):
        raise AssertionError("dry run must not build a client")

    monkeypatch.setattr(config, "build_client", boom)
    status = translate.main(
        [str(project / "sample.en.srt"), "--config", str(project / "config.toml"), "--dry-run"]
    )
    out = capsys.readouterr().out
    assert status == 0
    assert "3 entries in 1 batch" in out  # default size 60 fits the whole file
    assert "tokens" in out
    assert "cost unknown" in out  # no prices in the test config


# --- story bible pre-pass (step 15) -----------------------------------------

BIBLE_REPLY = json.dumps(
    {
        "summary": "Somsak runs a garage; Mai comes home from school.",
        "notes": "Bangkok, present day.",
        "characters": [
            {
                "name": "Somsak",
                "gender": "male",
                "age": "40s",
                "role": "owns a garage",
                "relationships": "Mai's father",
                "register": "ผม + ครับ with customers",
            }
        ],
        "terms": [{"en": "Bangkok", "action": "transliterate", "th": "กรุงเทพ"}],
    },
    ensure_ascii=False,
)


def test_bible_only_writes_both_files_and_stops(project, fake, capsys):
    fake.queue(BIBLE_REPLY)
    status = run(project, fake, copy(project, "sample.en.srt"), "--bible-only")
    captured = capsys.readouterr()

    assert status == 0
    assert fake.call_count == 1
    assert not (project / "sample.th.srt").exists()
    assert "Somsak" in (project / "sample.bible.md").read_text(encoding="utf-8")
    assert glossary.load(project / "sample.glossary.toml") == [
        glossary.Term("Bangkok", "transliterate", "กรุงเทพ")
    ]
    assert "sample.en.srt -> sample.bible.md, sample.glossary.toml" in captured.out
    assert "1 character  1 term" in captured.out
    assert "then translate without --bible-only" in captured.err


def test_bible_only_refuses_to_overwrite_hand_edits(project, fake, capsys):
    path = project / "sample.bible.md"
    path.write_text("my own notes", encoding="utf-8")
    status = run(project, fake, copy(project, "sample.en.srt"), "--bible-only")
    err = capsys.readouterr().err

    assert status == 2
    assert "already exists" in err and "--force" in err
    assert fake.call_count == 0
    assert path.read_text(encoding="utf-8") == "my own notes"
    assert not (project / "sample.glossary.toml").exists()


def test_force_rebuilds_the_bible(project, fake):
    (project / "sample.glossary.toml").write_text("# old", encoding="utf-8")
    fake.queue(BIBLE_REPLY)
    status = run(project, fake, copy(project, "sample.en.srt"), "--bible-only", "--force")

    assert status == 0
    assert (project / "sample.bible.md").exists()
    assert glossary.load(project / "sample.glossary.toml")


def test_bible_only_reports_every_existing_file_before_any_call(project, fake, capsys):
    (project / "a.bible.md").write_text("mine", encoding="utf-8")
    (project / "b.glossary.toml").write_text("# mine", encoding="utf-8")
    status = run(project, fake, copy(project, "a.en.srt"), copy(project, "b.en.srt"), "--bible-only")
    err = capsys.readouterr().err

    assert status == 2
    assert "a.bible.md already exists" in err
    assert "b.glossary.toml already exists" in err
    assert fake.call_count == 0


def test_bible_only_reads_only_the_selected_range(project, fake):
    fake.queue(BIBLE_REPLY)
    status = run(
        project, fake, copy(project, "sample.en.srt"), "--bible-only", "--range", "1-1"
    )

    assert status == 0
    script = fake.calls[0][1]["content"]
    assert "Hello there." in script
    assert "How are you?" not in script


def test_bible_only_with_several_files(project, fake, capsys):
    fake.queue(BIBLE_REPLY).queue(BIBLE_REPLY)
    status = run(project, fake, copy(project, "a.en.srt"), copy(project, "b.en.srt"), "--bible-only")
    captured = capsys.readouterr()

    assert status == 0
    assert "[1/2] a.en.srt" in captured.err and "[2/2] b.en.srt" in captured.err
    for name in ("a", "b"):
        assert (project / f"{name}.bible.md").exists()
        assert (project / f"{name}.glossary.toml").exists()


def test_a_failed_pre_pass_writes_nothing(project, fake, capsys):
    fake.queue("I would rather not.").queue("Still prose.")
    status = run(project, fake, copy(project, "sample.en.srt"), "--bible-only")
    err = capsys.readouterr().err

    assert status == 1
    assert "error: sample.en.srt" in err
    assert not (project / "sample.bible.md").exists()
    assert not (project / "sample.glossary.toml").exists()


def test_bible_only_rejects_output_flag(project, fake, capsys):
    status = translate.main(
        [
            str(copy(project, "sample.en.srt")),
            "--bible-only",
            "--output",
            str(project / "x.srt"),
            "--config",
            str(project / "config.toml"),
        ]
    )
    assert status == 2
    assert "--output" in capsys.readouterr().err
    assert fake.call_count == 0


def test_a_normal_run_does_not_build_a_bible(project, fake):
    fake.queue(REPLIES)
    status = run(project, fake, copy(project, "sample.en.srt"))

    assert status == 0
    assert fake.call_count == 1  # translation only, no pre-pass
    assert not (project / "sample.bible.md").exists()
    assert not (project / "sample.glossary.toml").exists()


def test_check_bible_targets_ignores_missing_inputs(project):
    assert translate.check_bible_targets([str(project / "ghost.srt")]) == []


def test_bible_paths_sit_beside_the_source(project):
    bible_path, glossary_path = translate.bible_paths(project / "Show.en.hi_partial.srt")
    assert bible_path.name == "Show.hi_partial.bible.md"
    assert glossary_path.name == "Show.hi_partial.glossary.toml"


# --- hand-editable files, reloaded each run (step 16) -----------------------

BIBLE_TEXT = "# Story bible: Sample\n\nSomsak uses ผม + ครับ with strangers.\n"


def write_bible(project: Path, text: str = BIBLE_TEXT, name: str = "sample.bible.md") -> Path:
    path = project / name
    path.write_text(text, encoding="utf-8")
    return path


def write_glossary(
    project: Path, *terms: glossary.Term, name: str = "sample.glossary.toml"
) -> Path:
    path = project / name
    path.write_text(glossary.render(terms), encoding="utf-8")
    return path


def test_the_bible_is_sent_with_the_request(project, fake, capsys):
    write_bible(project)
    fake.queue(REPLIES)
    status = run(project, fake, copy(project, "sample.en.srt"))

    assert status == 0
    prompt = fake.calls[0][1]["content"]
    assert "[STORY BIBLE]" in prompt
    assert "Somsak uses ผม + ครับ with strangers." in prompt
    assert f"bible {len(BIBLE_TEXT.strip()):,} chars" in capsys.readouterr().out


def test_a_hand_edit_applies_to_the_next_run(project, fake):
    write_bible(project, "Somsak is the father.\n")
    source = copy(project, "sample.en.srt")
    fake.queue(REPLIES)
    run(project, fake, source)
    assert "Somsak is the father." in fake.calls[0][1]["content"]

    write_bible(project, "Mai is the daughter.\n")  # the bible is part of every cache key
    fake.queue(REPLIES)
    run(project, fake, source)

    assert fake.call_count == 2
    assert "Mai is the daughter." in fake.calls[1][1]["content"]
    assert "Somsak is the father." not in fake.calls[1][1]["content"]


def test_a_missing_bible_gets_one_hint(project, fake, capsys):
    fake.queue(REPLIES)
    run(project, fake, copy(project, "sample.en.srt"))
    err = capsys.readouterr().err

    assert "hint: no story bible for sample.en.srt" in err
    assert "--bible-only" in err


def test_no_hint_when_every_file_has_a_bible(project, fake, capsys):
    write_bible(project)
    fake.queue(REPLIES)
    run(project, fake, copy(project, "sample.en.srt"))
    assert "hint:" not in capsys.readouterr().err


def test_the_hint_counts_missing_files(project, fake, capsys):
    fake.queue(REPLIES).queue(REPLIES)
    run(project, fake, copy(project, "a.en.srt"), copy(project, "b.en.srt"))
    assert "no story bible for 2 of 2 files" in capsys.readouterr().err


def test_the_glossary_is_loaded_with_its_terms(project, fake):
    write_bible(project)
    (project / "sample.glossary.toml").write_text(
        glossary.render([glossary.Term("NASA", "keep")]), encoding="utf-8"
    )
    notes, problems = translate.load_notes_for([str(copy(project, "sample.en.srt"))])

    assert problems == []
    assert [term.en for term in notes[next(iter(notes))].terms] == ["NASA"]


def test_a_broken_glossary_stops_the_run_before_any_call(project, fake, capsys):
    (project / "sample.glossary.toml").write_text(
        '[[term]]\nen = "NASA"\naction = "shout"\nth = "นาซา"\n', encoding="utf-8"
    )
    status = run(project, fake, copy(project, "sample.en.srt"))
    err = capsys.readouterr().err

    assert status == 2
    assert "sample.glossary.toml" in err
    assert "unknown action" in err
    assert fake.call_count == 0
    assert not (project / "sample.th.srt").exists()


def test_every_broken_glossary_is_reported_at_once(project, fake, capsys):
    broken = '[[term]]\nen = "NASA"\naction = "shout"\nth = "นาซา"\n'
    (project / "a.glossary.toml").write_text(broken, encoding="utf-8")
    (project / "b.glossary.toml").write_text(broken, encoding="utf-8")
    status = run(project, fake, copy(project, "a.en.srt"), copy(project, "b.en.srt"))
    err = capsys.readouterr().err

    assert status == 2
    assert "a.glossary.toml" in err and "b.glossary.toml" in err
    assert fake.call_count == 0


def test_a_broken_glossary_fails_a_dry_run_too(project, fake, capsys):
    (project / "sample.glossary.toml").write_text(
        '[[term]]\nen = "NASA"\naction = "shout"\nth = "นาซา"\n', encoding="utf-8"
    )
    status = run(project, fake, copy(project, "sample.en.srt"), "--dry-run")

    assert status == 2
    assert "unknown action" in capsys.readouterr().err
    assert fake.call_count == 0


def test_bible_only_force_replaces_a_broken_glossary(project, fake):
    (project / "sample.glossary.toml").write_text(
        '[[term]]\nen = "NASA"\naction = "shout"\nth = "นาซา"\n', encoding="utf-8"
    )
    fake.queue(BIBLE_REPLY)
    status = run(project, fake, copy(project, "sample.en.srt"), "--bible-only", "--force")

    assert status == 0  # the pre-pass does not read what it is about to replace
    assert glossary.load(project / "sample.glossary.toml")


def test_verbose_says_which_files_were_read(project, fake, caplog):
    write_bible(project)
    (project / "sample.glossary.toml").write_text(
        glossary.render([glossary.Term("NASA", "keep")]), encoding="utf-8"
    )
    fake.queue(REPLIES)
    with caplog.at_level("INFO"):
        run(project, fake, copy(project, "sample.en.srt"), "-v")

    assert "bible sample.bible.md" in caplog.text
    assert "sample.glossary.toml (1 term)" in caplog.text


def test_load_notes_for_skips_missing_inputs(project):
    notes, problems = translate.load_notes_for([str(project / "ghost.srt")])
    assert notes == {}
    assert problems == []


def test_load_notes_for_keys_notes_by_source(project):
    source = copy(project, "a.en.srt")
    write_bible(project, name="a.bible.md")
    notes, problems = translate.load_notes_for([str(source)])

    assert problems == []
    assert notes[source].has_bible


def test_missing_notes_hint():
    notes = {Path("a.en.srt"): bible.Notes(bible="Mai.")}
    assert translate.missing_notes_hint(notes) is None

    notes[Path("b.en.srt")] = bible.Notes()
    hint = translate.missing_notes_hint(notes)
    assert "b.en.srt" in hint and "--bible-only" in hint

    notes[Path("c.en.srt")] = bible.Notes()
    assert "2 of 3 files" in translate.missing_notes_hint(notes)


def _prompt_tokens(out: str) -> int:
    """The ~input-token number from a --dry-run line."""
    match = re.search(r"input\s+~([\d,]+) tokens", out)
    return int(match.group(1).replace(",", ""))


def test_dry_run_counts_the_bible(project, fake, capsys):
    source = copy(project, "sample.en.srt")
    run(project, fake, source, "--dry-run")
    without = _prompt_tokens(capsys.readouterr().out)

    write_bible(project, "Somsak is the father and Mai is his daughter. " * 40)
    run(project, fake, source, "--dry-run")
    with_bible = _prompt_tokens(capsys.readouterr().out)

    assert with_bible > without
    assert fake.call_count == 0


# --- glossary injection and cache keys (step 17) ----------------------------

NO_CONTEXT = '[llm]\nmodel = "test/model"\n[batch]\ncontext_before = 0\ncontext_after = 0\n'


def test_glossary_goes_only_with_the_batches_that_use_it(project, fake):
    (project / "config.toml").write_text(
        NO_CONTEXT.replace("[batch]", "[batch]\nsize = 1"), encoding="utf-8"
    )
    write_glossary(project, glossary.Term("Hello", "translate", "สวัสดี"))
    fake.queue("[1] สวัสดี").queue("[2] สบายดีไหม").queue("[3] {0}สบายดี{1}")

    status = run(project, fake, copy(project, "sample.en.srt"))

    assert status == 0
    assert "[GLOSSARY]\nHello = translate: สวัสดี" in fake.calls[0][1]["content"]
    assert "[GLOSSARY]" not in fake.calls[1][1]["content"]
    assert "[GLOSSARY]" not in fake.calls[2][1]["content"]


def test_editing_a_term_re_translates_only_its_lines(project, fake):
    (project / "config.toml").write_text(NO_CONTEXT, encoding="utf-8")
    source = copy(project, "sample.en.srt")
    write_glossary(project, glossary.Term("Hello", "translate", "สวัสดี"))

    fake.queue(REPLIES)
    run(project, fake, source)
    assert fake.call_count == 1

    assert run(project, fake, source) == 0  # unchanged notes: everything cached
    assert fake.call_count == 1

    write_glossary(project, glossary.Term("Hello", "translate", "หวัดดี"))
    fake.queue("[1] หวัดดี")
    status = run(project, fake, source)

    assert status == 0
    assert fake.call_count == 2
    prompt = fake.calls[-1][1]["content"]
    assert "[TRANSLATE]\n[1] Hello there." in prompt
    assert "How are you?" not in prompt and "Fine, thanks." not in prompt
    assert "หวัดดี" in prompt


def test_a_bible_edit_re_translates_cached_lines(project, fake):
    source = copy(project, "sample.en.srt")
    write_bible(project, "Somsak is the father.\n")

    fake.queue(REPLIES)
    run(project, fake, source)
    assert run(project, fake, source) == 0  # cached
    assert fake.call_count == 1

    write_bible(project, "Somsak is the mother.\n")
    fake.queue(REPLIES)
    status = run(project, fake, source)

    assert status == 0
    assert fake.call_count == 2
    assert "Somsak is the mother." in fake.calls[-1][1]["content"]


def test_dry_run_counts_the_glossary(project, fake, capsys):
    source = copy(project, "sample.en.srt")
    run(project, fake, source, "--dry-run")
    without = _prompt_tokens(capsys.readouterr().out)

    write_glossary(project, glossary.Term("Hello", "translate", "สวัสดี"))
    run(project, fake, source, "--dry-run")
    with_terms = _prompt_tokens(capsys.readouterr().out)

    assert with_terms > without
    assert fake.call_count == 0


# --- report.txt (step 19) ---------------------------------------------------

# sample.en.srt has three entries: "Hello there.", "How are you?", <i>Fine, thanks.</i>
CLEAN_REPORT = "Checked 3 entries; nothing flagged."


def test_a_run_writes_a_report_beside_the_source(project, fake, capsys):
    fake.queue(REPLIES)
    status = run(project, fake, project / "sample.en.srt")

    report = project / "sample.report.txt"
    assert status == 0
    assert report.exists()
    text = report.read_text(encoding="utf-8")
    assert "Translation report" in text
    assert "source:  sample.en.srt" in text
    assert "output:  sample.th.srt" in text
    assert "model:   test/model" in text
    assert CLEAN_REPORT in text
    assert "Each flag" not in text


def test_the_report_path_is_printed(project, fake, capsys):
    fake.queue(REPLIES)
    run(project, fake, project / "sample.en.srt")
    err = capsys.readouterr().err
    assert "report: " in err
    assert "sample.report.txt" in err


def test_the_report_lists_failed_entries(project, fake, capsys):
    # Entry 2 leaks Chinese, so it is retried and finally left in English.
    fake.queue("[1] สวัสดี\n[2] 你好\n[3] {0}สบายดี{1}").queue("[2] 你好").queue("[2] 你好").queue("[2] 你好")
    status = run(project, fake, project / "sample.en.srt")

    text = (project / "sample.report.txt").read_text(encoding="utf-8")
    assert status == 0
    assert "Checked 3 entries, flagged 1 entry." in text
    assert "Flags: failed 1" in text
    assert "[2] failed" in text
    assert "left in English" in text
    assert "How are you?" in text


def test_the_report_follows_the_source_not_a_custom_output(project, fake):
    target = project / "custom.srt"
    fake.queue(REPLIES)
    run(project, fake, project / "sample.en.srt", "--output", target)

    assert target.exists()
    assert (project / "sample.report.txt").exists()
    assert "output:  custom.srt" in (project / "sample.report.txt").read_text(encoding="utf-8")


def test_each_file_in_a_multi_file_run_gets_a_report(project, fake):
    fake.queue(REPLIES).queue(REPLIES)
    status = run(project, fake, copy(project, "a.en.srt"), copy(project, "b.en.srt"))

    assert status == 0
    assert (project / "a.report.txt").exists()
    assert (project / "b.report.txt").exists()


def test_the_report_does_not_change_the_translation(project, fake):
    fake.queue("[1] สวัสดี\n[2] สบายดีไหม\n[3] {0}ok นะ{1}")
    run(project, fake, project / "sample.en.srt")

    out = srtio.load_srt(project / "sample.th.srt")
    # The english_left flag must not rewrite the kept word.
    assert out.subtitles[2].content == "<i>ok นะ</i>"
    assert "ok" in (project / "sample.report.txt").read_text(encoding="utf-8")


def test_dry_run_writes_no_report(project, fake):
    run(project, fake, project / "sample.en.srt", "--dry-run")
    assert not (project / "sample.report.txt").exists()
    assert fake.call_count == 0


def test_bible_only_writes_no_report(project, fake):
    fake.queue(BIBLE_REPLY)
    run(project, fake, project / "sample.en.srt", "--bible-only")
    assert not (project / "sample.report.txt").exists()


def test_the_report_uses_the_glossary(project, fake):
    write_glossary(project, glossary.Term("Hello", "translate", "สวัสดี"))
    fake.queue("[1] หวัดดี\n[2] สบายดีไหม\n[3] {0}ok นะ{1}")
    run(project, fake, project / "sample.en.srt")

    text = (project / "sample.report.txt").read_text(encoding="utf-8")
    assert "glossary_missing" in text
    assert "สวัสดี" in text


def test_cached_entries_are_still_verified(project, fake, capsys):
    fake.queue(REPLIES)
    run(project, fake, project / "sample.en.srt")
    (project / "sample.report.txt").unlink()

    # Second run: nothing is queued, so every entry comes from the cache.
    status = run(project, fake, project / "sample.en.srt")
    assert status == 0
    assert fake.call_count == 1
    assert (project / "sample.report.txt").exists()
    assert CLEAN_REPORT in (project / "sample.report.txt").read_text(encoding="utf-8")


def test_an_unwritable_report_only_warns(project, fake, capsys, monkeypatch):
    fake.queue(REPLIES)

    def boom(path, text):
        raise OSError("read-only")

    monkeypatch.setattr(verify, "write_report", boom)
    status = run(project, fake, project / "sample.en.srt")
    captured = capsys.readouterr()

    assert status == 0
    assert (project / "sample.th.srt").exists()
    assert "report:" not in captured.err


# --- --fix (step 20) --------------------------------------------------------

# Entry 2 keeps an English word in the first pass; the fix pass must redo only it.
MIXED_REPLIES = "[1] สวัสดี\n[2] ไป market นะ\n[3] {0}สบายดี{1}"


def test_fix_retranslates_the_flagged_line(project, fake, capsys):
    fake.queue(MIXED_REPLIES).queue("[2] ไปตลาดนะ")
    status = run(project, fake, project / "sample.en.srt", "--fix")

    out = srtio.load_srt(project / "sample.th.srt")
    report = (project / "sample.report.txt").read_text(encoding="utf-8")
    assert status == 0
    assert fake.call_count == 2
    assert "[TRANSLATE]\n[2] How are you?" in fake.calls[1][1]["content"]
    assert "correction pass" in fake.calls[1][0]["content"]
    assert out.subtitles[1].content == "ไปตลาดนะ"
    assert out.subtitles[0].content == "สวัสดี"
    assert "Fix pass: re-translated 1 entry (all clean now)" in report
    assert "nothing flagged" in report


def test_fix_makes_no_extra_call_when_nothing_is_flagged(project, fake):
    fake.queue(REPLIES)
    status = run(project, fake, project / "sample.en.srt", "--fix")

    assert status == 0
    assert fake.call_count == 1
    assert "Fix pass" not in (project / "sample.report.txt").read_text(encoding="utf-8")


def test_fix_reports_what_still_fails(project, fake):
    # The fix attempt leaks Chinese too, so the earlier Thai is kept.
    fake.queue(MIXED_REPLIES).queue("[2] 你好").queue("[2] 你好").queue("[2] 你好").queue("[2] 你好")
    status = run(project, fake, project / "sample.en.srt", "--fix")

    out = srtio.load_srt(project / "sample.th.srt")
    report = (project / "sample.report.txt").read_text(encoding="utf-8")
    assert status == 0
    assert out.subtitles[1].content == "ไป market นะ"
    assert "Fix pass: re-translated 1 entry (0 clean, 1 still flagged)" in report


# --- --review (step 21) -----------------------------------------------------


def test_review_puts_suggestions_in_the_report(project, fake, capsys):
    fake.queue(MIXED_REPLIES).queue("[2] ไปตลาดนะ")
    status = run(project, fake, project / "sample.en.srt", "--review")

    out = srtio.load_srt(project / "sample.th.srt")
    report = (project / "sample.report.txt").read_text(encoding="utf-8")
    assert status == 0
    assert fake.call_count == 2
    assert out.subtitles[1].content == "ไป market นะ"  # review never changes the file
    assert "Suggestions (from --review, nothing applied):" in report
    assert "  [2] ไปตลาดนะ" in report
    assert report.index("Suggestions") < report.index("[2] english_left")


def test_review_makes_no_extra_call_when_nothing_is_flagged(project, fake):
    fake.queue(REPLIES)
    status = run(project, fake, project / "sample.en.srt", "--review")

    assert status == 0
    assert fake.call_count == 1
    assert "Suggestions" not in (project / "sample.report.txt").read_text(encoding="utf-8")


def test_fix_and_review_fix_then_advise(project, fake):
    fake.queue(MIXED_REPLIES).queue("[2] ไปตลาดนะ")
    status = run(project, fake, project / "sample.en.srt", "--fix", "--review")

    out = srtio.load_srt(project / "sample.th.srt")
    report = (project / "sample.report.txt").read_text(encoding="utf-8")
    assert status == 0
    assert fake.call_count == 2  # first pass, then the fix; review had nothing left
    assert out.subtitles[1].content == "ไปตลาดนะ"
    assert "Fix pass: re-translated 1 entry (all clean now)" in report
    assert "Suggestions" not in report


# --- cost reporting ---------------------------------------------------------


def test_a_dry_run_uses_the_published_price(project, monkeypatch, capsys):
    monkeypatch.setattr("pricing.fetch_price", lambda *a, **k: (0.15, 0.60))
    status = translate.main(
        [
            str(project / "sample.en.srt"),
            "--config",
            str(project / "config.toml"),
            "--dry-run",
        ]
    )
    out = capsys.readouterr().out
    assert status == 0
    assert "$" in out
    assert "input" in out and "output" in out and "reasoning" in out


def test_a_dry_run_falls_back_to_the_configured_price(project, capsys):
    (project / "config.toml").write_text(
        '[llm]\nmodel = "test/model"\n'
        "input_usd_per_mtok = 3.0\noutput_usd_per_mtok = 6.0\n",
        encoding="utf-8",
    )
    status = translate.main(
        [
            str(project / "sample.en.srt"),
            "--config",
            str(project / "config.toml"),
            "--dry-run",
        ]
    )
    out = capsys.readouterr().out
    assert status == 0
    assert "$" in out


def test_the_summary_splits_the_tokens(project, fake, capsys):
    from llm import Completion, Usage

    fake.queue(
        Completion(
            text=REPLIES,
            usage=Usage(
                prompt_tokens=100,
                completion_tokens=50,
                reasoning_tokens=40,
                cost_usd=0.0025,
            ),
        )
    )
    run(project, fake, copy(project, "sample.en.srt"))
    out = capsys.readouterr().out
    assert "150 tokens (100 in, 50 out, 40 reasoning)" in out
    assert "$0.0025" in out
