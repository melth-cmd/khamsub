# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.4.0] - 2026-11-12

### Added

- `--version` prints the package version.

### Changed

- KhamSub installs as a command: `uv tool install git+https://github.com/melth-cmd/khamsub`,
  `pipx install ...`, a wheel from the release, or `pip install .` from a clone.
  `python -m khamsub` works too. The modules now live in the `khamsub/` package;
  the root `translate.py` stays as a small shim, so old commands still run.
- Dependencies and metadata live in `pyproject.toml`; `requirements.txt` is gone.
- The config template is package data copied by `--init-config`. The built-in
  defaults now include the example model and fallback prices, so a fresh install
  needs no config file to run (the key is still required for a real translation).
  A `./config.toml` in the folder you run from still overrides the user file.
- Usage text says `khamsub` instead of `translate.py`.

## [0.3.0] - 2026-11-12

### Added

- `--repair` fixes an existing Thai `.srt` in place from the English file beside
  it: encoding and line endings, whitespace, stray digit-only `{N}` placeholders,
  one missing or orphan tag, and 3+ line cues reduced to two. It audits the
  result against the aligned English for empty, truncated, English-left, mixed
  script, glossary and timing problems, and writes `FILE.repair-report.txt`.
  The original is kept as `FILE.bak` (an existing one needs `--force`), indexes
  and timings never change, and no key or model is used. Only `.en.srt`/`.th.srt`
  and `.en.hi.srt`/`.th.hi.srt` pairs are accepted for now; mixed HI/non-HI is
  issue #21. `--source PATH` picks a different English file and `--dry-run`
  writes nothing.
- `--repair --retranslate` rewrites only the report's candidate lines (empty,
  truncated, English left in, mixed script, glossary) using the aligned English,
  the cache beside the English file, and its glossary/bible. `--dry-run` prints
  the candidate count and the token/cost estimate without a request. Cues with
  no confident English line are never rewritten, and a run with no candidates
  needs no key or config.
- `--where` prints every config.toml and .env path a run would use, which one
  wins, and where the key would come from. It never prints the key.
- `--init-config` writes `~/.config/khamsub/config.toml` and
  `~/.config/khamsub/.env` (0600) on first use, and never overwrites either.
- Config and key lookup now has one order: `--config`, then the current folder,
  then `~/.config/khamsub` (honouring `XDG_CONFIG_HOME`), then built-in
  defaults. For the key: the real environment, then `./.env`, then the user
  file. Falling back is announced once, and a missing model or key names the
  file to edit (or `--init-config`) while the shell export still works.
- `./.env` is read for the API key when the environment variable is unset, so
  `cp .env.example .env` then editing one line is enough to start. The real
  environment still wins, the file is only read, and the key is never logged.
- `.env.example` is the template for that file; `.env` stays gitignored, and the
  pre-commit hook now allows the example while still refusing a real key.

## [0.2.1] - 2026-11-10

### Added

- The run summary (per file, and the total) names the provider that served the
  requests when the API reports it: `$0.0053 via DeepSeek`. A run split between
  providers lists each with its request count. Reporting only; the cache keys and
  the cost maths are untouched (#16).
- `stray_punctuation` flag: a Thai line that ends with a full stop is reported
  and asked once more, then kept. Ellipses, `?`/`!`, a decimal (`88.5`) and a
  mid-line abbreviation (`ดร.`) are left alone. A repeated stop never turns the
  line back into English (#17).
- `truncation` flag: an entry that comes back with fewer lines than its source
  and far shorter than it (below 45% of the source length) is reported as a
  possible truncation instead of as an ordinary line-count change. On a corpus
  of 10,083 cached pairs this fires 8 times, where the previous `short` floor
  (15%) never fired at all.

### Changed

- The bible pass now records acronyms with the same letters-or-word rule as the
  translator, so the glossary and the prompt agree on the sound (AIM -> เอม, NASA
  -> นาซา). A live `--bible-only` run on the coverage sample writes AOL, AIM,
  NASA, FBI, JBL, UGIS and FM into the glossary with Thai spellings (#14).
- A reply that merges a multi-line cue and comes back far shorter is asked once
  more, then kept and flagged. A merge that is merely compact is common, and
  leaving it in English would be the worse guess.
- The report tells the two kinds of merge apart: a merge `layout.py` left on one
  line because no safe break point existed now reads "left merged by layout
  repair (no safe break point)" instead of looking like a changed line count.

### Fixed

- English ordinals and decade plurals (`1st`, `25th`, `20s`, `1990s`) no longer
  have to survive verbatim. They were treated as short letter-digit codes, so no
  natural Thai translation could pass and every such cue fell back to English
  (#18). Only the digit run is required now; a suffix left in the Thai is
  reported as leftover English, and real short codes (`K9`, `21L4`, `A1`) are
  unchanged.

## [0.2.0] - 2026-11-01

### Added

- A live progress line while translating: the batch, a bar, the time spent and a
  rough estimate of what is left. On a terminal it redraws in place, so a long
  request is visibly alive instead of looking hung; when the output is
  redirected, one line is printed per batch. `-v` keeps its per-batch log detail
  and falls back to one line per batch, so the two never fight over a row.
- `--dry-run` reports input and output tokens and cost on separate lines, and
  asks OpenRouter for the model's current price instead of trusting
  `config.toml`. It does not pretend to know the reasoning bill: it says so, and
  shows what reasoning cost on `samples/coverage.srt` (2.3x the visible output)
  as an illustration. `config.toml` prices are now only a fallback for providers
  that publish no price list.
- A finished run reports prompt, completion, reasoning and cached tokens, plus
  the exact cost the provider charged (`usage.cost`), so a run can be priced
  without a price table and a reasoning model can be told apart from a chatty one.
- Transliteration policy: names, nicknames, brands, song and film titles, speaker
  labels and acronyms are now rendered in Thai. A speaker label keeps its colon
  and stays on its words (`MAYA: No.` -> `เมย่า: ไม่`).
- `lost_token` check: a number, URL, @handle or short letter-digit code (under 5
  characters, e.g. `K9`) that the translation drops is a retryable failure and a
  report flag. A longer code keeps its digits and transliterates its letters
  (`Flymiamibro22` -> `ฟลายมยเอมีโบร22`).
- `bleed` flag: an entry whose first line repeats a line from the cue above is
  reported, unless the two sources share that line, so a deliberate refrain is
  not mistaken for a copy.
- `mixed_script` check: Latin letters glued to Thai (`ทwerp`) are a retryable
  failure and a report flag, so a mangled fragment can no longer ship as advisory
  text. Kept tokens are blanked first, so a short code that legitimately touches
  Thai is not reported.

### Changed

- A translation that comes back with more lines than the source is retried once,
  then merged back down to the source line count, so a third line can no longer
  overflow the safe area. An undashed continuation is absorbed into the line
  above it first, which keeps the speaker turns intact.
- A source cue that wrapped a speaker's continuation onto its own line is now
  joined to that speaker before translation, so the model is no longer forced to
  reproduce a stub line like `in school.`. Only cues that use a speaker dash are
  touched, and a group is joined only when it fits on one line, so no break point
  is invented. The source text is part of the cache key, so affected entries are
  retranslated once (18.9% of the multi-line cues measured).
- `english_left` no longer reports a run glued to Thai; that fragment is
  `mixed_script`, so the two flags never describe the same text.
- `english_left` is strict: only links, handles and short codes may stay Latin,
  so a name or label left in English is now reported as a mistake instead of
  being excused by the glossary keep-list.
- Prompt version 3: transliteration rules, digit preservation, the label rule and
  the official-title rule; the bible now asks for transliterations by default.

### Fixed

- A speaker dash written as U+2010 HYPHEN (`‐`) is now recognised. 1,600 two-line
  cues in the test episodes start with it, and every dash rule missed them: the
  dash count was zero, the dash repair never fired, `dashes_lost` could never
  report, and a wrapped continuation was never joined.
- Layout repair no longer splits Thai mid-word. When the model merges a
  multi-line entry, the re-wrap may only break at a space, and no piece may fall
  below 35% of its expected length; an entry with no safe break point is left on
  one line and reported as "left merged" instead of being cut at an arbitrary
  character. On the cached test runs this removed every mid-word cut (362 of
  6,402 multi-line entries are now left merged, 90% of them short enough to fit
  a single line).
- A URL or e-mail followed by sentence punctuation no longer takes that
  punctuation into the token it must keep. A correct translation that writes Thai
  punctuation after a link was reported as `lost_token`, retried, and could fall
  back to English.

## [0.1.0] - 2026-10-30

First public release.

- English `.srt` subtitles translated into Thai with an LLM (OpenRouter by
  default); only the subtitle text is sent, indexes and timestamps are copied.
- Per-entry cache, so a rerun pays only for lines that are new.
- Hand-editable story bible and glossary to keep names, pronouns and politeness
  consistent.
- Report of flagged lines; nothing is changed automatically.
- Several files per run, `--dry-run`, `--bible-only`, `--fix`, `--review`,
  `--limit`, `--range`, `--output`, `--config`, `-v` and `--force`.
- Any OpenAI-compatible provider through `[llm]`.
- API key from the environment only; exit codes 0, 1 and 2.

[Unreleased]: https://github.com/melth-cmd/khamsub/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/melth-cmd/khamsub/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/melth-cmd/khamsub/compare/v0.2.1...v0.3.0
[0.2.1]: https://github.com/melth-cmd/khamsub/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/melth-cmd/khamsub/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/melth-cmd/khamsub/releases/tag/v0.1.0
