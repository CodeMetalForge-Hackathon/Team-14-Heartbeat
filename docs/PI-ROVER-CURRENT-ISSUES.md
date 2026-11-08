# Pi rover: unresolved serial failure and unexpected track movement

Status recorded 2026-11-08. Continuous-mode source is committed in `101c7ca`,
but its firmware upload has **not succeeded** and live continuous movement is
**not validated**. Hardware testing is stopped. Keep rover battery and USB
disconnected until a controlled hardware diagnosis is arranged; do not resume
the armed patrol or repeated connection/upload attempts from the demo guide.

## What worked before the failure

- CLAP degradation/fallback changes: `717f052`.
- Bounded Pi pipeline and test confidence threshold 0.50: `cec0652`.
- In `runs/20261108-023003`, seven `STEP 3 35 1000` requests completed with
  acknowledgements (21 cm requested, not measured physical travel). An invalid
  front-range reading at 35.28 seconds caused STOP/REVIEW, with STOP acknowledged.
  The session was closed with `q` at 50.46 seconds.
- Continuous-mode code and firmware simulation passed the focused checks listed
  in [the pipeline record](PI-FULL-PIPELINE-CHECK.md#continuous-mode-validation).
  This is software evidence, not proof of operation on the rover.

## Current errors and evidence

### Firmware upload cannot establish communication

The previously compiled Mega 2560 image is at
`/tmp/heartbeat-rover-build/rover.ino.hex` on this Pi. Upload attempts used
Arduino CLI, `arduino:avr:mega:cpu=atmega2560`, the `wiring` programmer selected
by that board definition, 115200 baud, and verification enabled. Retries were
bounded to 25 or 30 seconds and ended with timeout status 124; an earlier hung
attempt was interrupted. Output included:

```text
Error: timeout
Error: timeout communicating with programmer
```

There is no successful upload/verification record or observed `cruise=1`
capability response. The currently running board firmware has not been
identified after the failure. Do not infer bootloader damage or a particular
firmware cause from these timeouts alone.

### USB enumeration succeeds, but rover replies are invalid

The stable rover path is
`/dev/serial/by-id/usb-1a86_USB_Serial-if00-port0`. It initially resolved to
`/dev/ttyUSB1`; after reconnects it resolved to `/dev/ttyUSB0`. Identify the
physical device before using a numbered node. PI-04 supersedes use of this
generic CH340 by-id alias for recovery: another CH340 board can share it.
Identify the isolated rover's ttyUSB node and record its by-path name first.

Kernel logs confirmed the rover's `1a86:7523` CH341 adapter disconnecting and
reconnecting. An earlier reconnect affected the u-blox GPS instead. The rover
adapter was also temporarily absent during one retry. Enumeration establishes
the USB adapter's presence, not successful communication with the Auriga MCU.

Subsequent communication probes used 115200 baud, exclusive serial access,
inactive DTR/RTS settings, bounded reads/writes, and only newline/STOP/PING
commands. CAPS/STATUS were conditional on a valid PONG; that condition was not
met. No DRIVE, STEP, CRUISE, PIVOT, TURN or EXPLORE command was sent by these
probes. Each probe closed its serial connection afterward.

Expected replies were `PONG rover v1` and `OK STOP`. Observed results included
unreadable bytes, repeated `!` characters, a two-byte `00 00` sample, and later
repeated five-second probes with this result:

```text
Rover USB: /dev/ttyUSB0
PING acknowledged: False
STOP acknowledged: False
Received bytes: 921
Most frequent byte values: [(0, 921)]
```

The application transport also reported `Rover reply exceeded line limit`.
These are serial bytes between the Pi and rover, not camera image corruption.
The quoted output is transcribed from tool results; complete retry transcripts
were not saved as repository log files. Earlier black-box run data remains local
under the ignored `runs/` directory.

### Unexpected left-track movement

The operator reported: "left tire is running non stop". A serial STOP attempt
was not acknowledged. The operator was instructed to switch off and disconnect
the battery and USB, then confirmed that the track had stopped. A process check
at that time found no running patrol or firmware uploader.

Before later serial-only probes, the operator explicitly confirmed the motor
battery was disconnected and only USB connected. The operator subsequently
reported "it moved once". Whether that referred to the latest probe or earlier
movement, and the hardware power arrangement at that instant, have not been
clarified. Battery isolation and motor power paths must not be assumed effective
from the earlier confirmation alone. Both battery and USB disconnection were
requested again, and further connection retries were stopped.

The unexpected motion is not evidence of a successful continuous-mode run.
Opening a serial port can affect reset lines; its relationship to this incident
has not been established. There is no reliable software STOP acknowledgement
for the current fault state.

## What remains unknown

The cause has not been isolated among controller power/reset behavior, wiring,
the serial interface/cable, existing firmware, or motor-driver hardware. No
specific component failure has been proven. The next diagnosis needs physical
inspection and verified motor-power isolation, followed by reliable controller
identity/STOP replies before firmware installation and a controlled motor test.
Repeated identical PING/upload attempts have not resolved the fault.

## Initial host-side diagnosis after the incident

Read-only host inspection found the correct `1a86:7523` CH340 adapter using the
Linux `ch341` driver, on kernel `6.18.60+rpt-rpi-2712`. Its USB runtime power state
was active and it enumerated at 12 Mbit/s. No process owned the rover tty at the
inspection time. ModemManager was running, but its journal reported that the
adapter was unsupported; that is not proof that it never probed the device.
No competing patrol or uploader was found. `vcgencmd get_throttled` returned
`0x0`; this describes the Pi and does not establish the rover's power quality.

The installed pyserial 3.5 code configures raw serial input and disables PARMRK
error marking. More importantly, the Raspberry Pi 6.18 driver source uses the
generic bulk receive path without per-byte framing/BREAK tagging. Consequently,
merely enabling PARMRK would not reliably distinguish genuine UART zero data
from an electrical BREAK condition on this adapter. The generic Linux terminal
explanation is a possible mechanism in general, not an established cause here.
Sources: [CH341 driver](https://raw.githubusercontent.com/raspberrypi/linux/rpi-6.18.y/drivers/usb/serial/ch341.c),
[generic receive path](https://raw.githubusercontent.com/torvalds/linux/v6.18/drivers/usb/serial/generic.c).

At that stage, USB monitoring was enabled but serial experiments waited for
motor isolation. The subsequent capture is recorded below. A USB trace cannot
by itself distinguish intentional MCU output from an electrical problem at the
adapter's UART input; that distinction may require physical signal measurement.

## Raw USB capture after motor-isolation confirmation

The operator answered "yes dont worry" to the explicit request to confirm both
motor cables were physically unplugged from the controller. With that
confirmation, one bounded capture and one baud comparison were performed.
No movement command, bootloader probe or firmware upload was issued. Both
serial sessions closed afterward; the baud setting was restored to 115200.
This diagnostic exception does not authorize reconnecting the motors or
resuming powered movement tests.

The capture filtered Linux usbmon events to the rover adapter only (bus 3,
address 10, `/dev/ttyUSB0`). Serial settings read back as 115200, 8 data bits,
no parity, one stop bit, with input flags zero. Three seconds of passive
application reads yielded no bytes. Three seconds with STOP/PING yielded
1,442 bytes, dominated by `FE` (742 bytes), followed by `60`, `80`, `78`, `E0`
and other values. No PONG or STOP acknowledgement was present.

The USB trace independently contains zero-valued data during opening and the
same malformed nonzero bytes during the probe. This establishes that the data
is already malformed in the adapter's USB IN payload, before Python reads it.
The application did not manufacture or text-decode zeros. All three outgoing
bulk submissions contain exactly `0a53544f500a50494e470a` (newline, STOP,
newline, PING, newline), with successful bulk OUT completions. No additional
bulk OUT writer is visible during this capture. Payload-bearing bulk IN
completions succeeded; the two empty `-2` completions at close are cancellations,
not evidence of a mid-test USB transfer failure. Initial buffered bytes can be
flushed during serial opening, so usbmon and application byte totals differ.

The control transfers also show an opening-time modem-line transition despite
the Python object's pre-open `dtr=False` and `rts=False`: CH341 request `a4`
values include `ff9f`, then `ffbf`, then `ffff`. Decoding the driver's inverted
control mask (DTR bit 5, RTS bit 6) means both asserted, then RTS alone, then
both deasserted. This is observed USB control traffic, not a measurement of
the physical pins. It could affect board reset circuitry connected to those
lines, but an actual MCU reset or causal connection to the unexpected motor
movement has not been established. Pre-setting pyserial's inactive flags does
not eliminate this observed opening-time transition.

The subsequent single-session baud comparison used one STOP/PING sequence per
rate and approximately 1.5 seconds of reception per rate:

| Baud | Bytes received | Observed byte values | Valid PONG / STOP |
| --- | ---: | --- | --- |
| 115200 | 2 | `00` only | Neither |
| 57600 | 445 | `00` only | Neither |
| 38400 | 475 | `00` only | Neither |
| 9600 | 474 | `00`, `C0`, `80` | Neither |

These short checks do not prove that every possible baud/clock mismatch is
excluded. They do show that selecting one of these common alternative rates
does not recover readable firmware responses. Output varied between tests;
the previous all-zero pattern is not a constant property of every capture.

**Current inference:** prioritize the UART path between the CH340 and MCU,
controller power/reset/clock state, attached circuitry affecting RX/TX, and the
adapter/MCU hardware itself. The evidence does not identify which component is
responsible, prove a short, establish bootloader corruption, or show that the
MCU intentionally transmitted zeros. USB enumeration and successful bulk
transfers alone do not validate the UART electrical signals. Physical wiring
inspection and measurements of controller supply, reset and UART signals are
the next discriminating checks; motor connections must remain isolated.

Saved evidence: [filtered USB trace and serial observations](evidence/20261108-rover-usb-trace.json),
[baud comparison](evidence/20261108-rover-baud-check.json). These files contain
only this rover's diagnostic data, not API credentials or camera/audio captures.

## PI-04 recovery

Reviewer updates `bbc4ec8` and `1919e4b` were fetched and fast-forwarded on
2026-11-08. This recovery follows `prompts/PI-04-rover-recovery.md`; firmware,
profiles and Python source remain unchanged. The Pi clock at intake was
03:26 EST / 08:26 UTC. The prompt's 08:00 go/no-go is treated as local event
time (America/New_York); the prompt does not explicitly specify a timezone.
Recovery is **in progress**, not yet recovered or conclusively ruled out.

The operator subsequently chose to move to a new Pi 5 with an NVMe M.2 HAT+
and requested a GitHub handoff. See [NEW-PI-HANDOFF.md](NEW-PI-HANDOFF.md).
This supersedes waiting for another USB connection on this host; it does not
establish a Pi hardware fault. The new host still needs the recovery gates.
The preserved HEX is bundled under `docs/evidence/rover-recovery/`, with its
hash and build provenance. This recovery record is being committed at the
operator's explicit migration request, before PI-04's hardware outcome is known.

### Step 0 — preserve the build: complete

Copied `/tmp/heartbeat-rover-build` to `/home/teamrover/heartbeat-rover-build`.
The preserved files are outside Git and survive reboot. SHA-256:

```text
rover.ino.hex (73535 bytes)
adaf180028278066cc287107827acfa2d05c5956c02cb00b747ecf83a4808575
rover.ino.elf (137916 bytes)
a4815a23a6f92137bb4eeffff00a2a147ca0d472928cc8e47d7398a34d2e9992
```

### Step 1 — host checks and competing service

`lifescout.service` was **enabled but inactive**, stopped since 01:27:11 EST.
Its ExecStart is
`/home/teamrover/LifeScout/.venv/bin/python -u /home/teamrover/LifeScout/lifescout.py daemon`.
The operator explicitly approved disabling it. Executed:

```bash
systemctl --user disable --now lifescout.service
systemctl --user is-enabled lifescout.service
systemctl --user is-active lifescout.service
```

The enablement symlink was removed; results were `disabled` and `inactive`.
This prevents a future reboot from reintroducing a competing controller. It
does not establish service contention as the cause of the recent failures:
the service was already stopped during those attempts.

No running LifeScout daemon, patrol, serial diagnostic or uploader was found.
`vcgencmd get_throttled` returned `0x0`. The filtered kernel journal showed the
previous disconnect/reconnect events and no under-voltage/over-current lines.
The rover, stick and GPS were still enumerated at this initial inspection, so
the reviewer-specified unplugged setup was not yet verified. The operator's
initial "ok" was not treated as proof of the requested physical changes.
At 03:30:53 EST the kernel recorded the CH341's USB disconnect. Rechecking
while the rover was absent confirmed LifeScout disabled/inactive and Pi
`throttled=0x0`. The microphone and GPS remained enumerated. Step 1's host
checks are complete; the isolated step-3 hookup is still pending.

### Step 2 — battery and wiring: incomplete

The operator reports no multimeter, and replacing the batteries is difficult
at present. Battery type, current measured voltage, freshness/charge, wall
supply confirmation and the wiring inspection remain unverified. Historical
board estimates were 7.92 V at rest and 6.77 V under lifted-track load; they are
not new measurements and do not prove a brownout caused this incident.

Fresh-pack measurement is unavailable. The proposed next step is an isolated
no-motion communication test using the current pack, explicitly preserving the
unknown battery condition. It cannot validate motor operation or remove the
requirement to investigate power before lifted motion tests. The operator was
asked to inspect the left encoder connection, sensor port 6, loose jumpers and
any Bluetooth/wireless module, while keeping both motor cables disconnected.

### Step 3 — isolated link test: approved, awaiting USB connection

No new port open has occurred under PI-04. Requested setup: operator at the
rover, Pi on wall power, stick/GPS/knocker unplugged, only Auriga connected,
both motor cables disconnected, and explicit yes before the bounded PING.
CAPS/STOP/STATUS follow only after a valid PONG. A failed PING permits only
the prompt's one operator-approved power-cycle/retry before the Mac branch.

The operator subsequently answered "Yes—setup checked, ready for link test".
At 03:35 EST, however, `/dev/serial/by-id/` and `/dev/serial/by-path/` still
showed only the M5Stick (`ttyUSB1`) and u-blox GPS (`ttyACM0`); no Auriga
adapter was enumerated. LifeScout remained disabled/inactive. No port was
opened and no PING was sent: the approved target is absent. The operator
was asked to disconnect the stick/GPS and connect only the Auriga, keeping
the motor leads unplugged, or request help identifying those plugs. Earlier
uncertainty about the motor/encoder connectors means physical isolation is
operator-reported, not independently inspected.

Before isolation, the CH340 was `/dev/ttyUSB0`, with observed physical path
`/dev/serial/by-path/platform-xhci-hcd.1-usbv2-0:1:1.0-port0` (also exposed
without `usbv2`). This association is provisional until the rover alone is
connected and checked. The generic by-id name will not select recovery ports.

### Steps 4–6 — not yet reached

- Clean upload: not attempted under PI-04; requires the step-3 result and a
  separate operator yes. Use the preserved build and reidentified ttyUSB node.
- Lifted motion: not attempted. Motor leads remain disconnected until the
  firmware identity/STOP gate passes, then each test needs a separate yes.
  The prompt's "no movement until step 5 passes" is read as no general patrol
  before step 5; its specifically authorized step-5 checks are the only motion
  exceptions after steps 1–4. No permission for those checks has been given.
- Mac comparison: not run. If reached, exact operator-run copy/flash commands
  are prepared below; the result remains pending. No Mac access is assumed.

### Prepared step-6 Mac handoff — not executed

Use only if the isolated Pi link/recovery path fails. The operator runs these
on the Mac with motor leads still unplugged, operator present and an identified
rover connected alone. Do not reconnect motor leads based on upload success.
First copy the preserved image from the Pi (while the Pi is reachable):

```bash
mkdir -p ~/heartbeat-rover-recovery
scp teamrover@heartbeat.local:/home/teamrover/heartbeat-rover-build/rover.ino.hex ~/heartbeat-rover-recovery/rover.ino.hex
shasum -a 256 ~/heartbeat-rover-recovery/rover.ino.hex
```

Expected hash is the step-0 hash above. With the rover connected to the Mac,
the following selects exactly one usbserial node, uses avrdude from PATH or
the Arduino package directory, checks the copied image, and bounds the upload
to 90 seconds. It refuses ambiguous device selection. This is an operator-run
firmware write; do not run it before agreeing to that recovery step. Do not
manually interrupt an upload that has begun writing.

```bash
cd ~/heartbeat-rover-recovery
python3 - <<'PY'
from pathlib import Path
import glob, hashlib, shutil, subprocess

image = Path('rover.ino.hex').resolve()
expected = 'adaf180028278066cc287107827acfa2d05c5956c02cb00b747ecf83a4808575'
if hashlib.sha256(image.read_bytes()).hexdigest() != expected:
    raise SystemExit('Image hash mismatch; do not flash.')
ports = sorted(set(glob.glob('/dev/cu.*usbserial*')))
if len(ports) != 1:
    raise SystemExit(f'Connect only the rover, then retry. Found: {ports}')
tool = shutil.which('avrdude')
extra = []
if tool is None:
    candidates = sorted((Path.home() / 'Library/Arduino15/packages/arduino/tools/avrdude').glob('*/bin/avrdude'))
    if not candidates:
        raise SystemExit('avrdude is unavailable. Use the Arduino-installed tool or install it through Homebrew first.')
    binary = candidates[-1]
    tool = str(binary)
    config = binary.parent.parent / 'etc/avrdude.conf'
    if config.is_file():
        extra = ['-C', str(config)]
cmd = [tool, *extra, '-c', 'wiring', '-p', 'm2560', '-P', ports[0],
       '-b', '115200', '-D', '-U', f'flash:w:{image}:i']
print('Rover node:', ports[0], flush=True)
result = subprocess.run(cmd, timeout=90)
raise SystemExit(result.returncode)
PY
```

Record the complete avrdude result. Success is evidence for a Pi-side difference,
not proof that every rover subsystem is healthy; return to step 3 on the Pi.
A matching failure on the Mac supports removing the rover from the demo, but
does not by itself distinguish battery, wiring, UART, reset, MCU or bootloader
failure. Do not label an individual component proven faulty without evidence.

### Recovery verification

`uv run pytest -q` passed **927 tests**, with the existing Starlette/httpx
deprecation warning, on the pulled `1919e4b` tree. This is offline software
validation, not proof of board recovery. No additional software tests are
needed for the documentation updates alone. The required hardware evidence
remains missing: reliable PONG/STOP, `CAPS guarded=1 cruise=1`, lifted motion
and interruption checks, and battery voltage during load. Root cause remains
unproven. No claim of a half-written sketch or bootloader damage is made.

## PI-04 recovery on the replacement Pi (PI-05)

New-host observations on 2026-11-08, starting at 04:08 EST, from a fresh
clone of `main` at `19d3927` into `/home/teamrover/Team-14`. This is the
replacement Pi, not a continuation of the old host's device identities.
Recovery remains **pending**; no successful new-host rover communication,
firmware upload, motion test or full pipeline run is established.

### Host and setup

- Raspberry Pi 5 Model B Rev 1.1, hostname `raspberrypi` (distinct from the
  old `heartbeat` host), Debian 13/trixie arm64, kernel
  `6.12.47+rpt-rpi-2712`.
- Root is already on `/dev/nvme0n1p2`, ext4, with about 443 GiB available.
  No storage formatting, boot configuration or accelerator changes were made.
- GitHub CLI was installed and authenticated as `zukhriddingit`; the clone
  uses HTTPS without credentials in its URL.
- `bash scripts/pi_setup.sh` completed as `teamrover`. OS audio packages,
  uv 0.13.7, Python 3.11.18 and the locked project dependencies including the
  CPU ML extra were installed. The account already has audio/dialout access.
  No motor-controller service was installed or enabled.
- YAMNet downloaded and passed CPU inference with 15,600 float32 input
  samples and 521 class scores (about 2.7 ms for the recorded extraction).
  This checks inference, not live microphone recognition.
- Both the bundled recovery HEX and firmware source match the SHA-256 values
  in [the preserved artifact README](evidence/rover-recovery/README.md).
  No old `/tmp` build was assumed to exist on this host.
- `uv run pytest -q` passed **927 tests** in 144.26 seconds, with the existing
  Starlette/httpx deprecation warning. The application tree remains `19d3927`;
  the recovery record is a documentation-only change. The suite was run once.
- The documented `scripts/clap_pi.sh` wrapper installed its separate Python
  3.12.16 environment and CPU-only torch 2.9.1, cached
  `laion/clap-htsat-unfused`, and started with `profiles/pi-full-check.yaml`.
  `/health` returned the expected `heartbeat-clap-v1` contract (16 kHz,
  15,600-sample clips, monitor enabled). Synthetic-silence `/classify` and
  `/observe` calls returned finite scores: 10 scores in about 651 ms and 14
  scores in about 704 ms respectively. These are one-off protocol/inference
  checks, not recognition accuracy or sustained-throughput validation. The
  service binds to `127.0.0.1:8767`; it is not a boot-enabled controller.
- No `.env.local` exists in this fresh checkout. No credential was copied,
  printed or committed, and no Decisions API call was made.

### Read-only host checks and actual device inventory

`lifescout.service` is absent (`not-found`, inactive). No LifeScout, patrol,
serial diagnostic or uploader was found running. ModemManager is active;
this alone does not establish port interference. The initial
`vcgencmd get_throttled` returned `0x0`, and the initial inspected kernel log
contained no matching under-voltage or over-current records. Physical
wall-supply and battery condition are not established by these initial readings.

**Later power fault:** after setup/inference checks, `get_throttled` returned
`0x50000` at 04:12 EST and on recheck. Kernel records show `Undervoltage
detected!` at 04:11:40 and 04:12:24, and `Voltage normalised` at 04:11:46 and
04:12:28. Bits 16 and 18 record historical undervoltage and throttling;
the sampled current-state bits are zero. See the official
[get_throttled bit definitions](https://www.raspberrypi.com/documentation/computers/os.html#get_throttled).
These are new-host faults, not evidence about Auriga battery voltage or the
cause of the old serial failure. The operator was asked to confirm the
official Raspberry Pi 27 W wall supply. PI-04's power gate is unresolved;
do not proceed with hardware recovery based on the earlier `0x0` reading.

At 04:10–04:12 EST, USB/sysfs enumeration showed:

| Device | USB identity | Node | Physical path |
| --- | --- | --- | --- |
| M5Stick microphone | `0403:6001`, `Hades2001_M5stack_81520161EE` | `/dev/ttyUSB0` | `/dev/serial/by-path/platform-xhci-hcd.1-usbv2-0:1:1.0-port0` |
| u-blox GPS | `1546:01a9` | `/dev/ttyACM0` | `/dev/serial/by-path/platform-xhci-hcd.0-usbv2-0:1:1.0` |
| Auriga rover | Expected CH340 `1a86:7523` | **Absent** | **Not established** |

These paths must be rechecked after replugging. In particular, `ttyUSB0`
currently belongs to the microphone and must not receive rover commands or
a firmware upload. No serial discovery/probing was performed.

`timeout 10 rpicam-hello --list-cameras` reported `No cameras available!`.
`aplay -l` listed only `vc4hdmi0` and `vc4hdmi1`; `arecord -l` listed no ALSA
capture devices. The M5Stick uses serial transport, so the latter does not
mean the stick microphone is faulty. No camera capture, audio capture or
speaker playback was attempted. A camera ribbon check must be done with Pi
power off. No GPIO/AI/audio overlays were copied from the old Pi.

### Operator confirmation and link-test gate

The operator initially said everything was connected and requested checks.
Asked specifically for the isolated recovery setup, the operator then
confirmed: "Ready: motors unplugged, rover alone; authorize PING". This
authorizes the first bounded no-motion link test, not flashing or motion.
However, USB still showed the microphone/GPS and no Auriga after that reply.
The operator was asked to connect the Auriga with a USB data cable, keeping
both motor leads unplugged. Physical isolation is operator-reported and USB
isolation has not been verified.

No rover port has been opened and no PING has been sent on this host because
the authorized target is absent. No fuser check, CAPS/STOP/STATUS exchange,
battery estimate, power cycle, upload or motion test has been reached.
Battery freshness/charge, multimeter voltage, wiring inspection and removal
of any wireless module remain unverified. There is no new evidence identifying
the cause of the old rover fault.

After the power gate is resolved and the isolated Auriga enumerates, identify
its actual ttyUSB node, check
`fuser -v ACTUAL_NODE`, then run the already authorized single
`timeout 15 uv run --no-sync hb rover --port ACTUAL_NODE ping`. A successful
PONG is required before CAPS/STOP/STATUS; a new port-opening step, power cycle,
upload or lifted motion check still follows PI-04's operator confirmations.
Keep the motor leads disconnected and do not start an armed patrol.

Software-only readiness can be checked from `/home/teamrover/Team-14` with:

```bash
export PATH="$HOME/.local/bin:$PATH"
curl --fail --max-time 5 http://127.0.0.1:8767/health
vcgencmd get_throttled
ls -l /dev/serial/by-id/ /dev/serial/by-path/
```

If the local CLAP process has been stopped, restart it in a separate terminal
using the cached model (no `--allow-download` needed):

```bash
bash scripts/clap_pi.sh --profile profiles/pi-full-check.yaml
```

Firmware, profiles, Python code, contracts and boot settings are unchanged.
Do not repeat the offline suite for this documentation update. PONG/CAPS/STOP,
live microphone transport, a GPS fix, camera capture, speaker output, voltage
under rover load and supervised full-pipeline behavior remain unverified here.

## Earlier validation scope

The earlier full suite passed 920 tests before the continuous-mode change.
Continuous-mode controller/serial checks subsequently passed 132 tests, and the
firmware host simulation and AVR build passed. Full-suite results must not be
represented as validation of the later continuous-mode revision. The initial
incident record was documentation-only; the subsequent isolated serial
diagnostics are detailed above. No additional software test suite was run for
these evidence/documentation changes. Audio and dialog contracts are unchanged;
the CRUISE/CAPS protocol
addition is documented in [DECISIONS.md](DECISIONS.md#continuous-forward-mode).
