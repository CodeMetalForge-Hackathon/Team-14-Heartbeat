# Audio-triggered rover investigation

`hb patrol --profile clap-distress` connects classified audio to the existing
route controller. It uses relative drive/turn instructions in `profiles/route.yaml`,
not GPS, mapping, obstacle avoidance, or joystick navigation. Each route leg
needs a human-checked path. The operator can pause, E-STOP, and resume using the
existing dashboard or terminal controls.

## Behavior

1. **MOVING:** capture continuously. A SCREAM, SHOUT, CRYING, enabled-ASR help
   phrase, or three accepted classified knocks within two seconds requests STOP.
   Normal speech and breathing do not request a stop. The passthrough classifier
   cannot trigger knock stops. Moving audio is never a dialog answer.
2. **STOPPING:** invalidate queued movement and bypass any outstanding DRIVE
   wait with the existing STOP transport. Keep audio capture running. Repeated
   detections cannot issue duplicate automatic stops. Only a successful STOP
   acknowledgement begins settling.
3. **SETTLING:** wait two stream seconds after the acknowledged stop. Retain
   raw audio, but discard analysis windows containing travel or settling noise.
4. **VERIFYING:** collect eight seconds of fresh stationary audio. CLAP uses
   three-second windows every second; the first result needs a full new window
   plus inference time. Late pre-stop results and own prompts cannot count.
   Overlapping windows are not independent confirmations. A fresh qualifying
   cue leads to the existing deterministic challenge-response dialog.
5. **REVIEW:** hold position after the check, including an unsuccessful check.
   No fresh cue is recorded as `UNCONFIRMED`, and a monitor error as `AUDIO_ERROR`.
   An initial possible-person signal remains `POSSIBLE` even if nobody answers.
   Only the dialog engine can produce `CONFIRMED`.
6. **Resume:** the operator explicitly advances to the next route gap. An
   interrupted leg is not replayed. Its result is marked `arrival_unverified`;
   later relative movements start at the actual stopped position, so the
   operator must review the remaining route before resuming. A route label is
   not a measured location of either rover or person.

Scheduled route stops also settle, verify, and hold for review. STOP failure or
timeout latches **FAULT**, requests E-STOP, and refuses resume. Audio capture
continues while the source is healthy. An audio session ending still invokes
the existing rover shutdown/STOP path. E-STOP and Abort retain their existing
session-ending behavior; neither can be undone by a late classifier result.

Timings, burst size, freshness limit, and enable switch are under
`rover.audio_stop` in `profiles/rubble.yaml`. The feature defaults off there and
on in `clap-distress`. `hb listen` and `hb run` remain observation-only with
respect to rover movement. No optional model dependency was added to the core.

For stationary automatic spoken responses before the rover is connected, use
`hb run --auto --profile clap-distress`; see [VOICE-RESPONSE.md](VOICE-RESPONSE.md).
Patrol keeps `dialog.auto_challenge` disabled and owns its STOP/settle/verify
sequence, so the stationary vocal response hook does not issue prompts during
travel or bypass this controller.

## Try the state machine without hardware

From the repository root:

```bash
uv run hb patrol --dry-run --profile rover-audio-demo --dashboard
```

Open the dashboard at the address printed by the server. This uses a fake rover,
scripted SCREAM observations, and a simulated answering survivor at Gap 1. No
microphone, serial port, network inference, or model weights are used. Watch
MOVING → STOPPING → SETTLING → VERIFYING → LISTENING → REVIEW. Click Resume to
advance. Later gaps have no scripted distress signal and also hold for review.
This demonstrates the controller, not CLAP recognition accuracy. The synthetic
source has a finite duration; restart the demo if it finishes while held.

## Try physical hardware (human-operated)

Terminal 1, start the existing service (add `--allow-download` only if needed):

```bash
uv run --python 3.12 --script tools/clap_service.py --profile profiles/clap-distress.yaml
```

Terminal 2, after checking the route and substituting the actual rover's stable
serial ID for `ACTUAL_ROVER_ID` below. The connected NEO-M9N GPS currently uses
`/dev/ttyACM0`; do not select that GPS port as the rover:

```bash
uv run hb patrol --profile clap-distress --rover /dev/serial/by-id/ACTUAL_ROVER_ID --device stick --knocker manual --dashboard
```

`--knocker manual` displays prompts for a human to carry out. Select the already
configured physical prompter when ready to test automatic spoken/knock prompts.
Do not run another microphone listener on the same stick. Terminal controls:
`p` + Enter pauses; `r` + Enter resumes after review; `e` + Enter E-STOPs;
`q` + Enter aborts. Ctrl-C also stops the rover.

First check STOP acknowledgement, motor settling, fresh stationary evidence,
and explicit resume. Then compare human signals against motors, bumps, speech,
and silence. Timing and score settings require measurement on the Pi/rover;
the software tests do not establish acoustic accuracy or stopping distance.

## Evidence and implementation

`patrol_cue` records the triggering/fresh cue's label, relative score, capture
start/end, transcript, and matched phrases. Patrol snapshots and gap results
retain the trigger and fresh cue for review. The black box records these events
and the continuous `raw.wav`; use the capture offsets to inspect vocal evidence.
Existing onset clips remain in `clips/`. Vocal clips are not separately exported.
No GPS position or source direction is invented.

The controller lives in `heartbeat/patrol.py`; `AudioMonitor` resets buffers
and rejects old inference generations on patrol phase changes. The audio and
Detection contracts remain unchanged. Added patrol states are STOPPING,
VERIFYING, REVIEW, and FAULT; snapshots add `audio_stop_enabled`, `trigger`,
`verified_cue`, and `fault`. REVIEW reports `paused=true` for existing controls.

Hardware-free checks:

```bash
uv run pytest -q tests/test_patrol.py tests/test_patrol_audio.py tests/test_audio_monitor.py
uv run pytest -q
```
