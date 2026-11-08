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
