# FIX-03: fixes from the first live sentinel test

Read `AGENTS.md` first. **Lane A.** No other session is editing these files; TOOL-01 runs on another laptop and only adds new files.

## What happened (black box `runs/20261107-164254`)
The real stick and knocker were used with `--profile live` (heuristic classifier):
1. **The stick works.** Knocks through the slab are detected and classified as `KNOCK`, but **many peaks are 1.00**: the stick clips on the device, before the host gain is applied.
2. Before anyone pressed Challenge, the team knocked to test: **22 knocks in 21 s.** Those went into `BackgroundRate`, so λ jumped to 17/min. μ = 1.75 > `mu_max`, so every attempt became `TOO_NOISY`. The teammate also kept knocking during the pre-listen windows, which pushed λ_used to 60–100/min.
3. After 6 tries the engine gave up with `NO_RESPONSE`. **It never prompted once.**
4. **This exposes a real design flaw.** A survivor who knocks spontaneously, which is exactly the signal we want, is counted as background noise. That blocks us from ever challenging them.

**Minor issues:**
- The knocker auto-scan probed the Bluetooth ports (slow, and noisy warnings).
- It printed nothing when it *did* find the knocker.
- uvicorn's access log floods the console, so the dialog transcript is unreadable.

## Files you own
- `heartbeat/dialog.py`, `heartbeat/stats.py`
- `heartbeat/prompter.py`: discovery only
- `heartbeat/stickmic.py`: the MAG command only
- `heartbeat/cli.py`, `heartbeat/server.py`
- `dashboard/`
- `tools/make_voice.py`, plus the new `assets/voice/stop_and_listen.wav`
- `profiles/rubble.yaml`: the `dialog` and `prompter` sections
- tests

## Changes
1. **Candidate bursts are not background.**
   - In `LISTENING`, knocks belonging to a burst that sets `POSSIBLE` (≥ `burst_knocks` within `burst_window_s`) are marked `candidate` in the `sound`/`state` data.
   - They are **excluded from `BackgroundRate`**. Only isolated knock-like events count toward λ.
   - They stay in the log.
   - **Re-run all FIX-01 periodic-noise fixtures (0.7–5 s) and confirm 0 confirmations still.** The pre-listen and silence-after rules must still catch drips.
2. **The `all_quiet` operator command** (optional args: `operator`, `challenge=True`):
   - It records that a human declared the site quiet, emitting an `operator` event with the initials. This is the real search-and-rescue "all quiet" call.
   - It **clears the long-run noise history back to the prior**, then starts the normal challenge flow: pre-listen, then the rounds.
   - Add an **"All quiet → Challenge"** button to the dashboard, next to Challenge.
   - Keys in `hb run`: `q` is quit, so use `w` + Enter for all quiet.
3. **When the survivor keeps knocking during the pre-listen:**
   - If the pre-listen saw knocks and status is `POSSIBLE`, play a new voice prompt `stop_and_listen` ("We hear you. Stop knocking and listen. Then copy our knocks.") before the next pre-listen.
   - The dashboard text says "Hearing knocks: asking them to listen."
   - Generate the WAV with `tools/make_voice.py` and commit it.
4. **Knocker discovery:**
   - Probe only likely USB serial devices: names containing `usbserial`, `usbmodem`, `wchusbserial`, `ttyUSB` or `ttyACM`. Skip Bluetooth and debug ports silently.
   - At startup, print and emit one clear line, either `Knocker: /dev/cu.usbserial-2130 (PONG knocker v1)` or `No knocker found: using speaker knocks`.
   - Do the same for `--knocker <port>`.
5. **Stick gain on the device:**
   - Add `hb stick [--port P] mag <1..255>`. It finds the stick, uses text mode, sends `MAG n`, prints `OK MAG n`, and the setting persists on the stick.
   - Add `hb stick [--port P] info`, which prints `PONG` and the current MAG if the firmware reports it. Otherwise just `PONG`.
   - **Do not change the firmware.**
6. **Readable console:** turn off uvicorn's per-request access log (`access_log=False`, and `log_level` from the profile, default `warning`). The dialog transcript stays on the console.

## Tests
- A spontaneous burst of 22 knocks over 21 s, followed by `challenge` with a quiet pre-listen, **prompts** instead of reporting `TOO_NOISY`.
- `all_quiet` resets λ and starts a challenge. The operator event is logged.
- A survivor who knocks during the pre-listen triggers `stop_and_listen`, then confirms once they comply.
- The FIX-01 periodic fixtures still give 0 confirmations.
- `hb sim --no-survivor --noise` still gives 0 confirmations over 10 minutes.
- Discovery skips Bluetooth ports and reports the knocker it found.
- The `hb stick mag` parser works with a fake serial port.
- The full suite passes.

## Done when
- The tests pass.
- Commit: `FIX-03: live-test fixes (candidate bursts, all-quiet, discovery, stick MAG)`.
- Report the new keys, the buttons, and the exact live re-test steps.
