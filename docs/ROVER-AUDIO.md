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
its README. NAVCFG includes the recovery timeout and valid-reading count, and
now accepts a zero turn angle for forward-only operation. Older firmware rejects
that zero angle and patrol remains stopped. Update both the host code and flashed
sketch before running this mode.
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
uv run hb patrol --rover /dev/cu.usbserial-1130 --device stick --profile clap-knocks --explore --sensor-port 6 --explore-seconds 60 --knocker manual --dashboard --port 8001
```

The rover waits 200 ms, measures in front, and starts continuous forward travel
only with at least 45 cm clearance. The 20 cm encoder checkpoints roll forward
without stopping the motors, restarting their ramp, or emitting repeated
`NAV FORWARD` lines. They retain bounded progress/deadline checks; a wheel that
passes a checkpoint is still monitored for stalls while the other catches up.
Front readings use an 80 ms poll interval plus the bounded echo acquisition time.
A reading at/below 30 cm cuts motor power. The default `turn_deg: 0` keeps the
rover stationary and continues reading the sensor; it does not turn or reverse.
After the 200 ms settling interval, two consecutive readings of at least 45 cm
resume forward travel. A smaller reading resets that count. If the rover starts
with less than 45 cm clearance, it waits for the same clearance confirmation.
The 30/45 cm thresholds prevent rapid start/stop cycling at one distance.

The previous search-turn behavior remains an explicit profile option: a nonzero
`turn_deg` from 15 through 90 enables turns. In that mode, eight unsuccessful
turns produce `navigation_blocked`, and a front obstacle closer than 15 cm
prevents a turn (`obstacle_too_close`). Leave the default zero angle for the
requested straight-ahead stop/resume behavior.

A missing echo immediately cuts motor power and reports `NAV SENSOR_WAIT`.
The controller retries while stopped for up to 800 ms, allowing 200 ms to settle.
It requires two consecutive valid readings agreeing on whether forward clearance
exists. A missing reading or a change between open/blocked resets that agreement.
`NAV SENSOR_READY` reports recovery, then the fresh distance determines forward
travel or a stationary obstacle wait (a search turn only if explicitly configured).
Restart uses the normal gradual ramp.
If recovery fails, `range_unknown` aborts. STOP, a knock-triggered STOP, watchdog,
and exploration expiry cancel recovery; later echoes cannot restart it. These
settings are `range_retry_ms` and `range_good_reads` in YAML. Front range is not
used to assess side clearance during a turn.

All these parameters live under `rover.navigation` in the profile and are sent
to the controller before movement. Firmware rejects values outside fixed bounds.
Echo acquisition is limited to 24 ms, so a disconnected sensor cannot block the
controller's serial/stop loop for the library's default long timeout.

Console `NAV FORWARD`, `NAV OBSTACLE` and `NAV TURN` lines include front distance
and are recorded as `rover` events. CLAP keeps capturing in parallel. A classified
knock cancels exploration, then follows SETTLING → VERIFYING → dialog → REVIEW.
Resume after review starts a fresh exploration period at the current location.
The one-minute default ends with motors off and DONE; it is not an endless loop.
`--explore-seconds` supports 1–600 seconds. Pause, abort, E-STOP, serial faults,
stall protection and the communication watchdog remain active. `--knocker manual`
still requires an operator to deliver the printed challenge prompts.

The Decisions API integration has been removed. This uses the earlier local
ultrasonic controller with a new forward-only obstacle wait. No camera or OpenAI
API key is required. The 60 seconds is a maximum exploration
period, not a promise of uninterrupted forward travel: obstacles, unknown range,
knocks and motor/encoder faults still stop or interrupt it. The previously
observed range and encoder faults are not repaired by this software rollback.
On Raspberry Pi, replace the Mac serial path above with the rover's actual
`/dev/serial/by-id/...` path. For CLAP on the Pi, start
`bash scripts/clap_pi.sh --profile profiles/clap-knocks.yaml --allow-download`
and wait for `CLAP ready`; omit `--allow-download` once the model is cached.

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
