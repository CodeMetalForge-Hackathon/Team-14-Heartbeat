# Training the knock classifier

The classifier decides whether each sound is a knock. It learns from **real recordings through the mic we demo with, on our slab**. Synthetic data got the code working, but the current model rejected real knocks in a test recording, so it must be retrained on real audio.

Run everything on **the main laptop** from the `Team-14` folder; that's where the YAMNet model lives. All the steps take about 10 minutes once the recordings exist.

## Rules for recordings
- **One label per file, 60 seconds each, as long as possible.** Don't trim a file down to just the knocks: the detector needs a few seconds of room sound to measure the noise floor.
- **Don't augment.** No speed or gain copies, and no padding with silence. Copies of one take add no real variety. Silence padding creates fake "clicks" at the joins, and our detector picks those up as sounds.
- **Name the file** `<label>__<surface>__<desc>__<NN>.wav` and put it in `data/raw/`.
  - The label is one of `knock`, `other`, `bg` or `self`.
  - End the description with `-stick`, so the mic is recorded in the file name.
  - The tools skip any file with a different name.
- **Don't commit recordings to git.** `data/raw/` is ignored on purpose. Move files with AirDrop instead.
- **Minimum to train:** **8 knock files and 8 non-knock files** (`other`, `bg` and `self` all count as non-knock). The training tool refuses to run with too few files per class, because it tests on recordings it never trained on. Aim for 150 or more knocks in total.

## Recording
Record with the stick, following `docs/RECORDING.md`. `hb record` writes correctly named files straight into `data/raw/`.

## Train
1. **Keep a copy of the current synthetic model:**
   ```bash
   cp models/knock_head.joblib models/knock_head.synthetic.joblib
   ```
2. **Cut each recording into one clip per sound:**
   ```bash
   uv run python tools/segment.py --raw data/raw --clips data/clips
   ```
3. **Review the clips, about 5 minutes.**
   1. Generate the review pages:
      ```bash
      uv run python tools/label_review.py --index data/clips/index.csv
      ```
   2. Open `reports/review_knock.html`, then the `other`, `bg` and `self` pages.
   3. Listen to the suspicious clips, which are listed first. Mark the wrong ones: a cough in a knock file, or a knock in an "other" file.
   4. Download the removal list, then apply it:
      ```bash
      uv run python tools/label_review.py --index data/clips/index.csv --remove-list ~/Downloads/review_remove.json
      ```
   If you ever re-run step 2, the review edits are lost and you must review again.
4. **Train and test:**
   ```bash
   uv run --extra ml python tools/train_head.py --profile rubble
   ```
   This writes the new `models/knock_head.joblib` and `reports/classifier.md`.
5. **Measure false alarms** on the long background file:
   ```bash
   uv run --extra ml python tools/eval_background.py data/raw/<your bg file>.wav --classifier auto
   ```

## Read the results (`reports/classifier.md`)
- **Good:** the **head** row has knock recall of 0.9 or higher at a false-positive rate of 5% or lower, and beats the **heuristic** row.
  - Use it. The profile's `classifier.name: auto` already picks it.
- **Not good:** the head does worse than the heuristic.
  - For the demo, set `classifier.name: heuristic` in `profiles/rubble.yaml`. The knock conversation does most of the filtering anyway.
  - Tell Claude the numbers.
- **The background report:** false knocks per hour are expected. **False confirmations should be 0.**

## Check it live
```bash
uv run --extra ml hb listen --device stick
```
- **Knocks on the slab** should print `KNOCK`.
- **A dropped book, footsteps and talking** should print `REJECTED`.

Paste a few lines, plus `reports/classifier.md`, to Claude.

## Retraining later
To retrain, add new files to `data/raw/` and repeat steps 2–5. Step 2 regenerates the clips and drops your review edits, so review again.
