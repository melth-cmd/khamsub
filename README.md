# KhamSub

Thai subtitles for some movies and shows are very hard to find and in a lot of
cases don't exist at all. This tool aims to make that a thing of the past.

A small command-line tool that turns English `.srt` subtitles into Thai with an
AI model (OpenRouter by default).

- Only the subtitle text goes to the model. Numbers and timestamps never change.
- Everything is rendered in Thai: names, brands, film and song titles, speaker
  labels and acronyms. Only numbers, URLs, @handles, e-mail and short
  letter-digit codes (under 5 characters, like `K9`) stay Latin.
- Reruns use a cache, so finished lines are not paid for twice.
- A story bible and a glossary keep names, pronouns and politeness consistent.
- A report shows what the checks found. Nothing is changed unless you ask.
- A 60-cue sample with a rubric lives in `samples/`, for comparing models before
  trusting one with a whole film.

## Install

Requires Python 3.11+.

    git clone https://github.com/melth-cmd/khamsub.git
    cd khamsub
    python -m venv .venv && source .venv/bin/activate
    pip install -r requirements.txt

Get an API key from openrouter.ai and export it. The key is read from the
environment only; it is never written to config or to the cache.

    export OPENROUTER_API_KEY=your-key-here

Open `config.toml` and check `[llm] model`. The default is an example model; use
any model that writes good Thai and that your key can call.

## First run

    python translate.py "Movie (2026).en.srt"

This reads the English file and writes `Movie (2026).th.srt` next to it. It also
writes a cache and a report (see "Files it makes"). A typical episode may take a
few minutes.

While it runs, one line shows the batch, a bar, the time spent and a rough
estimate of what is left. It updates in place on a terminal; when the output is
redirected, one line is printed per batch.

## Commands

    python translate.py "Movie.en.srt"
      Translate one file.

    python translate.py *.en.srt
      Translate several files in one run. A file that fails is reported, and the
      other files still run.

    python translate.py "Show.en.srt" --dry-run
      Show the batches and the input/output cost, with reasoning tokens shown
      as not estimated. No API call, no key needed.

    python translate.py "Show.en.srt" --bible-only
      Read the whole script once and write the story bible and glossary, then
      stop. Useful before translating a long series.

    python translate.py "Show.en.srt" --fix
      After translating, translate only the lines the checks flagged, with a
      stricter prompt.

    python translate.py "Show.en.srt" --review
      After translating, ask the model to look at the flagged lines. Its ideas
      are listed at the top of the report; the subtitles are not changed.

    python translate.py "Show.en.srt" --limit 20
      Translate only the first 20 subtitles, for a quick test.

    python translate.py "Show.en.srt" --range 100-140
      Translate only subtitle numbers 100 to 140 (real numbers from the file).

    python translate.py "Show.en.srt" --output /tmp/out.srt
      Write the Thai file somewhere else. One input file only.

    python translate.py "Show.en.srt" --config mine.toml
      Use another config file.

    python translate.py "Show.en.srt" -v
      Add per-batch log detail. The progress line is shown either way.

    python translate.py "Show.en.srt" --force
      Also accept inputs that look translated (`*.th.srt`). With `--bible-only`
      it rebuilds existing bible/glossary files, losing hand edits.

To drop sound descriptions like `[door slams]` and `(laughs)` before
translating, set `strip_hi = true` in `[cleanup]` of `config.toml`. That is a
config setting, not a command.

## Files it makes

For `Movie.en.srt` you get these next to it (the last two only with `--bible-only`):

    Movie.th.srt          the Thai subtitles (the file you watch)
    Movie.report.txt      what the checks found, and review suggestions
    Movie.cache.json      finished lines, so a rerun is free
    Movie.bible.md        characters and how they speak
    Movie.glossary.toml   names and their Thai spelling

The sidecar files drop the `.en.` part, so `Show.en.srt` gives `Show.report.txt`.
A normal run reads the bible and glossary if they exist; it never creates them.

## Read the report

Open the `.report.txt` after a run. It says how many lines were checked and
lists any problems with the line number, the English and the Thai. Common flags:

    failed                 the line could not be translated and stays English
    empty                  nothing came back
    short / long           the length is far from the source's
    line_count             the translation has a different number of lines
    truncation             fewer lines than the source and far shorter: text
                           may be missing
    bleed                  the first line repeats a line from the cue above
    dashes_lost            a speaker dash such as "- " disappeared
    stray_punctuation      a line ends with a full stop, which Thai does not use
    tag_mismatch           formatting tags are missing or invented
    lost_token             a number, URL or code from the source is missing
    glossary_missing       a term is not written the way the glossary says
    glossary_inconsistent  a term that should be Thai is still English
    mixed_script           Latin letters glued to Thai, a mangled fragment
    english_left           English left in the translation

Line layout is repaired automatically: breaks are re-split, speaker dashes are
put back, and a translation with more lines than its source is retried once and
then merged down. A line that comes back merged and far shorter is asked again
once, then kept and flagged as `truncation`. When the only difference is a merge
that layout allowed, `line_count` says so ("left merged by layout repair").
Everything else is reported. To let the model try the flagged lines again, run
with `--fix`. For advice without any change, run with `--review`; its
suggestions appear at the top of the report.

## The bible and glossary

Both files are plain text and made to be edited by hand.

- `bible.md` describes the story and the characters, including the Thai
  pronouns and particles each person should use.
- `glossary.toml` says how each name or term is written: spell it in Thai
  letters (the default), translate it, or keep it in English. The bible pass
  only writes `keep` for numbers, links, handles and short codes, because
  everything else is meant to be Thai; a hand edit can still keep any term.

A normal run reads both files again, so an edit applies to the next run. Editing
one glossary term re-translates only the lines that use it. Editing the bible
re-translates the whole file, because pronouns and politeness can depend on it.

`--bible-only` creates the files from the script. It never overwrites existing
files without `--force`.

## Use another provider

Any service with an OpenAI-compatible API works (DeepSeek, Groq, a local server,
and so on). Change three lines in `config.toml`:

    [llm]
    base_url = "https://api.deepseek.com/v1"
    key_env  = "DEEPSEEK_API_KEY"
    model    = "deepseek-chat"

Then export the new key:

    export DEEPSEEK_API_KEY=your-key-here

If the provider needs extra request headers, add them as a table. Never put the
key here:

    [llm.extra_headers]
    "HTTP-Referer" = "https://example.test"

## If something goes wrong

- `... is not set` — export the API key named in `[llm] key_env`.
- Thai looks garbled in your player — set `encoding = "utf-8-sig"` in `[output]`.
- The run stops and names a glossary term — fix that term, or rebuild the file
  with `--bible-only --force`. No API call was made.
- Some lines are still English — open the report, or run again with `--fix`.
- `rate_limited` — the tool waits and retries. If it keeps happening, try a
  smaller `[batch] size` (fewer tokens per request) or another model/provider.
- The headline cost leaves out reasoning tokens, which a reasoning model spends
  freely. A finished run prints the exact cost it was charged.
- "no story bible for ..." — that is only a hint. The tool works without one;
  `--bible-only` makes one.

## Exit codes

    0   finished (lines left in English are still exit 0: check the report)
    1   a file failed with an error; the other files still ran
    2   a problem before any work: bad usage, bad config, missing file

## What is where

`STRUCTURE.md` lists every file, what it controls, the order of one run, and where
each past bug lived. Read it before your first change.

## Development

The tests need `pytest`, which is not needed to run the tool:

    uv pip install -r requirements-dev.txt
    pytest -q
