# New Pi 5: Codex handoff, 2026-11-08

The operator requested moving to a second Pi 5 with an NVMe M.2 HAT+ and
pushing the complete project/recovery handoff. Start here; the rover is
**not recovered**. The new host has not been tested. Do not assume replacing
the Pi fixes the Auriga, its battery, wiring or firmware.

## Paste into the new Codex session

> Read AGENTS.md, docs/NEW-PI-HANDOFF.md, docs/PI-ROVER-CURRENT-ISSUES.md,
> docs/PI-CONNECTED-CHECK.md and prompts/PI-04-rover-recovery.md. We moved to
> this Pi 5 with an NVMe M.2 HAT+. Set up the project and continue the rover
> recovery, starting with an isolated no-motion link test. Follow PI-04's
> operator confirmations before opening the rover port, powering it, flashing,
> or testing motion. Confirm the new physical setup; the previous host's USB
> paths and readiness are not current. Keep both motor leads disconnected
> until firmware identity and STOP checks pass. Use the bundled recovery HEX
> if the upload stage is reached. Once recovery checks pass, run one supervised
> full pipeline test. Keep tests proportionate, record actual results, and
> commit/push the changes. Do not claim recovery from software tests alone.

## What has already changed

- CLAP-05 is implemented: classifier failures degrade to the configured fallback.
- `pi-full-check` uses motion confidence 0.50, speed 35%, a cumulative 50 cm
  command budget and 60 s navigation budget. Requested distance is not a
  calibrated physical distance.
- Continuous forward support is committed (`101c7ca`), including camera/API
  observations during motion. It requires `CAPS guarded=1 cruise=1`.
  The new firmware was compiled, but **was never successfully uploaded**.
- Previous short steps worked; later the left track ran unexpectedly and
  STOP did not acknowledge. The operator stopped it by removing power.
- Serial data contained zeros/malformed bytes already in USB IN payloads.
  These are serial bytes, not corrupt camera images. Root cause is unproven.
- On the old Pi, LifeScout was disabled with explicit approval, was already
  inactive during failures, and `get_throttled` was `0x0`.
- Historical Auriga voltage estimates were 7.92 V idle / 6.77 V under load.
  The operator has no multimeter and replacement batteries were unavailable.
- PI-04 stopped before its first link test: the Auriga was absent from USB.
  No PI-04 flash, motion test or Mac comparison ran.
- Offline suite: **927 passed**, one existing Starlette/httpx warning, on
  application tree `1919e4b`. Later changes in this handoff are documentation
  and a copy of an existing firmware artifact, not application modifications.

## NVMe HAT difference

The [M.2 HAT+](https://www.raspberrypi.com/documentation/accessories/m2-hat-plus.html)
connects an NVMe drive through PCIe. With an SSD fitted it supplies storage,
not AI acceleration. This application's YAMNet path uses CPU LiteRT; CLAP's
separate process uses CPU PyTorch; Decisions calls a remote API. No Hailo
integration is present in `heartbeat/`, `scripts/` or `tools/`.
No application change or AI HAT driver is needed for this migration.
NVMe may improve loading and recording I/O; it does not accelerate CLAP
inference. Keep the Pi on its wall supply and use adequate cooling.

Use the new Pi's working OS configuration. Do not copy the old Pi's boot
configuration, virtual environments, service enablement, or AI accelerator
overlays. Do not format an existing NVMe drive to migrate this repository.
If booting normally already, no boot-order changes are needed. Check storage
with `lsblk -o NAME,SIZE,FSTYPE,MOUNTPOINTS` and `df -h .`.

## Get the project and dependencies

Use 64-bit Raspberry Pi OS. Give the new Pi a distinct hostname while the old
`heartbeat` is online. Authenticate GitHub without embedding a token in URLs.
From the new Pi, clone (or `git pull --ff-only` in an existing clean checkout):

```bash
git clone https://github.com/CodeMetalForge-Hackathon/Team-14.git
cd Team-14
bash scripts/pi_setup.sh
```

Run setup as the normal account, not `sudo bash`. It installs OS audio tools,
uv/Python 3.11, locked project dependencies and YAMNet, and adds audio/dialout
groups. It does not install or enable a controller service. Log out/in for
groups, then `export PATH="$HOME/.local/bin:$PATH"` if needed. Its final
doctor/run suggestions touch hardware: defer them until recovery setup is
confirmed. Run `uv run pytest -q` once on the new environment.

Git includes source, profiles, voice WAVs, trained knock head, and diagnostic
JSON evidence. Git excludes `.env.local`, recordings in `runs/` and `data/raw/`,
large downloaded models, and virtual environments. Keep those out of commits.
Core setup downloads YAMNet; CLAP's first start downloads its own model.

For Decisions, create `.env.local` privately with `OPENAI_API_KEY=...`, or copy
the old file over SSH (substitute the old Pi's actual address if necessary):

```bash
scp teamrover@heartbeat.local:/home/teamrover/Team-14/.env.local .env.local
chmod 600 .env.local
```

Do not print the key or paste it into chat. Optional historical recordings can
be copied privately with `scp -r` from the old Pi's `Team-14/runs/` and
`Team-14/data/raw/`; they are not necessary to start a fresh test.

## Recover the rover before starting patrol

Follow PI-04's order and bounded attempts. First inspect service ownership,
power status and USB enumeration on this host. Do not install LifeScout or
enable any boot controller. A missing LifeScout service is normal here.
With the operator at the board, both motor leads unplugged, and only Auriga
connected by USB, identify the actual ttyUSB node and check `fuser` before
the approved link test. Never assume the old by-id or by-path names still fit.
Do not use broad serial auto-discovery while recovery is pending.

The CLI link check is `timeout 15 uv run --no-sync hb rover --port ACTUAL_NODE ping`.
This is one bounded CLI invocation; internally its boot handshake can retry
PING. Only a valid `PONG rover v1` passes. The CLI has no `caps` subcommand;
use `RoverLink.capabilities()` for CAPS, and its `stop()` / `status()` methods
in a bounded script with `finally: link.close()`, after operator approval.
No DRIVE/STEP/CRUISE commands belong in a link-check script.

The exact preserved sketch image is now included at
`docs/evidence/rover-recovery/rover.ino.hex`. Read its adjacent README and
verify SHA-256 before using it. This avoids depending on the old host's
custom Makeblock library and `/tmp` build folder.

If PI-04 reaches its upload stage and the operator approves it, an alternative
to installing the old Arduino environment is the standalone avrdude uploader:

```bash
sudo apt-get install avrdude
sha256sum docs/evidence/rover-recovery/rover.ino.hex
# Replace ACTUAL_NODE with the isolated, rechecked /dev/ttyUSB* node.
timeout 90 avrdude -c wiring -p m2560 -P ACTUAL_NODE -b 115200 -D \
  -U flash:w:docs/evidence/rover-recovery/rover.ino.hex:i
```

avrdude verifies by default; do not add `-V`. Let writing finish or reach its
timeout. This uploads the sketch, not a replacement bootloader. Then obtain
PONG, CAPS, STOP and STATUS; expected CAPS is `guarded=1 cruise=1`.
Follow PI-04's retry limits, power checks and lifted-motion gates. If this Pi
also gets malformed bytes, record the comparison; it does not by itself
identify a failed component. The Mac comparison remains available in PI-04.

## Full pipeline after recovery

Reconnect and identify the microphone; attach the CSI camera with power off.
On the old Pi the camera was IMX708; check this host with
`rpicam-hello --list-cameras`. GPS is not required for this patrol.
Speaker output was never validated on the old Pi: only HDMI cards appeared.
Use `aplay -l` to identify actual outputs, and test an available output before
claiming speech works. `--knocker manual` prints prompts for the operator.
Do not blindly apply old GPIO speaker/AI overlays to this new setup.

First CLAP start, in terminal 1 (CPU service, separate from core environment):

```bash
bash scripts/clap_pi.sh --profile profiles/pi-full-check.yaml --allow-download
```

Wait for readiness; check `curl --fail --max-time 5 http://127.0.0.1:8767/health`.
Later starts can omit `--allow-download`. Start observation in terminal 2 with
the newly identified paths assigned to ROVER and MIC:

```bash
uv run --no-sync --env-file .env.local hb patrol \
  --decisions --profile pi-full-check \
  --rover "$ROVER" --device "stick:$MIC" \
  --sensor-port 6 --knocker manual --dashboard --port 8001 \
  2>&1 | tee ~/pi-full-pipeline.log
```

ROVER should be the verified rover by-path; MIC the verified microphone path.
This command leaves motion unarmed. After PI-04 motion/STOP gates pass and the
operator confirms a suitable floor test area, add `--arm-motion` for the
requested supervised run. Do not enable tabletop driving based on old consent.
Use `q` + Enter to abort, `e` + Enter for E-STOP; an unresponsive controller
requires the operator's physical power switch. Continuous mode still stops
for hazards, HOLD, low confidence, distress cues, faults and its time/distance
limits. Silence alone does not authorize driving.

Record the new host, device paths, PONG/CAPS/STOP results, voltage readings,
upload outcome, and actual pipeline behavior in the current-issues document.
Do not repeat the entire suite for documentation-only edits or claim physical
motion was verified by a mock test. Commit explicit paths and push main.
