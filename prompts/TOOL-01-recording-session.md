# TOOL-01: guided recording session

Read `AGENTS.md` first. **Runs on the data teammate's own laptop and clone.**

**New files only:** another session (FIX-02) is changing `heartbeat/stickmic.py`, `sources.py`, `cli.py` and `config.py`. **Don't edit any existing file.**

## Goal
The data teammate must record about 25 one-minute files at the slab (the list in `docs/RECORDING.md`). Doing that by copy-pasting commands is slow, and mistakes are easy to make. Build a guided session that walks through the list:
- what to do next
- a countdown
- recording
- an instant quality check
- redo or continue

It must also resume after a crash, and say when the training minimum is met.

## Files you own (all new)
- `tools/record_session.py`
- `profiles/recording_plan.yaml`
- `tests/test_record_session.py`

## The plan (`profiles/recording_plan.yaml`)
**Each item has these fields:**
- `label`
- `surface`
- `desc`: the mic tag is appended at runtime
- `seconds`: default 60
- `star`: true/false
- `instructions`: one or two plain sentences telling the person what to do

**Create these items** with the same names as `docs/RECORDING.md`. ★ means `star: true`.

**Knock items:**

| `desc` | ★ |
|---|---|
| `underside-knuckles` | ★ |
| `underside-fist` | ★ |
| `patterns-2-3-4` | ★ |
| `knuckles-soft-30cm` | ★ |
| `knuckles-medium-1m` | ★ |
| `knuckles-hard-2m` | ★ |
| `metal-object-1m` | ★ |
| `weak-tired` | ★ |
| `fist-medium-1m` | |
| `far-end` | |

**Other items:**

| `desc` | ★ |
|---|---|
| `objects-set-down` | ★ |
| `book-drop` | ★ |
| `table-bumps` | ★ |
| `footsteps-walking-past` | ★ |
| `talking-nearby` | ★ |
| `pen-keys-drop` | ★ |
| `typing-on-laptop` | |
| `bag-drop` | |
| `chair-scrape` | |
| `stomps-near` | |
| `claps` | |
| `cough-laugh` | |
| `phone-music` | |

**Self items:**

| `desc` | ★ | Note |
|---|---|---|
| `servo-knocks` | ★ | |
| `speaker-prompts` | ★ | The tool may play `assets/voice/*.wav` itself with `afplay` (macOS) or `aplay` (Linux), behind a `--play-prompts` flag |

**The bg item:** `venue-evening`, 2400 s. It's marked `unattended: true`, so it's only run with `--bg`.

**Every item uses surface `table`.** Write the instructions so a stranger could follow them.

## The tool
```bash
uv run python tools/record_session.py [--device stick] [--mic stick] [--only-star] [--plan profiles/recording_plan.yaml] [--bg] [--practice]
```
**Record by calling `hb record` as a subprocess:** `uv run hb record --device ... --label ... --surface ... --desc <desc>-<mic> --seconds N`. Parse its `Saved <path> (<secs>s, peak <p>).` line.
- This avoids importing code that FIX-02 is changing.
- If the line is missing or the exit code is nonzero, show the output and offer retry or skip.

**For each item:**
1. Show `[7/24] KNOCK · underside-knuckles ★`, the instructions, and the keys `Enter = record`, `s = skip`, `q = quit`.
2. Record the file.
3. **Quality check.** Load the WAV and run P01's `OnsetDetector` with the `rubble` profile, read-only. Show the duration, the peak, and the number of sounds detected.
   - **Warn** if the peak is ≥ 0.99 (clipping), if the peak is < 0.02 (too quiet), or if a knock file has fewer than 10 detected sounds.
   - For `other`/`self` items, just report the count.
4. Offer `Enter = keep and continue`, `r = redo` (delete this file, then record again), `q = quit`.

**Resume:**
- Skip items whose `data/raw/<label>__table__<desc>-<mic>__*.wav` already exists, unless `--redo-all` is given.
- Append every action to `data/raw/session_log.csv`: time, item, path, peak, sounds, kept or redone.

**`--practice`:** move each kept file to `data/practice/` (create it; it's git-ignored) instead of leaving it in `data/raw/`. This allows a dry run with the laptop mic without polluting training data. Add `data/practice/` to `.gitignore` **only if it's not already ignored**. That's the single allowed edit to an existing file.

**Summary at the end, or on quit:**
- files per label
- knock sounds detected in total
- **"Training minimum met: 8/8 knock files, 8/8 non-knock files"**, or what's missing

**Robustness:** Ctrl-C during a recording must leave no half-written file in `data/raw/`. Delete it, and log it as aborted.

## Tests (no mic, no network)
- The plan loads and is validated: unknown keys are rejected, and all 26 items are present.
- `--only-star` filtering works.
- Resume skips existing files.
- The quality check flags clipping, quietness and low knock counts on synthetic WAVs from `heartbeat.synth`.
- The summary counts, including whether the minimum is met.
- `--practice` moves files.
- The subprocess call is mocked, including a failure path.

## Done when
1. The tests pass, and the full suite still passes.
2. A human can run `uv run python tools/record_session.py --practice --only-star` with the laptop mic for a dry run.

Commit only your files: `TOOL-01: guided recording session`. Then:
```bash
git pull --rebase && git push
```
