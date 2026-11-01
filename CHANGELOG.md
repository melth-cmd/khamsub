# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `--dry-run` asks OpenRouter for the model's current price instead of trusting
  `config.toml`, and says the estimate excludes reasoning tokens. `config.toml`
  prices are now only a fallback for providers that publish no price list.
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
- `mixed_script` check: Latin letters glued to Thai (`ทwerp`) are a retryable
  failure and a report flag, so a mangled fragment can no longer ship as advisory
  text. Kept tokens are blanked first, so a short code that legitimately touches
  Thai is not reported.

### Changed

- A source cue that wrapped a speaker's continuation onto its own line is now
  joined to that speaker before translation, so the model is no longer forced to
  reproduce a stub line like `in school.`. Only cues that use a speaker dash are
  touched, and a group is joined only when it fits on one line, so no break point
  is invented. The source text is part of the cache key, so affected entries are
  retranslated once (18.9% of the multi-line cues measured on subs/).
- `english_left` no longer reports a run glued to Thai; that fragment is
  `mixed_script`, so the two flags never describe the same text.
- `english_left` is strict: only links, handles and short codes may stay Latin,
  so a name or label left in English is now reported as a mistake instead of
  being excused by the glossary keep-list.
- Prompt version 3: transliteration rules, digit preservation, the label rule and
  the official-title rule; the bible now asks for transliterations by default.

### Fixed

- A speaker dash written as U+2010 HYPHEN (`‐`) is now recognised. 1600 two-line
  cues in the PEN15 sources start with it, and every dash rule missed them: the
  dash count was zero, the dash repair never fired, `dashes_lost` could never
  report, and a wrapped continuation was never joined.
- Layout repair no longer splits Thai mid-word. When the model merges a
  multi-line entry, the re-wrap may only break at a space, and no piece may fall
  below 35% of its expected length; an entry with no safe break point is left on
  one line and reported as "left merged" instead of being cut at an arbitrary
  character. On 16 cached runs this removed every mid-word cut (362 entries per
  6402 multi-line entries are now left merged, 90% of them short enough to fit a
  single line).
- A URL or e-mail followed by sentence punctuation no longer takes that
  punctuation into the token it must keep. A correct translation that writes Thai
  punctuation after a link was reported as `lost_token`, retried, and could fall
  back to English.

## [0.1.0] - 2026-10-30

First public release.

### Added

- Command-line tool that translates English `.srt` subtitles into Thai using an
  LLM (OpenRouter by default).
- Only subtitle text is sent to the model; subtitle numbers and timestamps are
  never changed.
- Translation cache (`.cache.json`) so reruns do not pay for finished lines again.
- Story bible (`.bible.md`) and glossary (`.glossary.toml`) to keep names,
  pronouns and politeness consistent. Both are plain text and meant to be
  edited by hand.
  - Editing a glossary term re-translates only the lines that use it.
  - Editing the bible re-translates the whole file.
- Quality-check report (`.report.txt`) listing flagged lines, with checks for
  `english_left`, `glossary_missing`, `line_count`, `dashes_lost` and `failed`.
  Nothing is changed automatically.
- Command options:
  - Multiple input files in one run; a failing file does not stop the others.
  - `--dry-run` to show the batch count and a rough cost with no API call.
  - `--bible-only` (with `--force` to rebuild) to generate the bible and
    glossary before translating a long series.
  - `--fix` to re-translate only flagged lines with a stricter prompt.
  - `--review` to have the model suggest improvements for flagged lines
    without changing the subtitles.
  - `--limit` and `--range` to translate part of a file.
  - `--output`, `--config`, `-v` and `--force`.
- Optional `strip_hi` setting to remove sound descriptions such as `[door slams]`
  and `(laughs)` before translating.
- Support for any OpenAI-compatible provider through `[llm]` settings in
  `config.toml`, including custom request headers.
- API key is read from the environment only and is never written to config
  or cache files.
- Exit codes: `0` finished, `1` a file failed, `2` problem before any work.
- Test suite (`pytest`) and development requirements file.

[Unreleased]: https://github.com/melth-cmd/khamsub/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/melth-cmd/khamsub/releases/tag/v0.1.0
