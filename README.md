# Heartbeat

Heartbeat finds people trapped under rubble using a contact microphone, a servo knocker, and a speaker. **Noise doesn't answer. People do.** The sentinel will ask for knock responses and use deterministic challenge–response matching to distinguish a person answering from random noise. This first stage provides audio capture, transient detection, labeled recordings, and reproducible synthetic audio on a laptop or Raspberry Pi 5 CPU. The `none` classifier accepts every detected transient; its `KNOCK` label is a data-collection baseline, not evidence of a person.

## Quickstart

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then run these commands from the repository root. Python is pinned to 3.11; `uv` manages the environment.

```sh
uv sync
uv run pytest -q
uv run hb synth --out /tmp/hb_scene.wav
uv run hb listen --wav /tmp/hb_scene.wav
```

The synthetic WAV has a `/tmp/hb_scene.wav.json` truth file containing sound times and kinds. Replay runs as fast as possible by default; add `--realtime` to pace it like a live input. Knocks and distractors can both be printed because P01 has no trained classifier.

## Try a real microphone

These commands access hardware and must be run by a human. Grant terminal microphone permission on macOS. Linux may require the system PortAudio library (`libportaudio2` on Raspberry Pi OS).

```sh
uv run hb devices
uv run hb listen --profile rubble
uv run hb listen --device 1 --profile rubble
uv run hb record --label knock --surface concrete --desc light-taps --seconds 15 --device 1
uv run hb record --label bg --surface concrete --desc room-noise --seconds 30 --device 1
```

Replace `1` with the input device index shown by `hb devices`, or omit `--device` to use the default. Press Ctrl-C to stop listening or a recording without `--seconds`. Recordings use 16 kHz mono PCM16 WAV and are saved under `data/raw/`, with normalized names and an automatically incremented suffix. The countdown and peak meter help collect a usable signal; the recorder warns about clipping or very quiet audio. Input devices that reject 16 kHz are opened at their default rate and resampled.

## Configuration and integration

For automatic spoken challenges after knock bursts or possible screams,
shouts and crying, see [Automatic voice response](docs/VOICE-RESPONSE.md).
The stationary mode uses `hb run --auto --profile clap-distress`; a working
audio output and the CLAP service are required. Rover movement is a separate
integration step.

For a bounded USB GNSS connection/fix check, see [NEO-M9N GPS](docs/GPS.md).

All operational settings and synthetic sound parameters are in `profiles/rubble.yaml`, with comments describing each value. `--profile` accepts a name or YAML path; custom profiles inherit omitted rubble settings, reject unknown defined keys, and preserve the reserved `dialog`, `prompter`, and `server` dictionaries for later stages.

`AudioSource` yields 256-sample `AudioBlock(t0, x)` hops (the last file hop may be shorter). Audio is float32, mono, and within [-1, 1]. Stream time is derived from sample counts. The detector filters with a stateful second-order high-pass, estimates a rolling percentile noise floor, and emits each onset after its 15,600-sample raw-audio clip is available. Clips begin 100 ms before the detected onset, with zeros where a file boundary has no audio. Peak measures the first 50 ms of raw audio after the onset. Gates mark self-generated sounds without hiding them from the log.

The detector requires the SNR threshold, minimum level, and refractory interval. It also re-arms only after SNR falls below `onset.rearm_db` (6 dB by default) or the level falls below `min_dbfs`. This hysteresis is an explicit refinement of P01's trigger conditions to meet its double-hit requirement: a ringing tail must not become another onset when the refractory interval expires. The shared audio, detection, and event contracts are unchanged.

`EventBus` synchronously sends `Event` objects to subscribers and isolates subscriber failures. `Event.to_dict()` returns the JSON event schema; `Event._clip` carries a clip separately from serialized data. `Pipeline` exposes `on_block` and `on_sound` callback lists for the later dialog engine; `on_block` receives the end-of-block stream time. `register(name, factory)` adds classifiers without changing callers. Missing or unavailable classifiers warn and fall back to `none`.

Tests use synthetic audio and fake device backends. Microphone quality, USB adapter behavior, PortAudio setup, and Raspberry Pi performance still require hardware validation. The Apache-2.0 license is in `LICENSE`.
