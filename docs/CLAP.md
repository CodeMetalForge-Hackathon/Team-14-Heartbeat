# Streaming CLAP

Select `--profile clap` to replace the classifier in the existing Heartbeat
pipeline. The M5Stick keeps streaming; Heartbeat detects each impact and sends
its audio directly from memory to a separate CLAP process. No recording command,
Enter key, or intermediate WAV file is needed. The existing dialog, dashboard,
gating, and black-box interfaces receive normal `Detection` events.

## Start on the Mac

Run these commands from this repository's root in two terminals. A person must
run commands that use microphones, serial ports, speakers, or motors.

Terminal 1: load and warm the model once, then leave it running:

```bash
uv run --python 3.12 --script tools/clap_service.py --profile profiles/clap.yaml
```

This defaults to cached weights and performs no Hugging Face network requests
for the model. If the model is not cached, add `--allow-download` for the first
start. Python dependencies may still need downloading through uv. If port 8766
is already occupied by this service, use the existing process rather than
starting a second one. The service never opens the microphone or serial port.

Terminal 2: close other applications using the stick, then start listening:

```bash
uv run hb listen --device stick:/dev/cu.usbserial-81520161EE --profile clap --debug
```

Use your current serial-port name if it changes. The console prints `KNOCK` or
`REJECTED` for each detected impact; `--debug` adds the top three descriptions
and the stick's initial serial diagnostics. Silence usually only produces meter
lines. Ctrl+C stops listening. Terminal 1 can stay running for the next trial.

For the existing dashboard and dialog with manual prompts:

```bash
uv run hb run --device stick:/dev/cu.usbserial-81520161EE --profile clap --knocker manual --dashboard
```

The full `hb run` still writes its usual black-box logs and audio for audit.
Classification does not wait for those files. For existing rover workflows,
select the same profile; only the classifier changes. Rover behavior has not
been verified with CLAP on hardware.

## Check recognition before adjusting the threshold

Do each condition separately with the stick in the intended position:

1. Stay quiet for ten seconds.
2. Make five normal knuckle knocks, about two seconds apart, without speaking.
3. Repeat with five gentle knocks.
4. Try five accidental table bumps or object set-downs.
5. Talk for ten seconds, then test servo and rover noise separately.

Compare expected impacts with printed events, and note false `KNOCK` results.
A missing event means the onset detector did not emit a candidate. A `REJECTED`
event means a candidate reached CLAP but its combined positive score was below
the cutoff. The initial cutoff is 0.60 in `profiles/clap.yaml`; changing it needs
both knock and non-knock trials. Mixed sounds without marked times do not give
a reliable accuracy measurement.

The score combines the two human-knock descriptions and is relative to all ten
descriptions. It is not a probability that a human is present. Text mentioning
a person cannot establish human versus servo origin. The deterministic
challenge-response dialog still makes the confirmation decision.

Saved recordings can exercise the same streaming code without hardware:

```bash
uv run hb listen --wav /absolute/path/to/recording.wav --profile clap --realtime --debug
```

Without `--realtime`, WAV processing is synchronous and runs as fast as
classification allows, avoiding artificial queue overflow during fast replay.
Only one listener should use a given CLAP service at a time.

## Timing and failure behavior

Each candidate retains the existing 15,600-sample, 16 kHz clip contract: 0.1 s
before the impact and 0.875 s after it. Consequently, total latency includes at
least 0.875 s plus model processing and transport. This is continuous capture
with short-clip classification, rather than an internally streaming model.
The CLAP processor resamples to 48 kHz and repeats/pads these clips to its model
input length. Short-clip recognition must be checked separately from the earlier
five-second tester.

The model loads once and caches text embeddings. A separate inference worker
keeps audio capture, detection, and meters moving. Results retain the original
impact times; response windows wait for outstanding results before closing.
`max_pending`, `max_lag_s`, and `timeout_s` bound queueing and delays. If inference
fails or cannot keep up, Heartbeat emits an error and stops this audio session;
unfinished dialog state is reset. It does not silently convert unclassified
impacts into knocks. Restart listening after addressing the error. This is a
deliberate exception to the usual classifier fallback behavior.

In one five-second real-time replay of the saved M5Stick knock recording on the
development Mac, nine candidate windows were classified, 51 meter events were
published, and there were no error events. Median latency was 0.944 s; maximum
was 0.960 s. All nine candidates exceeded the threshold, which is a functional
smoke test, not an accuracy result. The file contains clipping, and overlapping
candidate clips may contain more than one impact.

## Pi rover and Jetson

Keep audio capture, the dialog, and rover control on the Pi. Run this CLAP service
on the Mac or Jetson and set `classifier.clap.endpoint` in the Pi's profile to
that computer's LAN address, such as `http://192.168.1.50:8766`. Start the service
with `--host 0.0.0.0` on a trusted shared network. Both copies need identical
model, revision, and prompt settings. Audio clips travel over this local HTTP
connection; the default loopback setup keeps them on one computer.

On a Jetson, use a Python environment with a PyTorch build compatible with its
JetPack installation, then run `python tools/clap_service.py --profile
profiles/clap.yaml --host 0.0.0.0`. Set `device: cuda` in the service profile only
after CUDA works in that environment. Do not use the generic uv script command
to replace the Jetson's PyTorch installation. Actual Jetson compatibility and
latency depend on the board and installed software and remain unverified.

The core Python 3.11 project adds no PyTorch, Transformers, or CUDA dependency.
Those dependencies are isolated in the optional service. The normal `rubble`
profile still selects the existing auto/YAMNet/heuristic path. Switching back
requires restarting the listener with `--profile rubble`.
