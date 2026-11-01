# Coverage sample

`samples/coverage.srt` is a 60-cue subtitle file built to exercise every behaviour
this project has had to fix or argue about. One run over it replaces a whole-film
live test.

It is also the model-comparison harness: run `translate.py` on it with **no bible
and no glossary**, then score the output against the table below. The tool has to
do well unaided; the bible is a later upgrade, not a requirement.

## Running it

```sh
.venv/bin/python translate.py samples/coverage.srt
```

- 60 cues, 59 sent (cue 48 is dropped: nothing to translate), 1 batch.
- ~1,400 prompt tokens, about $0.0014 on the current model.
- Outputs land next to the input and are gitignored: `coverage.th.srt`,
  `coverage.cache.json`, `coverage.report.txt`.
- Do not pass `--bible` or a glossary when scoring models. Same prompt version,
  same config, one model per run.

## How to score

1. Run the tool on `coverage.srt` with one model.
2. Run `.venv/bin/python translate.py samples/coverage.srt --review` or open
   `coverage.report.txt` and count flags by kind.
3. Walk the table below. Each row has one thing to check, so a failure names the
   behaviour rather than "the Thai looked wrong".

The rows marked **auto** are mechanical and can be scripted; the rest need a Thai
reader or a careful eye.

## Cues

### Line structure (#1, #6, #11)

| Cue | Tests | Good looks like |
|---|---|---|
| 1 | baseline, one short line | one Thai line |
| 2 | two source lines, two sentences | exactly 2 lines |
| 3 | two long lines, Thai will have no spaces | exactly 2 lines; **auto**: no line starts with a combining mark (ั ิ ี ึ ื ุ ู ็ ์ ่ ้ ๊ ๋) and none ends with เ แ โ ใ ไ or ็ |
| 4 | dash, dash, then a continuation stub (#11) | 3 lines; the first two keep a dash, the third does not |
| 5 | dash, dash, bare continuation | 3 lines; dashes on lines 1–2 only |
| 6 | `<i>` spanning two dash lines | **auto**: `<i>` preserved once, 2 lines |
| 7 | one long line | exactly **1** line; it must not be split to match anything |

### Speaker labels (#7, #12)

| Cue | Tests | Good looks like |
|---|---|---|
| 8 | `MAYA:` label + text | Thai label, colon kept, Thai text; **auto**: no Latin in the line |
| 9 | label-only cue | still a label; colon kept; not empty, not dropped |
| 10 | alphanumeric label `DJ K9:` | label rendered in Thai; `K9` stays Latin (#13) |
| 11 | `DR. REYES:` title + period | label transliterated; the `DR.` period does not leak into the text |
| 12 | two labels joined by `&` | both names rendered; the `&` is not dropped or left bare |

### Names (#8, #12)

| Cue | Tests | Good looks like |
|---|---|---|
| 13 | two names in dialogue, no label | both transliterated, consistent with cues 8 and 12 |
| 14 | `Ume` / `Oomi` / `Ume`, three lines | **the two spellings differ**; the mishearing joke survives |
| 15 | `Ume` again | **auto**: same spelling as cue 14 |
| 16 | `Ume` again | **auto**: same spelling as cues 14–15 |
| 60 | `Who is Ume?` | **auto**: same spelling again, punctuation handled |

### Brands (#8)

| Cue | Tests | Good looks like |
|---|---|---|
| 17 | `Chanel`, settled Thai spelling | ชาแนล, or a defensible transliteration; no Latin |
| 18 | `Swarovski`, settled Thai spelling | สวารอฟสกี้, or a defensible transliteration |
| 19 | `Dippin' Dots`, no settled spelling | a transliteration; the apostrophe handled, no Latin |

### Titles (#8, #12)

| Cue | Tests | Good looks like |
|---|---|---|
| 20 | `"Final Destination"`, official Thai title exists | ปลายทางมรณะ, or a transliteration if unsure — **never an invented title** |
| 21 | `"The Lighthouse of Nowhere"`, invented film | transliteration; the model must not invent a Thai title |
| 22 | song title in quotes | transliterated; quotes kept or dropped consistently |
| 23 | book title in quotes | transliterated |

### Acronyms (#14)

| Cue | Tests | Good looks like |
|---|---|---|
| 24 | `AOL`, spelled as letters | เอโอเอล |
| 25 | `AIM`, read as a word | เอม |
| 26 | `NASA`, read as a word | นาซา |
| 27 | `FBI agent`, letters + common noun | เอฟบีไอ + เอเจนต์ |
| 28 | `JBL`, spelled as letters | เจบีแอล |
| 50 | `MAYA: NASA.` label + acronym | Thai label; NASA matches cue 26 |

### Numbers, codes and links (#13)

All of these are **auto**: the token must appear unchanged in the translation.

| Cue | Token | Note |
|---|---|---|
| 29 | `06:00` | a time, not `หกโมง` |
| 30 | `1,000` | the comma stays |
| 31 | `5%` | the percent sign stays |
| 32 | `63` | source is `'63`; the apostrophe may go, the digits may not |
| 33 | `@flymiamibro22` | handle verbatim |
| 34 | `21L4` | alphanumeric verbatim |
| 35 | `3B` | alphanumeric verbatim |
| 36 | `15` | source `Pen15`; letters become Thai, digits stay (เพน15) |
| 37 | `grade A` | **not** a keep: the bare `A` becomes เอ (เกรดเอ) |
| 38 | `https://nightshift.example.com/listen` | URL verbatim |
| 39 | `desk@nightshift.example.com` | address verbatim |
| 40 | URL followed by a comma | URL exact; the comma stays outside it |
| 41 | address followed by a period | address exact; the period stays outside it |
| 42 | `88.5` and `FM` | digits verbatim; `FM` transliterated (เอฟเอ็ม) |

### Validator edges (#3, #9, #13)

| Cue | Tests | Good looks like |
|---|---|---|
| 43 | three lines, no dashes | exactly 3 lines, safe break points |
| 44 | `- UGIS.` / `- UGIS.`, acronym-only cue (#3) | rendered in Thai, **not** left English and **not** `failed` |
| 45 | `Bullshit.`, single word (#9) | Thai only; **auto**: no Latin touching Thai (mixed_script) |
| 46 | `42.`, number-only cue | `42` preserved. The tool sends this but does not validate it |
| 47 | `[MUSIC PLAYING]` | unchanged; non-verbal, not validated |
| 48 | `♪ ♪` | **dropped before translation**; must be absent from the output |
| 49 | `Mm-hmm.` | unchanged, or a natural Thai interjection |

### Context bleed (#10)

| Cue | Tests | Good looks like |
|---|---|---|
| 51 | a refrain line | Thai |
| 52 | the **same** line again | **auto**: same Thai as cue 51, not a copy of a neighbour |
| 53 | a setup line | Thai |
| 54 | opens with cue 53's words (`I don't know, …`) | not a copy of cue 53 |

### Tags and signs

| Cue | Tests | Good looks like |
|---|---|---|
| 55 | `<i>` around a whole line | **auto**: `<i>` preserved, one line |
| 56 | `{\an8}` position tag + a sign | **auto**: `{\an8}` preserved; sign text in Thai |
| 57 | `[SIGN: CLOSED]` bracket label | bracket handled, text in Thai |
| 58 | `<i>` + a name | tag preserved; `Hector` matches cue 13 |
| 59 | two lines, a time and a number | **auto**: 2 lines; `06:00` and `1,000` exact; `Maya` matches cue 8 |

## Not covered, on purpose

- **Censored source text** (`B*******t.`). The user has never seen a censored
  `.en.srt` and judged the chance very low, so it is not worth testing for.
  (It would be skipped as non-verbal today; `is_non_verbal` splits on `*`.)
- **Glossary and bible overrides.** Deferred with the bible itself. Cue 26 and
  cue 50 would be the natural place for an acronym override once the bible lands.
- **Long-file behaviour** (#4 cost estimate, #5 progress output). Those are tool
  behaviour, not content; 60 cues will not show them.

## Adding a cue

Append it and add a row here with the same number, or the rubric drifts. Keep the
file at 60 until a second variant per behaviour is actually needed; the point is a
cheap run that several models can be compared on.
