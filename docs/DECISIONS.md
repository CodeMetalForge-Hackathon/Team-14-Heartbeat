# Decisions rover: build and operation

Decisions receives a fresh camera frame and rover observations, then recommends
one finite action. Local code validates the choice and owns every motor command.
Only the deterministic challenge–response dialog can report `CONFIRMED`.

**The Pi 5 does not need to be connected to build this feature.** Hardware is
needed to run it. The current encoder disagreement and intermittent ultrasonic
readings remain unresolved: keep motion unarmed until those faults are resolved.

## Local macOS webcam preview

Use the Mac webcam without saving a photo or connecting the Pi or rover:

```bash
cd /Users/azem/code-metal-forge/Team-14
uv run --env-file .env.local hb decisions-preview \
  --webcam --front-cm 80 --allow-turns
```

Open `http://127.0.0.1:8002/` in Safari or Chrome, click **Start camera**, and
allow camera access. Point the camera at the scene and click **Capture & decide**.
Each click sends one JPEG to the real Decisions API and displays the exact frame,
chosen action, probabilities and API latency. Capture again for another decision.
There is no automatic upload loop. The microphone is never requested.

The first recommendation can be `FORWARD_SHORT` when the image and example
distance support it. This preview never executes movement. The distance field
is editable hypothetical telemetry, not a live ultrasonic reading; turning is
an optional choice. Stopping the camera stops its local stream and discards a
pending result; it cannot recall an image already sent to the API.

The page listens only on loopback and uses port 8002 to coexist with the rover
dashboard on 8001. Use `--port 8003` if needed. The API key stays in the local
Python process. Ctrl-C closes the server; use **Stop camera** or close the tab
to release the webcam. If access is denied, allow the browser under macOS
**System Settings → Privacy & Security → Camera** and in the site's permissions.

## Local macOS saved-image preview

You can exercise the real Decisions API on the Mac without the Pi, rover,
microphone, speaker or CLAP service. Export a scene photo as JPEG, then run:

```bash
cd /Users/azem/code-metal-forge/Team-14
uv run --env-file .env.local hb decisions-preview \
  --image "$HOME/Desktop/scene.jpg" --front-cm 80 --allow-turns
```

Replace the image path with your JPEG. Keep it below the configured size limit
(1 MiB by default). `--front-cm 80` is explicitly an example sensor value;
the command does not read the ultrasonic sensor or open any hardware. The image
is uploaded in one real API request using the existing local key. Output includes
the selected action, probabilities, `api_latency_s`, and `motion_executed: false`.
Repeat with a different photo or example range to compare choices. A `HOLD`
answer is valid, especially for ambiguous saved imagery.

The preview uses `preview_timeout_s` (15 seconds by default), independently of
the live controller's shorter deadline. `--timeout 5` overrides that socket
timeout for one preview. Its timing covers the API request, not live camera,
sensor, audio or movement latency. It does not validate the full patrol loop.

The live `hb patrol --decisions` camera adapter still requires Raspberry Pi OS
and `rpicam-vid`; the Mac webcam page is a recommendation preview only.
No movement flag exists on `decisions-preview`.

## Runtime connections

- Pi 5 CSI camera, with Raspberry Pi OS `rpicam-vid` available.
- Pi USB to the Auriga rover controller, with the rover's motor power connected.
- USB contact microphone/stick to the Pi, plus a working speaker for prompts.
- Network access to the Decisions API and the selected CLAP inference service.

Use the existing [Pi setup](PI.md) and [rover firmware setup](../firmware/rover/README.md).
This feature adds no Python dependencies and does not install a CLAP model on Pi.
The existing CLAP service currently runs on the Mac at port 8767. If it stays
there, change `classifier.clap.endpoint` in the Pi's selected profile to a
reachable Mac LAN address, for example `http://192.168.1.20:8767`.
Pi `127.0.0.1` points to the Pi, not the Mac. Ensure the service listens on an
appropriate reachable interface. Do not assume PyTorch CLAP runs locally on Pi.

## Credentials and firmware

The selected API key is already saved in the original development repository's
ignored `.env.local`. It expires seven days after creation. Never print its value,
put it in a command argument, or commit it. The client reads `OPENAI_API_KEY` from
the process environment; `uv run --env-file .env.local` supplies that environment.
The build does not transfer credentials to the Pi. Provision the runtime through
the approved secure credential flow when ready; a Mac-only file is not on the Pi.

The Auriga must have the updated Heartbeat sketch. It adds `CAPS`, `STEP`, and
`PIVOT`. The host requires `CAPS guarded=1`; old firmware fails stopped without a
fallback to unguarded `DRIVE`/`TURN`.

These are **human-only** build/upload examples, from the repository root:

```bash
arduino-cli compile --fqbn arduino:avr:mega:cpu=atmega2560 firmware/rover
arduino-cli upload -p /dev/serial/by-id/ROVER_ID -b arduino:avr:mega:cpu=atmega2560 firmware/rover
```

Replace `ROVER_ID` with the Auriga's actual port. Close serial owners before an
upload. See the firmware guide for board/core and Makeblock library setup.
Compiling does not upload. No firmware upload or physical operation was performed
while building this feature.

## Start in observation mode

Resolve the owned rover and microphone separately using their actual Pi device
identities. Prefer `/dev/serial/by-id/...`; Mac `/dev/cu.*` names do not apply.
Replace `ROVER_ID` and `MIC_ID` below. Only one process may own each serial port.
Ultrasonic is on port 6; the port 9 line sensor is unused.

The following command is for a human to run on the prepared runtime machine:

```bash
uv run --env-file .env.local hb patrol \
  --decisions --profile clap-knocks \
  --rover /dev/serial/by-id/ROVER_ID \
  --device stick:/dev/serial/by-id/MIC_ID \
  --sensor-port 6 --knocker speaker --dashboard --port 8001
```

The default is **OBSERVATION ONLY**: camera/API observations and proposed actions
are real, but proposed movement is not executed. This still requires the rover,
camera, microphone, speaker, networking and credentials. The dashboard defaults
to loopback. From the Mac, use an SSH tunnel such as
`ssh -L 8001:127.0.0.1:8001 USER@PI_HOST`, then open `http://127.0.0.1:8001`.

Optional flags:

| Flag | Meaning |
| --- | --- |
| `--decisions-seconds 120` | Total navigation budget; default 120 seconds, maximum 600. |
| `--camera-command /usr/bin/rpicam-vid` | Override the camera executable path. |
| `--arm-motion` | Explicitly permit locally guarded forward actions. |
| `--allow-turns` | Offer left/right choices after physical direction and side-clearance verification. |

`--dry-run` is incompatible with `--decisions`. The old fake rover cannot validate
the new guarded sensor/encoder behavior. Observation mode is the available runtime
mode with motor actions disabled; it still makes real API requests.

Do not add `--arm-motion` while the encoder/range faults remain unresolved. Once
hardware readiness is established, arming is an explicit operator choice at launch.
Turning requires the additional `--allow-turns` opt-in and verification that
`positive_turn_is_right` matches the actual assembly. A front sonar reading cannot
establish side clearance or detect drops.

## What happens during a run

The rover stops and settles before capturing an observation. At most one request
is in flight, and motors remain stopped while waiting for the model. Choices are
`FORWARD_SHORT`, optional `TURN_LEFT_SHORT`/`TURN_RIGHT_SHORT`, `HOLD`, and
`STOP_AND_LISTEN`; unavailable movement choices are excluded locally.

The model cannot set PWM, distance, angle, duration, thresholds or confirmation.
Profile defaults assign a 3 cm forward step, a 10 degree turn, 25% speed, and a
1000 ms action deadline. These are requested bounds, not measured travel accuracy.
Freshness, confidence, state and sensor checks run again before motion dispatch.
The model's score is not a calibrated probability that movement is safe.

Firmware requires fresh front range and checks it during both guarded movement
types. Unknown/too-close range fails stopped with no automatic turn or restart.
Both encoder targets must complete; missing feedback cannot become timed-pulse
success. Per-wheel cutoff, direction checks, stall protection, slew and watchdog
remain active. The hard guarded deadline is at most 2000 ms; expiry is an error.

Camera/API errors, stale responses, action failures and session expiry leave
motors stopped; review the reported state before continuing. Old/canceled results
cannot authorize a later action. A navigation budget ending does not interrupt an
already active stationary verification/dialog.

An accepted classified knock interrupts movement through the existing independent
STOP path, then settling, fresh audio verification and deterministic dialog.
Moving audio never counts as a dialog answer. Investigation holds for operator
review; a proposal or camera image cannot confirm a survivor.

Use `p` + Enter to pause, `r` + Enter to resume when permitted, `q` + Enter to
abort, or `e`/Space + Enter for E-STOP. Dashboard E-STOP and Ctrl-C also request
STOP. E-STOP remains latched. If communication cannot deliver STOP, use the rover's
physical power switch. A STOP acknowledgement disables output but cannot prove
that chassis coasting has ended.

The black box records sanitized decisions, outcomes, errors and the selected
profile, including its mission and action descriptions. It does not log raw
camera images, complete API request bodies or API keys. Camera frames are sent
to the Decisions API; this is not an offline mode.

No tests were created or run for this work. Validation is limited to source
review, syntax checks and firmware compilation; no upload, physical run or live
Decisions request was performed. Camera operation, model/API latency, network reachability,
sensor behavior and braking remain unvalidated on the assembled rover. This
feature provides no mapping, systematic coverage or return-to-start guarantee.
