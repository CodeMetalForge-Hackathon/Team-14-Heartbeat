# Pi rover validation, November 8, 2026

The full software suite passed: **1,030 tests**, with one Starlette/httpx
deprecation warning. The firmware simulator checks and AVR Mega2560 compilation
also passed. These checks do not establish that the real motors can turn.

The installed sketch initially rejected `CAPS`. Its flash was backed up to
`runs/rover-flash-20261108/before.hex`, then the current repository firmware was
uploaded and verified by avrdude (25,814 bytes). The board now reports
`CAPS guarded=1 cruise=1 noecho=1 approach=1`. PING, STOP, STATUS, NAVCFG and the
port 10 ultrasonic sensor responded. The compiled encoder pin table matches
the local Makeblock Auriga definition for slots 1 and 2.

A single bounded `STEP 3 35 1000` with a measured forward gap of 73.41 cm
returned `ERR guard_timeout`. STOP succeeded. The operator reported that neither
track moved with the battery switched on. The post-error serial reconnect resets
encoder counters, so the subsequent zero counters cannot measure travel during
the attempt. The reported battery estimate was 0.09–0.10 V, but the ADC is not
calibrated; this is not a verified pack-voltage measurement. Motor power,
connections and driver operation remain unresolved. No guards were disabled.

Local evidence is in `runs/rover-flash-20261108/`: `backup.log`, `upload.log`,
`postflash.json` and `step-check.json`. Runs are ignored by Git.

The live camera, ultrasonic sensor, GPT Decisions requests, microphone and CLAP
pipeline ran together. HOLD messages included `front distance 73.4 cm`.
One Decisions request exceeded its deadline and correctly stopped decision
processing in REVIEW; the session later reached its two-minute exploration
limit. The completed run `runs/20261108-094636` passed black-box verification
(420 events, 10 clips). A fresh observation run was started afterward.
Physical playback audibility and successful motion have not been established.

Run from the repository root to observe camera decisions with motors disarmed:

```bash
uv run --no-sync --env-file .env.local hb patrol --decisions --profile pi-explore \
  --rover /dev/serial/by-path/platform-xhci-hcd.1-usbv2-0:1:1.0-port0 \
  --device stick:/dev/serial/by-id/usb-Hades2001_M5stack_81520161EE-if00-port0 \
  --sensor-port 10 --knocker speaker --dashboard \
  --host 127.0.0.1 --port 8001 --allow-turns
```

The local CLAP service configured in the profile must already be running.
Open `http://127.0.0.1:8001` on the Pi. Stop the existing instance before starting
another; only one process should own each serial device and the camera.

No shared API contracts changed. The normal debris destination remains 3 cm
with 0.5 cm tolerance; faults and uncertain scenes can still stop travel sooner.

## Motor timeout diagnostics

Following the operator's explicit tabletop/no-echo authorization, the repeated
bounded step again timed out and the operator reported no movement or twitch.
The firmware now preserves commanded PWM, encoder progress and Mega2560 output
registers at an active movement deadline. It stops the motors before ADC sampling
or serial printing. The existing `ERR guard_timeout` reason gains diagnostic
fields; host parsing already preserves extended error text. Motion limits,
missing-echo policy, watchdog and obstacle checks are unchanged.

The diagnostic build passed the firmware checks, compiled to 26,394 bytes, and
was uploaded with avrdude readback verification. The next single bounded test
returned:

```text
ERR guard_timeout progress=0,0 target=53 pwm=-89,89 battery_adc=6 ocr=89,89 timer=129,131 ddr=176,47 dir=198
```

The PWM duty registers both held 89/255, both PWM output pins were configured
as outputs, and the direction output bits matched the requested forward
polarity. Encoder progress was zero before the host reconnected. These are MCU
register observations, not electrical measurements at the motor terminals.
STOP was acknowledged after the timeout.

The battery ADC was 6/1023, corresponding to the existing nominal 0.09 V
estimate. Earlier successful checks in `PI-CONNECTED-CHECK.md` recorded roughly
7–8 V and completed this same step at 35% power. This strongly suggests a motor
power-path problem, but does not establish which battery, connector, switch or
board component is responsible. A battery-only power-light check was requested;
the operator has not yet supplied its result. No successful motor motion has
been established in these retries.

Evidence: `runs/rover-flash-20261108/pwm-diagnostic.json` and
`motor-diagnostic-upload.log`. The fresh live observation run is
`runs/20261108-095838`; its dashboard is on port 8001. The live run is disarmed.
