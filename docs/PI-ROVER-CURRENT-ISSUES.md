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

## Validation scope

The earlier full suite passed 920 tests before the continuous-mode change.
Continuous-mode controller/serial checks subsequently passed 132 tests, and the
firmware host simulation and AVR build passed. Full-suite results must not be
represented as validation of the later continuous-mode revision. The initial
incident record was documentation-only; the subsequent isolated serial
diagnostics are detailed above. No additional software test suite was run for
these evidence/documentation changes. Audio and dialog contracts are unchanged;
the CRUISE/CAPS protocol
addition is documented in [DECISIONS.md](DECISIONS.md#continuous-forward-mode).
