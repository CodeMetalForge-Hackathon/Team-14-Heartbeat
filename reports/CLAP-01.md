# CLAP-01 report

## Result

Added opt-in CLAP classification through the existing onset/detection pipeline.
Live capture and level meters continue during inference. No WAV file is needed
between capture and classification. The model and text embeddings load once in
an optional service that can run on the Mac or, after environment validation,
on a Jetson reached from the Pi over the local network.

The core project retains its original dependencies and default classifier.
This fulfills the user's requested CLAP option while isolating its PyTorch
requirement from the repository's usual CPU-only Pi runtime. No model weights
or recordings are committed. No microphone, serial, speaker, or motor command
was executed by the coding agent.

## Human commands

From the integration repository root, start the service in one terminal:

```bash
uv run --python 3.12 --script tools/clap_service.py --profile profiles/clap.yaml
```

Then start the microphone listener in another:

```bash
uv run hb listen --device stick:/dev/cu.usbserial-81520161EE --profile clap --debug
```

See `docs/CLAP.md` for startup, separated test cases, dashboard use, offline WAV
replay, tuning, and Pi/Jetson deployment. A cached local model is the default;
`--allow-download` permits the initial model download when needed.

## Validation

- Baseline: 685 passed, 15 skipped.
- Final full suite: 712 passed, 15 skipped. The existing Starlette/httpx
  deprecation warning remains. Skips cover optional model/runtime cases.
- New tests use fakes and in-process HTTP handling, with no hardware, downloaded
  models, or network. They cover response/audio identity, malformed results,
  service failure, queue overload, result deadlines, continued capture/meters,
  retained impact timestamps, dialog watermarking, and incomplete-answer reset.
- A real cached CLAP model processed the user's saved M5Stick file through
  `hb listen --wav ... --profile clap --realtime` with no error events.
- A second measured replay produced nine candidate detections, nine positive
  scores, and 51 meter events over five seconds. Median impact-to-result latency
  was 0.944 s; range 0.928–0.960 s. The last HTTP classification took 58 ms.
  These measurements apply to this development Mac and recording only.
- Debug-output smoke check prints the top descriptions for rejected candidates.
- The user's live mixed-sound test confirmed that the M5Stick and CLAP stream
  ran continuously. All shown candidates were rejected at the 0.60 cutoff; the
  log did not identify which events were actual knocks. This is not an accuracy
  validation. Separate knock and non-knock trials remain necessary.

## Contract changes and limitations

- `ClassifierConfig` gains optional `clap` settings; the registry gains `clap`.
  The CLAP profile explicitly selects it; `rubble` keeps its previous behavior.
- `Onset`, `Detection`, `sound` event fields, and dialog APIs are unchanged.
- Live classifiers opting into `async_inference` use a bounded single worker.
  Dialog time is held before outstanding candidates. Offline WAV and simulation
  replay stays synchronous and deterministic.
- CLAP startup errors are explicit rather than falling back to passthrough.
  An inference failure/timeout/backlog emits an error and stops the audio
  session; the dialog resets instead of confirming a partial answer. This is
  an explicit exception to the general keep-running fallback rule.
- The detector still supplies 0.975 s of audio, including 0.875 s after onset.
  Short-clip accuracy may differ from the successful five-second tester. CLAP
  repeats/pads inputs internally. Overlapping clips may contain nearby impacts.
- Positive descriptions are combined into one relative score. The 0.60 cutoff
  is uncalibrated. No claim of human/servo distinction or rescue reliability is
  made. Confirmation remains with the deterministic dialog.
- The service checks fixed audio size, finite sample bounds, model/prompt
  identity, and request digest. It allows one concurrent inference and does not
  create an unbounded hidden inference queue.
- Live acoustic accuracy, prolonged stick reliability, Jetson compatibility,
  Pi-to-Jetson latency, real prompt gating, and rover hardware are unverified.
- Work was performed on `codex/streaming-clap` in
  `/private/tmp/team14-clap-integration`, based on `603bb10`. macOS denied access
  to the user's Desktop checkout. A patch is provided for that checkout; it was
  not changed by the agent.
