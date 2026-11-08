# Connected Pi: credentials and hardware checks

This is the `teamrover@heartbeat` Pi checkout at `/home/teamrover/Team-14`.
The observations below are from 2026-11-08, against application commit
`edb1628`. They are bench checks, not autonomous-navigation acceptance.

## Save the API key on the Pi

Open the owner-readable, Git-ignored file:

```bash
cd /home/teamrover/Team-14
nano .env.local
```

Replace the placeholder so the file contains one assignment:

```dotenv
OPENAI_API_KEY=your_actual_key_here
```

Save with Ctrl+O, Enter, then Ctrl+X. Do not paste the key into chat, source,
shell command arguments or logs. Keep `chmod 600 .env.local`; `.gitignore`
already excludes it. Commands using the API must include
`uv run --env-file .env.local`. This supplies the environment variable read by
`heartbeat/decisions.py`; it is not necessary to run the app as root.
This follows the [OpenAI authentication guidance](https://developers.openai.com/api/reference/overview).

## Actual device identities

Use these stable paths for the application. Numbered USB paths can change after
replugging or rebooting; the GPS and microphone are not the rover controller.

| Device | Stable path | Current node |
| --- | --- | --- |
| Auriga rover (`PONG rover v1`) | `/dev/serial/by-id/usb-1a86_USB_Serial-if00-port0` | `/dev/ttyUSB0` |
| M5Stick microphone (`stickmic v1`) | `/dev/serial/by-id/usb-Hades2001_M5stack_81520161EE-if00-port0` | `/dev/ttyUSB1` |
| NEO-M9N GPS | `/dev/serial/by-id/usb-u-blox_AG_-_www.u-blox.com_u-blox_GNSS_receiver-if00` | `/dev/ttyACM0` |
| CSI camera | `rpicam-vid`, camera index 0, IMX708 | CSI camera stack |

## Firmware installed

The initially connected firmware answered PING, STOP and RANGE but lacked CAPS.
The current `firmware/rover/rover.ino` was compiled and uploaded with verification.
After upload, the real controller returned `CAPS guarded=1`, acknowledged NAVCFG
and STOP, and reported `moving=0`, `enc=0,0`. Its nominal battery reading was
7.94 V; this is not a calibrated multimeter measurement. A low-brightness blue
LED command and LED-off command were both acknowledged.

This Pi already has the Arduino CLI, AVR core 1.8.8 and a Makeblock library under
`LifeScout`. That library has an existing `LIFESCOUT_AURIGA` conditional to avoid
duplicate MegaPi/Auriga pin-map definitions. The successful build used it:

```bash
/home/teamrover/LifeScout/tools/arduino-cli compile \
  --fqbn arduino:avr:mega:cpu=atmega2560 \
  --libraries /home/teamrover/LifeScout/libraries \
  --build-property build.extra_flags=-DLIFESCOUT_AURIGA \
  --build-path /tmp/heartbeat-rover-build firmware/rover
```

Result: 25,954 bytes flash and 2,217 bytes static RAM. No library files were
changed. This CLI's upload discovery rejected the `/dev/serial/by-id` alias;
the successful upload used its freshly verified canonical `/dev/ttyUSB0` node:

```bash
/home/teamrover/LifeScout/tools/arduino-cli upload \
  --fqbn arduino:avr:mega:cpu=atmega2560 --port /dev/ttyUSB0 \
  --protocol serial --verify --input-dir /tmp/heartbeat-rover-build firmware/rover
```

Re-identify the node and close serial owners before any future upload. The
temporary build directory is not permanent installation storage. Uploading
resets the controller; this sketch boots with motor output zero.

## Hardware results and remaining checks

- **Microphone transport:** 80,128 samples (5.008 s) received, zero bad, missing
  or stale frames. Peak 0.9995 and RMS -23.9 dBFS in that sample; this is not a
  calibrated gain or recognition-accuracy test. A subsequent 20 s live
  microphone/onset/CLAP pipeline run completed with zero errors and zero bad,
  missing or stale frames. Two ambient transients were classified below the
  knock threshold; no controlled human-knock recognition was established.
- **Camera:** direct MJPEG capture succeeded. The app adapter then produced
  successive complete JPEGs at the configured 5 fps; cold start was about
  1.56 s. One earlier 3 s attempt timed out during concurrent setup work;
  the cause was not isolated. Defaults were retained after successful repeats.
- **CLAP:** local CPU service started with `clap-knocks`; health returned 200
  with the expected model and audio contract. It is a separate Python service,
  not a boot-enabled motor controller.
- **Front ultrasonic, port 6:** 17/20 readings valid, ranging 6.09–9.86 cm;
  three readings had no valid echo. This is below both the forward and turn
  clearance requirements. After the operator cleared the front, five subsequent
  readings all returned `cm=-1 valid=0`. Unknown range remains a stop condition.
- **GPS:** 30.13 s receive-only test, 360 checksum-valid NMEA sentences, zero
  invalid sentences, zero satellites used/in view, no fix. See [GPS.md](GPS.md)
  for the antenna/open-sky follow-up. GPS is not integrated into this controller's
  navigation; this check does not add that feature.
- **Speaker:** only HDMI ALSA cards detected, and `hb devices` reported no usable
  audio output. GPIO12 remains input. The previously tested direct GPIO12-to-Grove
  analog connection has not produced clear speech; ordinary speaker playback
  cannot use that wiring without the appropriate analog interface. The operator
  confirmed that this is still the current connection.
- **Motor/encoder travel:** the lifted-track check below passed at 35% power.
  Floor travel, stopping distance, turning and direction relative to the chassis
  still require physical checks before autonomous movement.

## First live API and integration results

The operator saved the key locally; no secret value was printed or committed.
A fresh CSI camera image and actual stationary rover status were sent to the
Decisions API. The range reading was explicitly marked invalid (`front_cm=null`,
`range_valid=false`) and only HOLD/STOP_AND_LISTEN were offered. The API returned
HOLD, with probabilities 0.79/0.21, in 0.662 s. This isolated API check used the
15 s preview timeout and never executed movement. It does not bypass the live
patrol's requirement for valid range data.

The actual `hb patrol --decisions` observation session opened the camera,
microphone, classifier, rover and local dashboard. Its front-range error caused
STOP and REVIEW as intended. A later burst of audio transients exceeded CLAP's
2.5 s maximum result age and aborted the session, with STOP acknowledgements.
One corrupt stick frame and one missing frame were also reported at startup;
the source resynchronized and inserted the documented silence block. This run
did **not** establish successful continuous patrol operation. No freshness
limits or obstacle checks were relaxed.

The dashboard returned HTTP 200 with `moving=false`, `armed=false`, and zero
steps. An E-STOP request after the session had already aborted returned success;
this was not a test of emergency braking during movement. The session was then
closed. Local recordings are under the ignored
`runs/pi-connected-observation-20261108/` directory.

### Flat-target repeat and lifted-track tests

The operator confirmed tracks supported off the ground, an accessible power
switch, a flat target in front and the ultrasonic cable in port 6. Ten readings
were then consistent at 47.90–48.00 cm. A second full observation run completed
its 20 s navigation budget: 17 real camera/API decisions, all HOLD, API latency
0.184–0.558 s, range 46.03–46.53 cm, no error events and no motion commands.
Both session recordings passed `hb verify` (57 events/4 clips and 228 events/1
clip respectively). The second run is at `runs/pi-connected-target-20261108/`.
This is a short successful stationary integration check; the earlier burst-load
failure remains a throughput limitation.

A separate `STEP 3 25 1000` guarded bench request returned `guard_timeout` at
1.013 s. The operator saw both tracks turn. STOP was acknowledged. A repeat
kept the same USB connection open and polled STATUS during motion, avoiding a
controller reset before reading the counters. Feedback reached only about
`enc=-1,3`; after STOP it was `enc=-1,2`, versus about 54 degrees per wheel with
the fallback calibration. The nominal voltage reading was 7.94 V before and
7.08 V immediately after that repeat. These observations do not distinguish
limited mechanical movement from incorrect encoder feedback or supply issues.
No calibration values or firmware guards were changed.

A comparison at 35% power completed `STEP 3 35 1000` in 0.619 s. Both encoder
readings progressed together and finished at `enc=-55,55`; STOP was acknowledged.
This supports inadequate 25% drive power as the cause of the earlier bench
shortfall, rather than a missing encoder channel in this check. The nominal
battery reading dipped from 7.92 V to 6.77 V under load; supply performance was
not measured independently. `profiles/clap-knocks.yaml` now overrides only
`rover.decisions.speed_pct` to 35. Its default remains observation-only, and
the 3 cm bound, 1000 ms deadline, range checks and other guards are unchanged.
This does not calibrate on-floor distance, braking or turn direction.

An initial attempt to test mid-step STOP was not dispatched: front range had
fallen to 44.29 cm, below the unchanged 45 cm forward-start threshold. The
preflight left the rover stopped; this attempt does not validate interruption
of an active move.

After the target was moved farther away, range read 61.03 cm. The same guarded
step at 35% was interrupted by a separate STOP after 450 ms. STOP acknowledged
in 4.46 ms, the pending step returned canceled at 455 ms, and final status was
`moving=0`. This verifies command interruption on the lifted chassis, not
on-floor braking distance. No motor command remains running.

## Observation session after provisioning the key

First start the cached classifier in one terminal and wait for `CLAP ready`:

```bash
cd /home/teamrover/Team-14
bash scripts/clap_pi.sh --profile profiles/clap-knocks.yaml
```

Only one CLAP process should own port 8767. Then, in another terminal:

```bash
cd /home/teamrover/Team-14
uv run --env-file .env.local hb patrol \
  --decisions --profile clap-knocks --decisions-seconds 30 \
  --rover /dev/serial/by-id/usb-1a86_USB_Serial-if00-port0 \
  --device stick:/dev/serial/by-id/usb-Hades2001_M5stack_81520161EE-if00-port0 \
  --sensor-port 6 --knocker manual --dashboard --port 8001
```

This sends real camera frames to the Decisions API, incurs API usage, and keeps
model-proposed movement disabled. `manual` prints prompts for an operator; it
does not validate speaker playback or a physical knocker. With a tested audio
output, use `--knocker speaker --speaker-device OUTPUT_INDEX` from `hb devices`.
The knock-only profile does not enable vocal-distress triggering.

From the laptop, tunnel the local dashboard:

```bash
ssh -L 8001:127.0.0.1:8001 teamrover@heartbeat.local
```

Open `http://127.0.0.1:8001`. `e` then Enter requests E-STOP; Ctrl+C stops and
closes the run. The dashboard process can remain open after the navigation
budget ends. A detected knock can enter stationary verification and operator
review. Errors hold the rover stopped; do not mask invalid sensor data or
relax freshness guards to make a test appear to pass.

Do not add `--arm-motion` until the motor, encoder, range and physical placement
checks pass. Turn support additionally requires `--allow-turns` and physical
direction/side-clearance verification. See [DECISIONS.md](DECISIONS.md) for the
control and logging behavior.

## Software validation

`uv run --no-sync pytest -q`: **885 passed**, one existing Starlette/httpx
deprecation warning. `uv run --no-sync python tools/check_rover_firmware.py`
also passed. The latter is a host C++ simulation of the actual sketch; it does
not measure braking, motor polarity, encoder wiring or ultrasonic performance.
