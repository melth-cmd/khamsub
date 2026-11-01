# Project structure

Where things live, and where to look when something is wrong. Read this before
your first change; it is meant to answer "which file do I open?" in a few
seconds, for a human or an agent.

This file describes **where**. The **why** behind a decision lives in
`CHANGELOG.md` and in the local `PROGRESS.md`.

## What the tool does

One run takes an English `.srt`, sends the text to an LLM in batches, and writes
a Thai `.srt` beside it, plus a cache and a report. Timestamps and cue indexes
never change.

## Layout at a glance

```
khamsub/
├── translate.py       # CLI: flags, pre-flight, run summary, --dry-run
├── config.py          # loads and validates config.toml; builds the model client
├── config.toml        # user settings (model, batch, prices fallback)
├── srtio.py           # read/write .srt, encoding fallback, output + sidecar naming
├── tags.py            # {0} placeholders: protect / check / restore formatting tags
├── hi.py              # strip SDH descriptions ("[door slams]"), keep "[Maya]"
├── batcher.py         # batching, context lines, --limit/--range selection
├── llm.py             # OpenRouter client, the prompt, reply validation, kept tokens, usage
├── runner.py          # one batch: retry only failures, backoff, English fallback
├── cache.py           # per-entry JSON cache, atomic saves, cache keys
├── bible.py           # story bible pre-pass: chunk, prompt, merge, write both files
├── glossary.py        # terms: validate, render, parse, load; per-batch selection
├── layout.py          # normalise wrapped source lines; rewrap/merge the translation
├── verify.py          # checks a finished file, returns flags, writes report.txt
├── pipeline.py        # orchestrates one file: prepare -> batch -> translate -> write
├── pricing.py         # asks the provider for the model's current price
├── samples/
│   ├── coverage.srt   # 60-cue sample: every behaviour we have fixed or argued about
│   └── coverage.md    # the rubric for that sample, with cue numbers
├── tests/             # one test file per module, plus conftest/helpers
├── README.md          # user-facing: install, commands, report
├── STRUCTURE.md       # this file
└── CHANGELOG.md       # user-facing history

# Local only, gitignored: AGENTS.md, SPEC.md, PROGRESS.md, subs/
```

`subs/` holds real subtitle files to try things on and everything the tool writes
next to them. Nothing in it is source.

## One run, in order

This is the mental model to use when the change you want is not in the table
below. Each step names the file that owns it.

1. `translate.py` parses the flags, loads `config.toml` through `config.py`, and
   builds the client.
2. `srtio.load_srt` parses the file, normalising encoding and line endings.
3. `pipeline.prepare` strips SDH descriptions (`hi.py`), joins source lines that
   were wrapped mid-sentence (`layout.normalise_source`), and protects formatting
   tags as placeholders (`tags.py`).
4. `batcher.make_batches` groups entries into requests, with a few lines of
   context around each batch.
5. `runner.translate_batch` sends the batch and retries only the entries that
   failed (`llm.validate` decides what failed).
6. `cache` stores each entry as it lands, so a rerun pays only for what is new.
7. `layout.rewrap` repairs the line count: re-splits, restores dashes, or merges
   extra lines back down.
8. `verify.verify_file` checks the finished file and writes `report.txt`. It
   never edits the subtitles.

## Where to make a change

| I want to… | Open | Notes |
|---|---|---|
| change how a word, name or title is translated | `llm.py` | the rules are in `SYSTEM_PROMPT`; bump `PROMPT_VERSION` |
| force one specific term | `glossary.py` | a glossary term beats the prompt; hand edits beat the bible |
| make a check stricter, or add one | `verify.py` | it reports only |
| make the model retry instead of just reporting | `llm.py` | add an `Issue` in `validate`, like `missing_thai` |
| change line wrapping, splitting or merging | `layout.py` | `rewrap` and `_merge_down` |
| fix a line split mid-word | `layout.py` | `_splits_ok`, `_bad_boundary` |
| fix cue parsing, timestamps or encoding | `srtio.py` | |
| change batching or how much context a batch sees | `batcher.py` | |
| change retry count, backoff or failure handling | `runner.py` | |
| change how a whole file is processed, start to finish | `pipeline.py` | `prepare`, `translate_subtitles`, `retranslate_flagged` |
| change cost, token or usage reporting | `llm.py`, `translate.py`, `pricing.py` | `Usage`, `_print_estimate`, `fetch_price` |
| add a CLI flag | `translate.py` | then README.md |
| add a config option | `config.py`, `config.toml` | then README.md |
| change what makes two runs share a cache entry | `cache.py`, `glossary.py` | `fingerprint` |
| change the bible or glossary pre-pass | `bible.py` | |
| strip or keep SDH descriptions | `hi.py` | |
| change placeholder or tag handling | `tags.py` | |

## Symptom to module

Real bugs, and where each one lived. Useful when a symptom looks new.

| Symptom | Where it was fixed |
|---|---|
| a Thai word split in half at a line break | `layout.py` — floor on piece length, no character offsets |
| `ทwerp`: Latin glued to Thai, shipped unreported | `llm.mixed_script`, `verify.mixed_script` |
| three lines where the source had two | `llm.validate` (`extra_lines`) then `layout._merge_down` |
| a cue opening with the line above it | the `extra_lines` retry, plus `verify.bleed_flag` |
| a number, URL or code lost in translation | `llm.missing_kept`, reported as `lost_token` |
| speaker dashes ignored entirely | `layout.DASHES` was missing U+2010 |
| a stub like "in school." forced onto its own line | `layout.normalise_source` in `pipeline.prepare` |
| estimate far below the real bill | `llm.usage_from_response`, `pricing.py` |
| names and labels left in English | `llm.SYSTEM_PROMPT` (transliteration policy) |

## Invariants

Break one of these and the tool is wrong even if the tests pass.

- `verify.py` never changes subtitles. It only reports.
- Cue indexes and timestamps are copied, never recomputed.
- A line break only ever lands on a space, never inside a word or next to a Thai
  combining mark or leading vowel.
- `llm.PROMPT_VERSION` is part of every cache key. Changing the prompt without
  bumping it reuses translations written under the old rules.
- The source text is part of the cache key, so normalising the source
  retranslates those entries once.
- Numbers, URLs, @handles and short letter-digit codes must survive translation.
- The API key comes from the environment only; it is never written to config,
  cache or report.

## Adding a feature end to end

A new *check* touches four places, and forgetting one is the usual bug:

1. `verify.py` — the flag kind in `KINDS`, the function, and the call in
   `check_entry` or `verify_file`.
2. `llm.py` — an `Issue` in `validate` if the model should retry rather than
   just report.
3. `tests/test_verify.py` and `tests/test_llm.py` — one test each, with a real
   string where there is one.
4. `CHANGELOG.md` under `[Unreleased]`.

Then add a cue to `samples/coverage.srt` and a row to `samples/coverage.md` so a
live run exercises it.

## Tests

One test file per module: `tests/test_<module>.py`.

```
.venv/bin/python -m pytest -q          # the suite, no network
.venv/bin/python -m pytest -m live     # one real API call
```

`samples/coverage.srt` is the live check and the model comparison: 60 cues
covering every behaviour above, run with no bible and no glossary. Score it
against `samples/coverage.md`.

## Keeping this file current

If you add a module, add it here — `tests/test_structure.py` fails when a `.py`
file in the root is not mentioned. That is the only maintenance this file needs;
keep the prose short rather than detailed, so it stays true.
