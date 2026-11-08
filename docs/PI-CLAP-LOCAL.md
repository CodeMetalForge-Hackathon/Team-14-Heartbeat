# Local CLAP on this Raspberry Pi

The clone uses the current `streaming-clap` implementation merged into `main`.
Use CLAP, not the default `rubble`/YAMNet classifier, for this setup. The core
Python 3.11 environment and the optional model service remain separate.

## Model service

```bash
cd ~/Team-14
bash scripts/clap_pi.sh --allow-download
```

The helper selects the existing `profiles/clap-distress.yaml` and pins the
isolated service to the ARM64 CPU-only PyTorch 2.9.1 wheel from PyTorch's public
CPU index. The ordinary unpinned resolver can select a newer CUDA dependency
set on ARM64; this wrapper avoids that for the Pi. It does not change the core
project dependencies, event contracts, model, prompt set, or thresholds.

After model weights are cached, omit `--allow-download`. Wait for `CLAP ready`
before launching the listener. The service binds only to `127.0.0.1:8767` and
does not open a microphone, serial device or motor. Stop it with Ctrl+C. Only
one service should use that port.

## Feed live signals (human-operated)

For an M5Stick running the repository's `stickmic v1` firmware, in a second
terminal:

```bash
cd ~/Team-14
uv run --no-sync --extra ml hb listen --device stick --profile clap-distress --debug
```

Use `--device stick:/dev/ACTUAL_PORT` to select a known microphone bridge.
Do not use the Auriga rover serial port as a microphone. For a USB audio
microphone, run `uv run --no-sync --extra ml hb devices`, then substitute its
input index for `stick`. Do not run two listeners on the same input.

The source streams 16kHz mono float32 PCM. Onsets send in-memory clips to
`/classify`; continuous three-second windows go to `/observe` once per second.
The service loads CLAP once and resamples for its feature extractor. Knocks
and vocal observations retain separate event paths. Speech, shouting, crying,+screaming and breathing are sound categories; ASR is disabled in this profile.
Nothing in `hb listen` controls rover motors or confirms a person.

Model inference can be smoke-tested without hardware by sending synthetic
PCM to the service. Such a check verifies the streaming protocol, not
recognition accuracy. See [DISTRESS.md](DISTRESS.md) for human test conditions,
gating, timing and limits. Pi throughput and real microphone detection still
need measurement with the actual input.

## Installation verification (2026-11-07)

- Upstream baseline: `bb7ffe7` (latest main when checked through GitHub CLI).
- Hardware-free suite: `786 passed`, with one existing Starlette/httpx warning.
- CLAP loaded on CPU; `/health` identifies `laion/clap-htsat-unfused` and the
  monitor-enabled profile. Requests to `/classify` and `/observe` returned
  HTTP 200 with finite scores. Initial inference timings were 461ms and 505ms,
  respectively; these are smoke measurements, not sustained-load benchmarks.
- The real `hb listen --wav ... --profile clap-distress --debug` path processed
  a six-second fixture containing the tracked `q_hurt.wav` voice prompt and
  emitted SPEECH observations. This checks transport and inference, not field
  accuracy. No microphone, serial device, speaker or motor was opened.
