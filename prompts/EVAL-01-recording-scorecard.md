# EVAL-01: scorecard on the real stick recordings

Read `AGENTS.md` first. **Run this on the laptop that has the recordings in `data/raw/`.**

**New files only.** Another session (CLAP-05) is editing `runner.py`, `classifiers/clap.py` and `doctor.py`, so don't edit any existing file.

## Why
The judges need numbers, not "it detects well". The data team recorded one-label files through the stick (`docs/RECORDING.md`): every sound in a `knock__` file is a knock, and no sound in an `other__` or `self__` file is. That makes per-file scoring honest without marking times by hand.

## Files you own (all new)
- `tools/scorecard.py`
- `tests/test_scorecard.py`

## The tool
```bash
uv run python tools/scorecard.py [--raw data/raw] [--classifiers heuristic,clap] [--profile rubble] [--clap-profile clap] [--out reports/scorecard.md]
```

**For each file named `<label>__<surface>__<desc>__<NN>.wav`:**
1. Stream it through the real `OnsetDetector`, with the profile's settings.
2. Classify every candidate with each requested classifier:
   - `heuristic`, `head` and `auto` use `--profile`.
   - `clap` uses `--clap-profile`, and the CLAP service must already be running. If its `/health` fails, print the start command from `docs/CLAP.md` and skip CLAP; don't crash.
3. Record the number of candidates and the number accepted as `KNOCK`.

Skip files whose names don't match, and list them.

**Report `reports/scorecard.md`, plus the same summary on the console:**
- **Per file:** label, desc, minutes, candidates, and accepted per classifier.
- **Per label, per classifier:**
  - `knock` files: the share of candidates accepted (the knock pass rate).
  - `other` and `self` files: the share wrongly accepted, plus false knocks per minute.
  - `bg` files: false knocks per hour.
- **A "headline" block** of 3–4 plain sentences someone can paste onto a slide, for example: "On 16 real stick recordings (412 knocks), CLAP accepted 94% of knocks and 6% of non-knock sounds." Use the real numbers and say which classifier.
- **The bg file's false confirmations**, if one exists. Run the existing `tools/eval_background.py` on it as a **subprocess**, with the best classifier, and quote its false-confirmation line. Don't import its internals.
- **A caveats section:**
  - The candidates in a knock file include every detected sound, so the pass rate is a lower bound.
  - It's one slab and one room.
  - The thresholds weren't tuned on these files.

## Tests (no network, no mic)
- Use synthetic WAVs from `heartbeat.synth`, saved with valid names in a temp dir.
- Check filename parsing and that bad names are skipped.
- Check the per-label rates with a fake classifier that accepts a known set.
- CLAP unreachable: it is skipped with a message.
- The headline text uses the computed numbers.
- The `eval_background` subprocess is mocked.
- The full suite passes.

## Done when
1. The tests pass.
2. A human runs it on the real `data/raw`, with the CLAP service running, and pastes `reports/scorecard.md` to Claude.

Commit only your files: `EVAL-01: recording scorecard`.
