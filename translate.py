"""Command line entry point.

    python translate.py input.en.srt
    python translate.py *.en.srt --config my.toml
    python translate.py input.en.srt --bible-only

The API key comes from the environment variable named in [llm] key_env. It is
read only when the client is built, and never printed.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

import batcher
import bible
import config as config_module
import glossary
import llm
import pipeline
import pricing
import srtio
import verify

log = logging.getLogger("translate")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="translate.py",
        description="Translate English .srt subtitles into Thai with an LLM.",
    )
    parser.add_argument("files", nargs="+", metavar="FILE", help="English .srt file(s)")
    parser.add_argument(
        "--config", metavar="PATH", help="Path to config.toml (default: next to this script)"
    )
    parser.add_argument(
        "--output", metavar="PATH", help="Write here instead of FILE.th.srt (one file only)"
    )
    parser.add_argument(
        "--limit", type=int, metavar="N", help="Translate only the first N entries"
    )
    parser.add_argument(
        "--range", dest="range_", metavar="A-B",
        help="Translate only entries with subtitle indexes A to B (inclusive)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show batches, tokens and estimated cost without calling the API",
    )
    parser.add_argument(
        "--bible-only", action="store_true",
        help="Read the script once and write FILE.bible.md + FILE.glossary.toml, "
        "then stop (no translation)",
    )
    parser.add_argument(
        "--fix", action="store_true",
        help="After translating, re-translate only the entries the checks flagged "
        "(stricter prompt; the report shows what changed)",
    )
    parser.add_argument(
        "--review", action="store_true",
        help="After translating, ask the model to review the flagged lines and put its "
        "suggestions at the top of the report (nothing is applied)",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Also accept inputs that look like existing translations (*.th.srt), "
        "and rebuild bible/glossary files that already exist",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Show per-batch progress"
    )
    return parser


def planned_output(
    source: Path, settings: config_module.Settings, output: Path | None = None
) -> Path:
    """Where translating `source` would be written."""
    return output or srtio.output_path_for(
        source, strip_en_suffix=settings.output.strip_en_suffix
    )


def bible_paths(source: Path) -> tuple[Path, Path]:
    """Where the pre-pass writes its two hand-editable files."""
    return (
        srtio.sidecar_path(source, ".bible.md"),
        srtio.sidecar_path(source, ".glossary.toml"),
    )


def check_bible_targets(files: list[str], force: bool = False) -> list[str]:
    """Files the pre-pass would overwrite, checked before any call is made.

    Both files are hand-editable, so rebuilding is never implicit: it takes
    --force, and every affected file is reported before the first call.
    """
    if force:
        return []

    problems: list[str] = []
    for name in files:
        source = Path(name)
        if not source.is_file():
            continue  # check_inputs already reported it
        existing = [path for path in bible_paths(source) if path.exists()]
        if existing:
            problems.append(
                f"{existing[0]} already exists; use --force to rebuild the bible "
                "and glossary (hand edits will be lost)"
            )
    return problems


def looks_translated(path: Path) -> bool:
    """True for names this program would produce: Movie.th.srt, Movie.en.th.srt."""
    return path.suffix.lower() == ".srt" and path.stem.lower().endswith(".th")


def check_inputs(
    files: list[str],
    settings: config_module.Settings,
    output: Path | None = None,
    force: bool = False,
) -> list[str]:
    """Every reason the file list cannot be translated, checked before any call.

    One batch run is a single instruction, so it is all or nothing: reporting a
    collision after three files were translated has already overwritten one.
    """
    problems: list[str] = []
    seen: dict[Path, Path] = {}
    targets: dict[Path, Path] = {}

    for name in files:
        source = Path(name)
        if not source.exists():
            problems.append(f"{source} not found")
            continue
        if not source.is_file():
            problems.append(f"{source} is not a file")
            continue

        resolved = source.resolve()
        if resolved in seen:
            problems.append(f"{source} is given more than once")
            continue
        seen[resolved] = source

        if not force and looks_translated(source):
            problems.append(
                f"{source} looks like an existing translation (name ends in .th); "
                "use --force to translate it anyway"
            )

        target = planned_output(source, settings, output)
        key = target.resolve()
        if key == resolved:
            problems.append(f"{source} would be overwritten by its own output")
            continue
        if key in targets:
            problems.append(
                f"{source} and {targets[key]} would both write {target.name}; "
                "translate them one at a time"
            )
            continue
        targets[key] = source

    return problems


def load_notes_for(files: list[str]) -> tuple[dict[Path, bible.Notes], list[str]]:
    """Read every file's bible and glossary before any work starts.

    One call per run, so an edit always applies to the next run. Problems are
    collected rather than raised: a typo in a hand-edited glossary should stop
    the run once, with every bad file named, not three files in.
    """
    notes: dict[Path, bible.Notes] = {}
    problems: list[str] = []
    for name in files:
        source = Path(name)
        if not source.is_file():
            continue  # check_inputs already reported it
        try:
            notes[source] = bible.load_notes(source)
        except (bible.BibleError, glossary.GlossaryError) as exc:
            problems.append(str(exc))
    return notes, problems


def missing_notes_hint(notes: dict[Path, bible.Notes]) -> str | None:
    """One line pointing at --bible-only, or None when every file has a bible."""
    missing = [source.name for source, entry in notes.items() if not entry.has_bible]
    if not missing:
        return None
    if len(missing) == 1:
        return f"hint: no story bible for {missing[0]} (optional; --bible-only builds one)"
    return (
        f"hint: no story bible for {len(missing)} of {len(notes)} files "
        "(optional; --bible-only builds one)"
    )


def translate_one(
    client: llm.ChatClient,
    source: Path,
    settings: config_module.Settings,
    output: Path | None = None,
    limit: int | None = None,
    range_: str | None = None,
    notes: bible.Notes | None = None,
    fix: bool = False,
    review: bool = False,
) -> tuple[pipeline.TranslatedFile, Path, Path | None]:
    """Translate a single file and write its report.

    Returns (result, output path, report path); the report path is None when it
    could not be written, which is a warning, not a failed translation.
    """
    target = planned_output(source, settings, output)
    if notes is not None:
        log.info(
            "%s: bible %s, glossary %s",
            source.name,
            f"{notes.bible_path.name} ({len(notes.bible):,} chars)"
            if notes.has_bible
            else "none",
            f"{notes.glossary_path.name} ({len(notes.terms)} "
            f"term{'' if len(notes.terms) == 1 else 's'})"
            if notes.has_glossary
            else "none",
        )
    terms = notes.terms if notes is not None else ()
    result, target = pipeline.translate_file(
        client,
        source,
        target,
        encoding=settings.output.encoding,
        strip_hi=settings.cleanup.strip_hi,
        model=settings.llm.model,
        size=settings.batch.size,
        context_before=settings.batch.context_before,
        context_after=settings.batch.context_after,
        max_retries=settings.llm.max_retries,
        limit=limit,
        range_=range_,
        bible=notes.bible if notes is not None else "",
        terms=terms,
        fix=fix,
        review=review,
    )
    return result, target, write_report_for(source, target, result, terms, settings)


def write_report_for(
    source: Path,
    target: Path,
    result: pipeline.TranslatedFile,
    terms: Sequence[glossary.Term],
    settings: config_module.Settings,
) -> Path | None:
    """Verify the finished file and write its report beside the source.

    An unwritable report only warns: the subtitles are already written, and a
    read-only folder must not turn a finished translation into a failed run.
    """
    verification = verify.verify_file(result, terms)
    text = verify.render_report(
        result,
        verification,
        source_name=source.name,
        target_name=target.name,
        model=settings.llm.model,
    )
    path = verify.report_path(source)
    try:
        verify.write_report(path, text)
    except OSError as exc:
        log.warning("could not write %s: %s", path, exc)
        return None
    return path


def _usage_text(usage: llm.Usage, detail: bool = True) -> str:
    """Tokens, optionally split, and the cost when the provider reported one.

    `completion_tokens` already includes `reasoning_tokens`, so reasoning is
    shown as part of the output rather than added to it.
    """
    tokens = usage.prompt_tokens + usage.completion_tokens
    text = f"{tokens:,} tokens"
    if detail and tokens:
        parts = [f"{usage.prompt_tokens:,} in", f"{usage.completion_tokens:,} out"]
        if usage.reasoning_tokens:
            parts.append(f"{usage.reasoning_tokens:,} reasoning")
        if usage.cached_tokens:
            parts.append(f"{usage.cached_tokens:,} cached")
        text += " (" + ", ".join(parts) + ")"
    if usage.cost_usd is not None:
        text += f" ${usage.cost_usd:.4f}"
    return text


def _summary(
    source: Path,
    target: Path,
    result: pipeline.TranslatedFile,
    notes: bible.Notes | None = None,
) -> str:
    count = len(result.subtitles)
    parts = [
        f"{source.name} -> {target.name}",
        f"{count} {'entry' if count == 1 else 'entries'}",
    ]
    if result.dropped:
        parts.append(f"{len(result.dropped)} dropped")
    if result.failures:
        parts.append(f"{len(result.failures)} FAILED")
    parts.append(_usage_text(result.usage))
    if notes is not None and notes.has_bible:
        parts.append(f"bible {len(notes.bible):,} chars")
    return "  ".join(parts)


def _dry_run(
    args,
    settings: config_module.Settings,
    notes: dict[Path, bible.Notes] | None = None,
) -> int:
    """Report what the run would do. Needs no API key and makes no calls."""
    notes = notes or {}
    price = pricing.fetch_price(settings.llm.model, settings.llm.base_url)
    if price is None:
        price = (settings.llm.input_usd_per_mtok, settings.llm.output_usd_per_mtok)
    input_price, output_price = price
    status = 0
    for name in args.files:
        source = Path(name)
        if not source.exists():
            print(f"error: {source} not found", file=sys.stderr)
            status = 2
            continue

        loaded = srtio.load_srt(source)
        estimate = pipeline.estimate(
            loaded.subtitles,
            strip_hi=settings.cleanup.strip_hi,
            size=settings.batch.size,
            context_before=settings.batch.context_before,
            context_after=settings.batch.context_after,
            limit=args.limit,
            range_=args.range_,
            bible=notes[source].bible if source in notes else "",
            terms=notes[source].terms if source in notes else (),
            input_usd_per_mtok=input_price,
            output_usd_per_mtok=output_price,
        )
        dropped = f", {estimate.dropped} dropped" if estimate.dropped else ""
        batches = "batch" if estimate.batches == 1 else "batches"
        entries = "entry" if estimate.entries == 1 else "entries"
        if estimate.cost_usd:
            cost = f"~${estimate.cost_usd:.4f} (excludes reasoning)"
        else:
            cost = "cost unknown"
        print(
            f"{source.name}: {estimate.entries} {entries}{dropped} in "
            f"{estimate.batches} {batches}, ~{estimate.prompt_tokens:,} + "
            f"{estimate.completion_tokens:,} tokens, {cost}"
        )
    return status


def _bible_summary(
    source: Path, result: bible.BibleResult, bible_path: Path, glossary_path: Path
) -> str:
    characters = len(result.bible.characters)
    terms = len(result.bible.terms)
    parts = [
        f"{source.name} -> {bible_path.name}, {glossary_path.name}",
        f"{characters} {'character' if characters == 1 else 'characters'}",
        f"{terms} {'term' if terms == 1 else 'terms'}",
        f"{result.total_tokens:,} tokens",
    ]
    return "  ".join(parts)


def _bible_only(
    args,
    sources: list[Path],
    settings: config_module.Settings,
    client: llm.ChatClient,
) -> int:
    """Read each script once and write its bible and glossary, then stop."""
    status = 0
    for number, source in enumerate(sources, 1):
        if len(sources) > 1:
            print(f"[{number}/{len(sources)}] {source.name}", file=sys.stderr)

        bible_path, glossary_path = bible_paths(source)
        try:
            loaded = srtio.load_srt(source)
            prepared = pipeline.prepare(
                loaded.subtitles, strip_hi=settings.cleanup.strip_hi
            )
            selected = batcher.select_range(
                prepared.entries, limit=args.limit, range_=args.range_
            )
            title, year = bible.title_from_filename(source)
            result = bible.build(client, selected, title=title, year=year)
            bible.write(result.bible, bible_path, glossary_path)
        except Exception as exc:  # noqa: BLE001 - one bad file must not stop the rest
            print(f"error: {source.name}: {exc}", file=sys.stderr)
            status = 1
            continue

        print(_bible_summary(source, result, bible_path, glossary_path))

    if status == 0:
        print(
            "Edit the files by hand if needed, then translate without --bible-only.",
            file=sys.stderr,
        )
    return status


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(message)s",
    )

    if args.output and len(args.files) > 1:
        print("error: --output can only be used with a single input file", file=sys.stderr)
        return 2
    if args.output and args.bible_only:
        print(
            "error: --output is for translated subtitles; --bible-only writes "
            "FILE.bible.md and FILE.glossary.toml next to the input",
            file=sys.stderr,
        )
        return 2

    if args.limit is not None and args.limit < 0:
        print("error: --limit must not be negative", file=sys.stderr)
        return 2
    if args.range_ is not None:
        try:
            batcher.parse_range(args.range_)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

    try:
        settings = config_module.load_settings(args.config)
    except config_module.ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    output = Path(args.output) if args.output else None
    problems = check_inputs(args.files, settings, output, force=args.force)
    if args.bible_only:
        problems += check_bible_targets(args.files, force=args.force)
    if problems:
        for problem in problems:
            print(f"error: {problem}", file=sys.stderr)
        return 2

    # The bible and glossary are read here, once, so a hand edit applies to this
    # run, and a broken glossary stops the run before a single paid call.
    notes: dict[Path, bible.Notes] = {}
    if not args.bible_only:
        notes, note_problems = load_notes_for(args.files)
        if note_problems:
            for problem in note_problems:
                print(f"error: {problem}", file=sys.stderr)
            return 2
        hint = missing_notes_hint(notes)
        if hint:
            print(hint, file=sys.stderr)

    if args.dry_run:
        return _dry_run(args, settings, notes)

    if settings.output.bilingual:
        print("warning: bilingual output is not implemented yet; writing Thai only",
              file=sys.stderr)

    sources = [Path(name) for name in args.files]
    if len(sources) > 1 and (args.limit is not None or args.range_ is not None):
        print("warning: --limit/--range apply to every file given", file=sys.stderr)

    try:
        client = config_module.build_client(settings)
    except (config_module.ConfigError, llm.AuthError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.bible_only:
        return _bible_only(args, sources, settings, client)

    status = 0
    failed_files = 0
    entries = 0
    usage = llm.Usage()
    leftover = 0

    for number, source in enumerate(sources, 1):
        if len(sources) > 1:
            print(f"[{number}/{len(sources)}] {source.name}", file=sys.stderr)
        try:
            result, target, report = translate_one(
                client,
                source,
                settings,
                output,
                limit=args.limit,
                range_=args.range_,
                notes=notes.get(source),
                fix=args.fix,
                review=args.review,
            )
        except Exception as exc:  # noqa: BLE001 - one bad file must not stop the rest
            print(f"error: {source.name}: {exc}", file=sys.stderr)
            status = 1
            failed_files += 1
            continue

        entries += len(result.subtitles)
        usage = usage + result.usage
        leftover += len(result.failures)

        print(_summary(source, target, result, notes.get(source)))
        if result.failures:
            print(
                f"  {len(result.failures)} entr"
                f"{'y' if len(result.failures) == 1 else 'ies'} left in English",
                file=sys.stderr,
            )
        if report is not None:
            print(f"  report: {report}", file=sys.stderr)

    if len(sources) > 1:
        print(_total(len(sources), failed_files, entries, leftover, usage))
    return status


def _total(
    files: int, failed: int, entries: int, leftover: int, usage: llm.Usage
) -> str:
    """One line for the whole run, so 20 files do not hide the totals."""
    parts = [f"{files} files", f"{files - failed} translated"]
    if failed:
        parts.append(f"{failed} FAILED")
    parts.append(f"{entries} {'entry' if entries == 1 else 'entries'}")
    if leftover:
        parts.append(f"{leftover} left in English")
    parts.append(_usage_text(usage, detail=False))
    return "total: " + "  ".join(parts)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
