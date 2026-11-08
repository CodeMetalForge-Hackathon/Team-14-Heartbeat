# Decisions rover: build and operation

Decisions receives a fresh camera frame and rover observations, then recommends
one finite action. Local code validates the choice and owns every motor command.
Only the deterministic challenge–response dialog can report `CONFIRMED`.

The rover dashboard's **Camera at each stop** gallery updates automatically with
the camera frame used for a stop decision, its reason, and the frame's age at
the stop. Camera-flagged debris is labeled **Possible debris**; other stops show
**Stopped scene**. Click a photo to enlarge it. Stops before the first camera
frame explicitly report that no photo is available. The latest eight stop
snapshots are retained in memory (`rover.decisions.stop_image_history_size`).
Retries for the same condition while already stopped share one card. A new
movement or stop reason creates another card. Restarting the patrol clears
the gallery. JPEGs are served separately from event JSON
at `/api/stop-images/{id}` and are not saved in the black-box recording.

If a stop card says **Distance reading unavailable; retrying the sensor**, the
caption reports a sonar hold, not a debris detection. `cm=-1 valid=0` is an
unusable echo; the firmware does not report it as an unobstructed path. With
the rover stopped, reseat the ultrasonic module's RJ25 cable at both ends,
check controller power and sensor alignment, and use the configured port 10.
[Makeblock's Auriga port guide](https://support.makeblock.com/hc/en-us/articles/4412149618327-About-Me-Auriga)
lists ports 6–10 as compatible with the ultrasonic module. A brief check with
a box in front distinguishes a missing echo in the current scene from a
connection problem; a diagnostic target is not required during exploration.
If the same setup still has no return, try another RJ25 cable, then a known
working module or supported port. Do not run this command while patrol owns
the serial device:

```bash
uv run hb rover --port /dev/ttyUSB0 range --sensor-port 10
```

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

For the connected `teamrover@heartbeat` Pi, see the
[credential setup, verified device paths and bench results](PI-CONNECTED-CHECK.md).

- Pi 5 CSI camera, with Raspberry Pi OS `rpicam-vid` available.
- Pi USB to the Auriga rover controller, with the rover's motor power connected.
- USB contact microphone/stick to the Pi, plus a working speaker for prompts.
- Network access to the Decisions API and the selected CLAP inference service.

Use the existing [Pi setup](PI.md) and [rover firmware setup](../firmware/rover/README.md).
This feature adds no Python dependencies and does not install a CLAP model on Pi.
For a CLAP service on the Pi, follow [Local Pi CLAP setup](PI-CLAP-LOCAL.md).
For this guide's knock-only patrol, launch that helper with the matching profile:
`bash scripts/clap_pi.sh --profile profiles/clap-knocks.yaml --allow-download`.
After the model is cached, omit `--allow-download`; wait for `CLAP ready` before
starting patrol. The profile's `http://127.0.0.1:8767` then reaches the local service.
If CLAP instead runs on the Mac, set `classifier.clap.endpoint` to its reachable
LAN address, for example `http://192.168.1.20:8767`, and ensure the service listens
on a reachable interface. Pi `127.0.0.1` points to the Pi, not the Mac. The new
CPU-only service setup does not establish end-to-end rover readiness or throughput.

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

### Camera-guided autonomous exploration

Use `pi-explore` for exploration with camera decisions, ultrasonic avoidance,
audio evidence and the dashboard together. Ordinary exploration keeps the 30 cm
front motor cutoff and 45 cm forward-start requirement. Unlike the forward-only
`pi-full-check` test, it supports short turns after an acknowledged STOP and a
new camera/range observation. Turning still requires `--allow-turns` and physical
verification of direction and clearance.

The replacement Pi's actual ultrasonic connection was verified on **port 10**:
three readings were 99.71–99.81 cm while port 6 returned no echo. Both
`pi-explore` and `pi-full-check` now select port 10. Earlier port-6 instructions
describe the old wiring; an explicit `--sensor-port 6` would override the fix.

The mission requests `APPROACH_DEBRIS` when the camera recognizes aligned debris
ahead, then `INSPECT_DEBRIS` only at a close measured arrival. Recognition is a
model recommendation, not a validated debris detector. It cannot confirm a
survivor. With `inspection_trigger: debris_only`, only this close visual debris
choice starts a listening investigation. Knocks,
screams, other sounds and a visible person alone do not interrupt exploration
to start listening. Audio remains recorded/classified; fresh audio is still
used after the rover has stopped at a camera-selected debris site. Ordinary obstacles can trigger
a stop, reobservation, a permitted turn and continued exploration. Stall,
communication, STOP, camera and API failures still require review.

`rover.decisions.debris_approach` is enabled in `pi-explore`, with a nominal
**3 cm gap** measured from the ultrasonic sensor face and **0.5 cm arrival
tolerance**. This is close positioning, not confirmation that the chassis or
microphone touched debris. The camera selects short approaches, checks target
alignment and identity again after each move, and may HOLD when uncertain.
The host limits each approach to 2 cm, 35% power and 1000 ms; the final move
is reduced to the remaining measured gap. All approach requests count against
the session's forward distance budget.

The separate guarded APPROACH primitive permits travel inside the ordinary
30 cm obstacle cutoff and stops early at its configured close target. It
requires a fresh usable ultrasonic reading even when camera-only exploration
is enabled. Losing range during a close approach stops it; subsequent unknown
readings offer HOLD until distance returns. The controller never treats a
missing echo, motor stall, image alone or an encoder step completing as arrival.
Listening requires another fresh close reading and a camera debris decision.
The dashboard shows the approach target and the arrival photo/reason.

This policy needs matching firmware advertising `approach=1`. The disconnected
rover was not flashed or run for this change. After reconnecting, build/upload
the updated [rover firmware](../firmware/rover/README.md) before arming. The host
rejects old firmware rather than silently starting a distant listening check.
Set `debris_approach.enabled: false` to retain the earlier any-distance visual
inspection behavior. Other profiles keep that earlier policy by default.

For knock pickup, the current [M5Stick firmware](../firmware/stickmic/README.md)
captures its built-in PDM microphone; it does not implement the piezo input.
A closer air microphone may help, but this change has no measured sensitivity
or contact-coupling guarantee. For physical contact, the suggested hardware is
a compliant front mic mount and a bumper/limit switch that detects contact.
A piezo contact pickup needs an appropriate audio input/preamp and a supported
capture path. Contact pickups capture substrate vibrations through coupling to
the material ([example research](https://pmc.ncbi.nlm.nih.gov/articles/PMC11845984/)).
The nominal 3 cm target is at the lower end of the [sensor specification](https://support.makeblock.com/hc/en-us/articles/16579980385047-About-Me-Ultrasonic-Sensor);
mounting offset, range noise and coasting require hardware calibration. No
bumper input or physical-contact detector is implemented by this code change.

`pi-explore` enables `rover.decisions.no_echo_camera_motion` at the operator's
request. After a fresh, explicit `cm=-1 valid=0` reply, it offers the AI fixed
forward and opted-in turn choices based on the fresh camera image. The model
still receives `front_cm: null`, `range_valid: false`, and `no_echo: true`;
missing echoes are never converted into a made-up obstacle distance. HOLD
remains available; close-debris listening still requires a usable near reading.
The dashboard identifies camera control while
the ultrasonic sensor has no echo. A missing echo may also indicate a faulty
sensor or cable; camera-only movement has no ultrasonic distance protection
until valid readings return. Camera obstacle/edge recognition is unvalidated.

The base setting is false, so other profiles keep stationary choices and retry
on missing echoes. Silent serial links, malformed readings, stale telemetry,
camera/API errors, weak decisions and motor faults still stop the rover. Valid
readings continue to enforce the 45 cm forward-start, 30 cm forward-stop and
15 cm turn thresholds for ordinary exploration. Close APPROACH uses its separate
target and always requires usable range. The mode requires firmware advertising `noecho=1`.
The host sends `NOECHO 1` after acknowledged NAVCFG, and checks its acknowledgement
before allowing movement. The policy is RAM-only and resets on boot or NAVCFG.
Interrupting a move can reopen/reset USB; the host reapplies navigation settings
and the no-echo policy before the next camera-approved move.
MCU-only EXPLORE keeps its original stopped retry behavior.
No flat calibration target is a runtime prerequisite. Terminal output, dashboard
status and event logs include readable reasons for stops. A distinct
`INSPECT_DEBRIS` choice prints "Camera flagged possible debris"; a generic HOLD
prints "Camera requested HOLD" and is never relabeled as debris. Other messages
identify obstacle rechecks, turns, knocks, low confidence and missing range.

Start the classifier with:

```bash
bash scripts/clap_pi.sh --profile profiles/pi-explore.yaml
```

Then run the following after identifying the rover and microphone ports and
verifying the physical movement setup. These paths match the observed Pi setup;
recheck them after changing USB connections:

```bash
uv run --no-sync --env-file .env.local hb patrol \
  --decisions --profile pi-explore \
  --rover /dev/serial/by-path/platform-xhci-hcd.1-usbv2-0:1:1.0-port0 \
  --device stick:/dev/serial/by-id/usb-Hades2001_M5stack_81520161EE-if00-port0 \
  --sensor-port 10 --knocker speaker --dashboard --host 127.0.0.1 --port 8001 \
  --arm-motion --allow-turns
```

Omit `--arm-motion` to observe without motor commands. The profile bounds a
session to 120 seconds and a cumulative 100 cm of reserved forward commands,
using segments of at most 20 cm. Interrupted distances are not refunded.
These are command budgets, not calibrated chassis travel. Press `e` + Enter
for E-STOP, or Ctrl+C to close. Camera fallback still requires `--arm-motion`,
and turns require `--allow-turns`. Set `no_echo_camera_motion: false` to restore
stopped retries for missing echoes.

An optional `min_battery_v` setting can restrict movement using the nominal
battery reading. It is disabled in this profile pending pack/ADC calibration.
The live run reported 6.94 V, but the operator subsequently confirmed a new
battery and an actual forward step. This ADC estimate alone does not establish
battery health. The last known reading stays visible during a cruise; it is
refreshed from STATUS while stationary, not continuously measured under load.

### Forward-only check

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
Base profile defaults assign a 3 cm forward step, a 10 degree turn, 25% speed,
and a 1000 ms action deadline. The `clap-knocks` profile uses 35% following the
Pi's lifted-track check; see the [bench results](PI-CONNECTED-CHECK.md).
These are requested bounds, not measured travel accuracy.
Freshness, confidence, state and sensor checks run again before motion dispatch.
The model's score is not a calibrated probability that movement is safe.

### Continuous forward mode

`pi-full-check` opts into `rover.decisions.continuous_forward`. One accepted
FORWARD_SHORT proposal starts a bounded CRUISE target, reserving the remaining
forward budget. Subsequent forward decisions renew permission without stopping
or sending another movement command. Camera requests run while moving; the MCU
streams `NAV FORWARD` range observations, avoiding a blocking RANGE transaction
behind the active motor command. HOLD, weak proposals, stale evidence and faults
stop motion, as do the existing audio and operator controls. Turns are disabled
in this mode. The total reserved distance is not measured completed distance;
an interrupted target is not refunded.

Protocol addition: `CRUISE <cm> <speed_pct> <max_ms>`, acknowledged at completion
with all three arguments. Limits are forward 100 cm, 60% power, 30000 ms; profile
limits may be lower. Updated firmware advertises `cruise=1` after `guarded=1` in
CAPS. STEP/PIVOT retain their existing 2000 ms ceiling. Install matching host and
firmware versions together. The test profile uses a 50 cm target and 15000 ms
deadline. Continuous-mode serial heartbeats independently consult the host
motion gate so expired camera evidence cannot leave the motors running while
the processing loop is stalled.

Firmware samples front range during guarded movement. Too-close readings stop
the rover; unknown readings stop it unless the explicit NOECHO policy is enabled
for STEP/PIVOT/CRUISE. Close APPROACH always requires usable range and uses its
own measured stopping target.
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

Hardware-free controller and dashboard tests cover the movement gates, debris-only
inspection policy, stop-image retention and image endpoint. Live Pi recordings
and their limitations are documented in [the exploration evidence](evidence/20261108-autonomous-exploration.json)
and [the debris-only policy check](evidence/20261108-debris-only-policy.json).
The code-only [close-approach check](evidence/20261108-close-debris-approach.json)
records the software tests, compiled firmware, and unverified physical behavior.
Debris recognition accuracy, braking distance and encoder travel accuracy remain
unvalidated. This feature provides no mapping, systematic coverage or
return-to-start guarantee.
