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
`/dev/ttyUSB1`; after reconnects it resolved to `/dev/ttyUSB0`. Always use the
stable identity: the microphone also uses a numbered ttyUSB node.

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

USB monitoring was enabled on the Pi, but no new serial connection or raw
transfer experiment was started. Confirmation that both motor leads are
physically disconnected is pending. A filtered raw USB trace can establish
whether zeros already arrive in the adapter's USB payload; it cannot by itself
distinguish intentional MCU output from an electrical problem at the adapter's
UART input. That distinction may require physical signal measurement.

## Validation scope

The earlier full suite passed 920 tests before the continuous-mode change.
Continuous-mode controller/serial checks subsequently passed 132 tests, and the
firmware host simulation and AVR build passed. Full-suite results must not be
represented as validation of the later continuous-mode revision. No additional
tests or hardware commands were run to prepare this documentation-only incident
record. Audio and dialog contracts are unchanged; the CRUISE/CAPS protocol
addition is documented in [DECISIONS.md](DECISIONS.md#continuous-forward-mode).
