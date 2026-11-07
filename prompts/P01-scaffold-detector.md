# P01: scaffold, audio input, onset detector, recorder

Read `AGENTS.md` first. **Lane A, first task.** P02 runs in parallel and touches only `firmware/` and `tools/knocker_test.py`.

## Goal
When you're done, three commands work, and everything is tested with synthetic audio:
- `uv run hb listen` prints every knock-like sound from the microphone with its time and loudness.
- `uv run hb record ...` saves labeled training audio.
- `uv run hb synth ...` makes test audio.

## Files you own
- `pyproject.toml`, `.python-version`, `.gitignore`, `README.md` (do **not** touch the existing `LICENSE`)
- `heartbeat/__init__.py`, `config.py`, `events.py`, `audio.py`, `onset.py`, `classify.py`, `synth.py`, `runner.py`, `cli.py`
- `profiles/rubble.yaml`
- `tests/`
- `data/.gitkeep`

## Build
1. **Project.**
   - Make a uv package pinned to Python 3.11 that installs the console script `hb = heartbeat.cli:main`.
   - Runtime dependencies: numpy, scipy, sounddevice, soundfile, pyyaml, pyserial.
   - Dev dependencies: pytest.
   - The repo already has an Apache-2.0 `LICENSE`. Keep it, and set `license = "Apache-2.0"` in `pyproject.toml`.
   - `.gitignore`: `.venv`, `__pycache__`, `runs/`, `data/raw/`, `data/clips/`, `models/*.tflite`, `.DS_Store`.
   - `README.md`: one paragraph on the mission plus a quickstart. Other prompts will extend it.
2. **`config.py`.**
   - Load a profile YAML into dataclasses, one per section: `audio`, `onset`, `classifier`, `dialog`, `prompter`, `server`.
   - Leave `dialog`, `prompter` and `server` as free-form dicts for now; later prompts will define them.
   - Raise a clear error on unknown keys in the sections you define.
   - Provide `load_profile(name_or_path)`.
3. **`events.py`.**
   - Write the `Event` dataclass and a thread-safe `EventBus`. `publish` is synchronous to its subscribers.
   - A subscriber that raises becomes an `error` event, and publishing continues.
   - Add `to_jsonable()` to convert numpy scalars and arrays to Python types.
   - Add `ConsoleSink` for pretty printing.
4. **`audio.py`.**
   - `AudioBlock(t0, x)`.
   - `MicSource(device=None, block=256)`:
     - Use a sounddevice `InputStream` whose callback feeds a bounded queue.
     - If the consumer lags, drop the oldest blocks, count the overruns and publish a warning.
     - **If the device can't open at 16 kHz** (common with USB adapters), open it at its default rate and resample to 16 kHz with `scipy.signal.resample_poly`.
     - Compute `t0` from samples delivered.
   - `WavSource(path, realtime=False)`:
     - Accept any rate and channel count, converting to 16 kHz mono.
     - `realtime=True` sleeps to pace playback like a live mic.
   - `list_devices()`.
5. **`onset.py`: `OnsetDetector(cfg)`.**
   - **Signal path:**
     1. A 2nd-order Butterworth high-pass at `highpass_hz`, using `sosfilt` with its state carried across blocks.
     2. RMS level in dBFS for each 256-sample hop.
     3. A noise floor equal to the 20th percentile of the last `floor_window_s` of hop levels. Exclude hops within 0.3 s after an onset from the floor.
   - **Trigger** when all three hold:
     - `level - floor >= threshold_db`
     - `level >= min_dbfs`
     - at least `refractory_s` since the last onset
   - **Emitting:**
     - Emit each `Onset` once its 15600-sample clip (starting 0.1 s before `t`) is complete. Keep a ring buffer for this.
     - Set `peak` to the max absolute value in the first 50 ms after onset, and `snr_db` to level minus floor.
   - **Gating:** `gate(t_start, t_end)` marks onsets inside the interval `gated=True`. Allow `t_end=inf`; a later call closes the gate. Keep a short list of gate intervals.
   - Expose `level_db` and `floor_db` for the meter.
6. **`classify.py`.**
   - Define the `Detection` dataclass (see AGENTS.md) and a `Classifier` protocol.
   - Add `PassThroughClassifier`: score 1.0, `is_knock=True`, `classifier="none"`.
   - Add a `make_classifier(cfg)` registry (`register(name, factory)`), so P05 can add `heuristic`, `yamnet`, `head` and `auto` without editing callers.
   - If a requested classifier is missing, fall back to passthrough with a warning.
7. **`synth.py`.** Seeded, deterministic generators.
   - Sounds:
     - `knock()`: 2–3 damped sinusoid modes, 150–1200 Hz, decay 10–40 ms, plus a 2 ms click
     - `footstep()`: low thump, 50–120 Hz, decay ~60 ms
     - `clap()`: broadband burst
     - `speech_like()`: band-limited noise, 300–3000 Hz, amplitude-modulated at about 4 Hz
     - `drop()`: an object bouncing 2–4 times with shrinking gaps
     - `background(level_dbfs)`: pinkish noise
   - `scene(duration_s, events=[(t, kind, gain_db)], bg_dbfs=-50, seed)` returns `(audio, truth)`, where `truth` lists `{t, kind}`.
   - P03 and P04 will reuse these generators.
8. **`runner.py`: `Pipeline(source, detector, classifier, bus, profile)`.**
   - One processing thread consumes blocks.
   - Publish `level` at 10 Hz.
   - Publish one `sound` event per onset after classification. Pass the clip array through `bus.publish(..., _clip=array)`; sinks that don't want it ignore it, and it is never serialized.
   - Provide hook lists `on_block(t)` and `on_sound(det)`, so P03 can attach the dialog engine without rewriting the loop.
   - `stop()` shuts down cleanly on Ctrl-C.
9. **`cli.py`** (argparse).
   - `hb devices`: list devices and mark the default.
   - `hb listen [--device D | --wav PATH] [--profile rubble] [--realtime]`
     - Print one line per sound, for example `12.48s  KNOCK  snr 23.1 dB  peak 0.41  (none)`.
     - Print gated or rejected sounds dimmed.
     - Print a one-line level bar every second.
   - `hb record --label {knock,other,bg,self} --surface S --desc D [--seconds N] [--device D]`
     - Write 16-bit PCM to `data/raw/<label>__<surface>__<desc>__<NN>.wav`. `NN` auto-increments. Normalize `surface` and `desc` to lowercase-with-dashes.
     - Show a countdown and a live peak meter.
     - At the end, warn if the audio clipped (peak > 0.99) or is too quiet (peak < 0.02).
     - Without `--seconds`, record until Ctrl-C.
   - `hb synth --out PATH [--seconds 60] [--seed 1]`: write a scene WAV with knocks and distractors, plus `PATH.json` holding the ground truth.
10. **`profiles/rubble.yaml`.** Include every tunable with a one-line comment. Starting values:
    - `highpass_hz: 60`
    - `threshold_db: 12`
    - `min_dbfs: -55`
    - `refractory_s: 0.12`
    - `floor_window_s: 3.0`
    - `classifier.name: none`
    - `classifier.threshold: 0.5`

## Tests (synthetic only, no mic, no network)
- In a scene of 20 knocks among distractors, at least 95% of knocks are detected within ±25 ms.
- 30 s of stationary background at -50 dBFS produces 0 onsets.
- A double hit 60 ms apart produces 1 onset.
- Clips are 15600 samples long and start 0.1 s before the onset.
- Stream time is exact: it comes from the sample count, not the wall clock.
- Onsets inside a gate are marked `gated`. An open-ended gate followed by a close works.
- `WavSource` correctly converts 48 kHz stereo to 16 kHz mono.
- `EventBus`: a raising subscriber produces an `error` event and other subscribers still receive events. Every event is JSON-serializable.
- `load_profile("rubble")` works, and an unknown key raises.

## Done when
`uv sync && uv run pytest -q` passes. These print knocks matching the ground truth:
```
uv run hb synth --out /tmp/hb_scene.wav
uv run hb listen --wav /tmp/hb_scene.wav
```

Commit `P01: scaffold, audio, onset detector, recorder`. In your report, include the exact commands a human should run with a real microphone, covering `hb devices`, `hb listen` and `hb record`.
