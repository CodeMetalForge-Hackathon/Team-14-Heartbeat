# Recording training data with the stick (data person)

The classifier learns what a knock sounds like **through the M5StickC Plus SE, pressed on our slab**. That's exactly how the demo will hear it. Every file you record here becomes training or test data.

**Time:**
- about 40 minutes at the slab
- plus a 30–45 minute background recording that runs unattended later

**If time is short,** do only the ★ items. That's the minimum the training needs: 8 knock files and 8 non-knock files.

## 0. Before you start
- **Wait until the Codex driver says the stick works:** `hb listen --device stick` shows sounds when someone knocks.
- **The stick can only be used by one program at a time.** Tell the team: "Recording now: the stick is busy, and nobody knocks on or bumps the slab unless I ask."
- **Agree on a slot for the 30–45 minute background recording** (section 7), for example during dinner, when nobody needs the stick.

## 1. Setup (5 minutes)
1. **Use the laptop the stick is plugged into.** If you're using your own laptop, set it up once:
   ```bash
   brew install uv
   ```
   ```bash
   git clone https://github.com/CodeMetalForge-Hackathon/Team-14.git && cd Team-14 && uv sync
   ```
2. **Plug the stick in** with its USB-C **data** cable, then check that it's found:
   ```bash
   uv run hb devices
   ```
   A stick port should be listed. If it isn't, ask the Codex driver.
3. **Set two variables.** Run this again in every new terminal window:
   ```bash
   export DEV=stick MIC=stick
   ```

## 2. Mount the stick (5 minutes)
1. **Find its microphone pinhole.** It's a tiny hole in the case; check it against the case markings or the M5Stack picture, and don't assume every hole is the mic.
2. **Press that face flat onto the slab,** where the stick will sit during the demo.
3. **Seal the edges with gaffer tape** so the mic "listens into" the table. Keep the hole itself open, not taped over.
4. **Lay a folded cloth over the stick** to block sound from the room.
5. **Put tape on the table to mark the spot.** Don't move the stick between files.
6. **Keep the knocker at least 30 cm away** on the same slab.

## 3. Set the level (5 minutes)
1. Start the meter:
   ```bash
   uv run hb listen --device $DEV
   ```
2. **Knock hard, right next to the stick.** The `peak` value should stay at **0.6 or below**.
3. **Knock normally, 1 m away.** It should still show up, with an `snr` above about 10 dB.
4. **Adjust if needed:** change `stick_gain_db` in `profiles/rubble.yaml` in steps of 6, then restart the meter.
   - lower it (−6, −12) if you see `peak 1.00`
   - raise it (+6, +12) if far knocks don't show up
5. **If it still shows `peak 1.00` at −12,** the stick's own hardware gain is too high. Ask the Codex driver to lower the stick's `MAG` setting (the default is 16).
6. **Ignore the KNOCK and REJECTED labels** for now; the classifier isn't trained yet. Press Ctrl-C to stop.

**Write down the final `stick_gain_db`, and don't change it after you start recording.**

## 4. Rules
- **One label per file.**
  - A `knock` file contains only knocks: no talking, coughing or bumping.
  - An `other` file contains no knocks and no palm slaps on the slab. A trapped person might slap, so slaps count as signals.
- **In knock files:**
  - Knock in groups of 1–4, with a 2–3 second pause between groups.
  - Vary the force within each file.
- **Each command counts down for 3 seconds, then records for 60 seconds.**
  - Files number themselves (`__01`, `__02`, …), so you can re-run a line to get more of that sound.
- **If you ruin a file, delete it.** It's the newest file in `data/raw/`:
  ```bash
  ls -t data/raw | head -3
  ```
  Then `rm data/raw/<that file>`.

## 5. Knocks (about 12 minutes)
★ marks the minimum set.
```bash
uv run hb record --device $DEV --label knock --surface table --desc underside-knuckles-$MIC --seconds 60   # ★
uv run hb record --device $DEV --label knock --surface table --desc underside-fist-$MIC --seconds 60       # ★
uv run hb record --device $DEV --label knock --surface table --desc patterns-2-3-4-$MIC --seconds 60       # ★
uv run hb record --device $DEV --label knock --surface table --desc knuckles-soft-30cm-$MIC --seconds 60   # ★
uv run hb record --device $DEV --label knock --surface table --desc knuckles-medium-1m-$MIC --seconds 60   # ★
uv run hb record --device $DEV --label knock --surface table --desc knuckles-hard-2m-$MIC --seconds 60     # ★
uv run hb record --device $DEV --label knock --surface table --desc metal-object-1m-$MIC --seconds 60      # ★
uv run hb record --device $DEV --label knock --surface table --desc weak-tired-$MIC --seconds 60          # ★
uv run hb record --device $DEV --label knock --surface table --desc fist-medium-1m-$MIC --seconds 60
uv run hb record --device $DEV --label knock --surface table --desc far-end-$MIC --seconds 60
```
What some of these mean:
- **underside:** knock from **under the table**. This is exactly the demo, so do it well.
- **patterns-2-3-4:** knock in exact groups of 2, 3 and 4.
- **metal-object:** knock with keys, a wrench or a rock instead of your hand.
- **weak-tired:** slow, uneven knocks, like an exhausted person.
- **far-end:** knock at the far end of the slab.

## 6. Other sounds (about 15 minutes)
**No knocks** during any of these:
```bash
uv run hb record --device $DEV --label other --surface table --desc objects-set-down-$MIC --seconds 60       # ★
uv run hb record --device $DEV --label other --surface table --desc book-drop-$MIC --seconds 60              # ★
uv run hb record --device $DEV --label other --surface table --desc table-bumps-$MIC --seconds 60            # ★
uv run hb record --device $DEV --label other --surface table --desc footsteps-walking-past-$MIC --seconds 60 # ★
uv run hb record --device $DEV --label other --surface table --desc talking-nearby-$MIC --seconds 60         # ★
uv run hb record --device $DEV --label other --surface table --desc pen-keys-drop-$MIC --seconds 60          # ★
uv run hb record --device $DEV --label other --surface table --desc typing-on-laptop-$MIC --seconds 60
uv run hb record --device $DEV --label other --surface table --desc bag-drop-$MIC --seconds 60
uv run hb record --device $DEV --label other --surface table --desc chair-scrape-$MIC --seconds 60
uv run hb record --device $DEV --label other --surface table --desc stomps-near-$MIC --seconds 60
uv run hb record --device $DEV --label other --surface table --desc claps-$MIC --seconds 60
uv run hb record --device $DEV --label other --surface table --desc cough-laugh-$MIC --seconds 60
uv run hb record --device $DEV --label other --surface table --desc phone-music-$MIC --seconds 60
```
What some of these mean:
- **objects-set-down:** put cups, phones and bottles down on the table, normally.
- **table-bumps:** bump the table with a hip or knee, push it, lean on it.

**Our own devices (★, 2 files), once the knocker works:**
1. Start this recording:
   ```bash
   uv run hb record --device $DEV --label self --surface table --desc servo-knocks-$MIC --seconds 60
   ```
   While it records, the hardware person runs `knock 3` over and over (see `docs/KNOCKER-SETUP.md`).
2. Then start this one:
   ```bash
   uv run hb record --device $DEV --label self --surface table --desc speaker-prompts-$MIC --seconds 60
   ```
   While it records, play our voice prompts from a second terminal:
   ```bash
   for f in assets/voice/*.wav; do afplay "$f"; sleep 1; done
   ```

## 7. Background (30–45 minutes, unattended, in the agreed slot)
This file measures how often normal venue noise fools us, so it matters a lot.
```bash
caffeinate -i uv run hb record --device $DEV --label bg --surface table --desc venue-evening-$MIC --seconds 2400
```
- `caffeinate` keeps the Mac from sleeping. Keep the laptop plugged in.
- Life goes on around the slab, but nobody knocks on it on purpose.
- `--seconds 2400` is 40 minutes. Stop early with Ctrl-C if the team needs the stick; the file is still saved.

## 8. Hand off
1. **If you recorded on your own laptop,** AirDrop everything in `data/raw/` to the main laptop's `Team-14/data/raw/`. **Don't commit recordings to git.**
2. **Send the Codex driver a message:** "N knock files, M other files, background done, `stick_gain_db` = X."
3. **Training** then follows `docs/TRAINING.md`. It takes about 10 minutes, and you can run it yourself on the main laptop.
