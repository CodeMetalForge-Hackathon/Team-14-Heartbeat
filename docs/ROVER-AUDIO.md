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

Terminal 2, after checking the route and substituting the actual rover serial
port for `/dev/ttyACM0`:

```bash
uv run hb patrol --profile clap-distress --rover /dev/ttyACM0 --device stick --knocker manual --dashboard
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


## Stop to investigate knocks only

Use `--profile clap-knocks` instead of `clap-distress` for the patrol. This
profile is compatible with the existing CLAP distress service on port 8767;
the service does not need restarting. Restart the patrol process to load it.
For a fresh service, use `--profile profiles/clap-knocks.yaml` on the service too.

One accepted knock classification requests STOP, followed by the usual
two-second settle, eight-second verification, and operator review. Further
accepted stationary knocks can initiate the existing challenge-response dialog.
The classification cutoff is unchanged. This detects knock-like impacts, not
necessarily human knocks; test motor noise and bumps on the actual rover.

Vocal monitoring is disabled in the patrol client. Screams, speech, breathing,
and help phrases do not trigger this mode. Route checkpoints advance without
scheduled listening or review pauses. The bounded drive/turn commands still
finish between segments, and the rover stops at the end of the route. Operator
pause/E-STOP, communication faults, and firmware protections remain active.
Fixed-route mode does not repeat automatically or read the ultrasonic sensor.
Use the explicit exploration mode below for front-obstacle navigation.

Human-operated command (Mac example):

```bash
uv run hb patrol --rover /dev/cu.usbserial-1130 --device stick --profile clap-knocks --route profiles/route.yaml --knocker manual --dashboard --port 8001
```

After a knock investigation, Resume skips the interrupted route leg as before;
it does not replay an unknown remaining distance.


## Autonomous local exploration (ultrasonic port 6)

`--explore` replaces the fixed route with bounded, reactive front-obstacle
navigation. The confirmed wiring on this Ranger is ultrasonic **port 6**, line
sensor **port 9**. The line sensor is unused. Port 6 returned a valid distance
after port 10 returned unknown; the reason for the original unknown is not established.
The firmware's positive DRIVE direction is now mapped toward the ultrasonic
sensor on this assembly. After uploading, test `drive 5 --speed 30` and verify
actual forward travel before starting exploration. Encoder signs change together
with motor direction; positive turns retain their previous motor mapping. This mode provides no GPS position,
map, coverage guarantee, or return-to-start function. It cannot detect stairs,
drops, side/rear obstacles, or every reflective/absorbent surface. Start on a
level, unobstructed supervised test area with cable slack and access to STOP.

Upload the updated `firmware/rover` first, using the build/upload instructions in
its README. An older sketch rejects the new commands and patrol remains stopped.
The straight-drive constant-power experiment is now part of this firmware.
Exploration turns also retain their requested power until encoder completion;
startup slew, target tolerance, stall guard and watchdog remain active. Actual
turn angles and coasting distance need testing before autonomous operation.

First exit patrol (Ctrl+C) and measure a stationary target at two distances:

```bash
uv run hb rover --port /dev/cu.usbserial-1130 range --sensor-port 6
```

Place a flat target around 20 cm ahead, then farther away and rerun. Check that
`cm` changes plausibly and `valid=1`. `cm=-1 valid=0` means no usable echo, which
can also occur in open space beyond sensor range. Do not start until this check
works. RANGE never starts the motors; do not run another serial owner beside it.

Leave the CLAP service running with `profiles/clap-knocks.yaml`. Then run:

```bash
uv run hb patrol --rover /dev/cu.usbserial-1130 --device stick --profile clap-knocks --explore --sensor-port 6 --explore-seconds 120 --knocker manual --dashboard --port 8001
```

The rover waits 200 ms, measures in front, and starts a 20 cm forward segment
only with at least 45 cm clearance. It checks every 80 ms while moving forward;
a reading at/below 30 cm cuts motor power before requesting a turn. It then waits
and turns 45 degrees in place, repeating until a reading permits forward travel.
Eight unsuccessful turns produce a stopped `navigation_blocked` error. A front
obstacle closer than 15 cm prevents even a turn (`obstacle_too_close`). A missing
echo produces a stopped `range_unknown` error, never permission to drive.
Front range is not used to assess side clearance during a turn.

All these parameters live under `rover.navigation` in the profile and are sent
to the controller before movement. Firmware rejects values outside fixed bounds.
Echo acquisition is limited to 24 ms, so a disconnected sensor cannot block the
controller's serial/stop loop for the library's default long timeout.

Console `NAV FORWARD`, `NAV OBSTACLE` and `NAV TURN` lines include front distance
and are recorded as `rover` events. CLAP keeps capturing in parallel. A classified
knock cancels exploration, then follows SETTLING → VERIFYING → dialog → REVIEW.
Resume after review starts a fresh exploration period at the current location.
The two-minute default ends with motors off and DONE; it is not an endless loop.
`--explore-seconds` supports 1–600 seconds. Pause, abort, E-STOP, serial faults,
stall protection and the communication watchdog remain active. `--knocker manual`
still requires an operator to deliver the printed challenge prompts.

Software-only checks (the dry run simulates elapsed exploration, not geometry):

```bash
uv run hb patrol --dry-run --profile clap-knocks --explore --explore-seconds 2
uv run pytest -q tests/test_navigation.py
python3 tools/check_rover_firmware.py
```

The C++ harness needs a C++17 compiler and supports `--compiler` and `--sysroot`
for local toolchain setup. It compiles the actual sketch against fake I/O and
exercises obstacles, unknown range, search limits, STOP, watchdog, stalls, and
completion. These checks do not validate physical obstacle detection or braking.
