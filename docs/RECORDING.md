# Recording training data (data person)

The classifier learns what a knock sounds like **through our microphone, on our slab**. Every file you record here becomes training or test data. Plan on about 35 minutes of active recording, plus a 45-minute background recording that runs unattended.

## 0. Setup (10 minutes)
1. **Get the code.** Skip this step if you're using the laptop that already has the repo.
   ```bash
   brew install uv
   ```
   ```bash
   git clone https://github.com/CodeMetalForge-Hackathon/Team-14.git && cd Team-14 && uv sync
   ```
2. **Pick the mic.** Record with the mic we'll demo with: the **M5StickC Plus SE**, plugged in over USB once its firmware (P07) is flashed.
   - **Mounting:** press the stick's mic hole flat onto the slab, seal a ring of Blu-Tack around the hole, and cover the stick with foam or a folded cloth.
   - Set a tag for the mic. It goes into every filename, so data from different mics never gets mixed:
     ```bash
     export MIC=stick
     ```
   - If you have to use the laptop mic before the stick works, use `export MIC=lapmic` instead.
3. **Set the device:**
   ```bash
   export DEV=stick
   ```
   - For the laptop mic, run `uv run hb devices` and use its number instead, for example `export DEV=3`.
   - Run both `export` lines again in every new terminal window.
4. **Set the input level.**
   1. Set the starting volume:
      - **Stick:** the level is `stick_gain_db` in `profiles/rubble.yaml`. Start at 0.
      - **Laptop mic:** open System Settings → Sound → Input, select the mic, and set the input volume to about 50%.
   2. Start the meter:
      ```bash
      uv run hb listen --device $DEV
      ```
   3. Knock **hard, right next to the mic**. The `peak` number should stay at **0.6 or below**. If you see `peak 1.00`, turn it down and try again: lower `stick_gain_db` by 6 at a time (stick), or lower the input volume (laptop).
   4. If ordinary knocks 1 m away barely show up, raise `stick_gain_db` by 6 instead.
   5. Ignore the KNOCK and REJECTED labels; you're only checking the level. Press Ctrl-C to stop.
5. **Mark the mic's spot** on the slab with tape. Don't move the mic between files unless a step says to.

## 1. Rules
- **One label per file.**
  - A `knock` file contains only knocks. No talking, coughing or bumping.
  - An `other` file contains **no knocks**, and no palm slaps on the slab either: a trapped person might slap, so slaps count as signals.
- In knock files:
  - Knock in groups of 1–4, with a 2–3 second pause between groups.
  - Vary the force within the file.
- Each command counts down for 3 seconds, then records for 60 seconds.
- Files number themselves (`__01`, `__02`, …), so you can re-run a line to get more of that sound.
- If you ruin a file, delete it. It's the newest file in `data/raw/`; this command lists the newest first:
  ```bash
  ls -t data/raw | head -3
  ```
  Then `rm data/raw/<that file>`.

## 2. Knocks on the table (about 12 minutes)
The mic is on the slab. Run these one at a time:
```bash
uv run hb record --device $DEV --label knock --surface table --desc knuckles-soft-30cm-$MIC --seconds 60
uv run hb record --device $DEV --label knock --surface table --desc knuckles-medium-1m-$MIC --seconds 60
uv run hb record --device $DEV --label knock --surface table --desc knuckles-hard-2m-$MIC --seconds 60
uv run hb record --device $DEV --label knock --surface table --desc fist-medium-1m-$MIC --seconds 60
uv run hb record --device $DEV --label knock --surface table --desc metal-object-1m-$MIC --seconds 60
uv run hb record --device $DEV --label knock --surface table --desc underside-knuckles-$MIC --seconds 60
uv run hb record --device $DEV --label knock --surface table --desc underside-fist-$MIC --seconds 60
uv run hb record --device $DEV --label knock --surface table --desc patterns-2-3-4-$MIC --seconds 60
uv run hb record --device $DEV --label knock --surface table --desc weak-tired-$MIC --seconds 60
uv run hb record --device $DEV --label knock --surface table --desc far-end-$MIC --seconds 60
```
What some of these mean:
- **metal-object:** knock with keys, a wrench or a rock instead of your hand.
- **underside:** knock from under the table. **This is exactly the demo**, so do it well.
- **patterns-2-3-4:** knock in exact groups of 2, 3 and 4.
- **weak-tired:** slow, uneven knocks, like an exhausted person.
- **far-end:** knock at the far end of the slab.

**Optional: other surfaces.** Tape the mic onto each surface first, then knock on that surface:
```bash
uv run hb record --device $DEV --label knock --surface cardboard --desc knuckles-mixed-$MIC --seconds 60
uv run hb record --device $DEV --label knock --surface plywood --desc knuckles-mixed-$MIC --seconds 60
uv run hb record --device $DEV --label knock --surface metal --desc knuckles-mixed-$MIC --seconds 60
uv run hb record --device $DEV --label knock --surface concrete --desc knuckles-mixed-$MIC --seconds 60
```

## 3. Other sounds (about 15 minutes)
Put the mic back on its taped spot on the table. **No knocks** during any of these:
```bash
uv run hb record --device $DEV --label other --surface table --desc talking-nearby-$MIC --seconds 60
uv run hb record --device $DEV --label other --surface table --desc typing-on-laptop-$MIC --seconds 60
uv run hb record --device $DEV --label other --surface table --desc objects-set-down-$MIC --seconds 60
uv run hb record --device $DEV --label other --surface table --desc book-drop-$MIC --seconds 60
uv run hb record --device $DEV --label other --surface table --desc pen-keys-drop-$MIC --seconds 60
uv run hb record --device $DEV --label other --surface table --desc bag-drop-$MIC --seconds 60
uv run hb record --device $DEV --label other --surface table --desc table-bumps-$MIC --seconds 60
uv run hb record --device $DEV --label other --surface table --desc chair-scrape-$MIC --seconds 60
uv run hb record --device $DEV --label other --surface table --desc footsteps-walking-past-$MIC --seconds 60
uv run hb record --device $DEV --label other --surface table --desc stomps-near-$MIC --seconds 60
uv run hb record --device $DEV --label other --surface table --desc claps-$MIC --seconds 60
uv run hb record --device $DEV --label other --surface table --desc cough-laugh-$MIC --seconds 60
uv run hb record --device $DEV --label other --surface table --desc phone-music-$MIC --seconds 60
```
What some of these mean:
- **objects-set-down:** put cups, phones and bottles down on the table, normally.
- **table-bumps:** bump the table with a hip or knee, push it, lean on it.

**When the rover drives:** put the mic on the floor and run:
```bash
uv run hb record --device $DEV --label other --surface floor --desc rover-driving-$MIC --seconds 60
```

## 4. Our own devices (about 3 minutes, once the knocker works)
1. Start recording the knocker:
   ```bash
   uv run hb record --device $DEV --label self --surface table --desc servo-knocks-$MIC --seconds 60
   ```
2. While it records, the hardware person runs `knock 3` over and over. Their guide is `docs/KNOCKER-SETUP.md`.
3. Then start recording the speaker:
   ```bash
   uv run hb record --device $DEV --label self --surface table --desc speaker-prompts-$MIC --seconds 60
   ```
4. While that records, play our voice prompts from a second terminal:
   ```bash
   for f in assets/voice/*.wav; do afplay "$f"; sleep 1; done
   ```

## 5. Background (45 minutes, unattended)
This is the most important file. It measures how often normal venue noise fools us.
```bash
caffeinate -i uv run hb record --device $DEV --label bg --surface table --desc venue-afternoon-$MIC --seconds 2700
```
- `caffeinate` keeps the Mac from sleeping. Keep the laptop plugged in.
- Life goes on around the slab, but nobody knocks on it **deliberately**. Do this while the knocker and rover work happen somewhere else.
- If you have time later, record a second one in the evening, with `--desc venue-evening-$MIC`.

## 6. Hand off
- Recordings land in `data/raw/`. That folder isn't in git, so if you recorded on another laptop, AirDrop it to the Codex driver.
- Then the Codex driver trains on the data:
  ```bash
  uv sync --extra ml
  uv run --extra ml python tools/fetch_models.py --allow-network
  uv run python tools/segment.py --raw data/raw --clips data/clips
  uv run python tools/label_review.py --index data/clips/index.csv
  uv run --extra ml python tools/train_head.py --profile rubble
  uv run --extra ml python tools/eval_background.py data/raw/<the bg file>.wav --classifier auto
  ```
  - After `label_review.py`, open `reports/review_knock.html`, mark the bad clips, and apply the list it downloads with `--remove-list`.
  - Paste `reports/classifier.md` and the background report to Claude.
