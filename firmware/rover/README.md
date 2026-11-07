# Heartbeat Ranger firmware

This sketch turns an mBot Ranger in its tracked Land Raider assembly into a USB
serial rover. It targets the Me Auriga's ATmega2560 and its two onboard encoder
motor connections, SLOT1 and SLOT2. It does not run the sentinel dialog or make
rescue decisions. The host must stop the rover, allow it to settle, and refuse
movement while listening.

## Install and flash — human-operated

Run these from the repository root. These commands are instructions for the human
operator; automated checks do not open the serial port or power the motors.

```bash
arduino-cli core update-index
arduino-cli core install arduino:avr
arduino-cli config set library.enable_unsafe_install true
arduino-cli lib install --git-url https://github.com/Makeblock-official/Makeblock-Libraries.git
arduino-cli compile --fqbn arduino:avr:mega:cpu=atmega2560 firmware/rover
arduino-cli board list
arduino-cli upload --fqbn arduino:avr:mega:cpu=atmega2560 --port /dev/cu.YOUR_RANGER firmware/rover
```

The official repository has `library.properties` and `src/` at its root, so the
Git install has the expected Arduino library layout. If your CLI cannot perform
the Git install, download the repository's ZIP from its Code menu, then use
`arduino-cli lib install --zip-path /absolute/path/Makeblock-Libraries-master.zip`.
Remove conflicting older Makeblock copies if the compiler reports ambiguous
symbols. The `library.enable_unsafe_install` switch is needed for Git/ZIP installs.

Use the actual port shown by `board list`; a Pi usually uses `/dev/ttyUSB0` or
`/dev/ttyACM0`. Uploading or opening USB serial may reset the Auriga. The sketch
boots with motor output zero, the ring off, and exactly `READY rover v1`.

For first movement checks, support the chassis with the tracks off the ground.
Keep its power switch accessible. Check `ping`, `status`, `stop`, and a small
movement before placing it on a flat test surface. The motor battery supply must
be connected for movement; a USB connection alone does not establish that the
motor power rail is available.

```bash
uv run hb rover --port /dev/cu.YOUR_RANGER ping
uv run hb rover --port /dev/cu.YOUR_RANGER status
uv run hb rover --port /dev/cu.YOUR_RANGER stop
uv run hb rover --port /dev/cu.YOUR_RANGER led 255 0 0
uv run hb rover --port /dev/cu.YOUR_RANGER drive 5
uv run hb rover --port /dev/cu.YOUR_RANGER turn 10
```

## Protocol

115200 baud, ASCII, one command per LF-terminated line. CRLF is also accepted.
Commands are uppercase. Numeric arguments use finite decimal notation, with an
optional sign, decimal point, and exponent. Extra tokens, NaN, infinity, hex
numbers, non-ASCII bytes, and lines longer than 95 characters are rejected.

| Command | Reply / meaning |
| --- | --- |
| boot | `READY rover v1` |
| `PING` | `PONG rover v1` |
| `DRIVE <cm> <speed_pct>` | `OK DRIVE <cm>` after both encoder targets are reached; negative distance reverses |
| `TURN <deg> <speed_pct>` | `OK TURN <deg>` after both encoder targets are reached; in-place turn |
| `STOP` | `OK STOP`, after immediately setting both motor outputs to zero |
| `HB` | No reply; refreshes the movement watchdog |
| `STATUS` | `STATUS batt=<V> moving=<0/1> enc=<left>,<right>` |
| `LED <r> <g> <b>` | `OK LED <r> <g> <b>`; integer channels 0–255; all 12 ring LEDs |
| `CAL <deg_per_cm> <deg_per_turn_deg>` | `OK CAL <deg_per_cm> <deg_per_turn_deg>` after EEPROM write/read verification |
| invalid input or fault | `ERR <reason>`; both motor outputs are zero |

`OK LED` and `OK CAL` make these commands usable by host methods that wait for an
acknowledgement. A canceled move never produces a later `OK DRIVE` or `OK TURN`.
`STOP` is parsed before the next motion update, including when a move would
otherwise finish on that same iteration. It bypasses normal speed ramping.

While moving, `HB`, `PING`, `STATUS`, and `STOP` are supported. Another motion,
`CAL`, or `LED` returns `ERR busy` and stops the current movement. Requiring the
rover to be stopped for LED updates avoids the ring driver's interrupt masking
interfering with encoder counting. Every accepted command refreshes the watchdog;
incomplete bytes do not. A raw serial terminal must send `HB` at least every
0.5 seconds throughout a move; the host does this automatically.

Firmware limits are `abs(cm) <= 100`, `abs(deg) <= 180`, and
`1 <= speed_pct <= 60`, including fractional speed percentages. A zero distance
or angle acknowledges immediately without enabling a motor. A nonzero movement
smaller than one encoder pulse returns `ERR resolution`.

`batt` is the nominal Auriga A4 reading scaled by `15 / 1024`, following
Makeblock's firmware. Verify it against a multimeter; it is not a calibrated
measurement. `enc` reports signed wheel/output-shaft degrees since boot, rounded
to whole degrees, in native SLOT1/SLOT2 directions. Positive driving runs SLOT1
positive and SLOT2 negative. Positive turns run both slots positive. The physical
forward and turn directions still need checking on the actual assembly.

## Calibration

The fallback values are **18.0 wheel degrees/cm** and **2.5 wheel degrees per
chassis turn degree**. These approximate values are mirrored in the host profile;
they have not been measured on this rover. The MCU cannot load YAML. A valid
EEPROM record overrides its fallback values until another `CAL` is accepted.

```bash
uv run hb rover --port /dev/cu.YOUR_RANGER calibrate
```

The interactive host calibration writes the profile baseline with `CAL` first,
so an unknown previous EEPROM calibration is not silently assumed. It commands
a 50 cm drive at the calibration speed, asks for the measured distance, then
computes:

```text
new_deg_per_cm = baseline_deg_per_cm * 50 / measured_cm
```

It saves that value with `CAL`, then performs a 90 degree turn and asks for the
measured absolute angle:

```text
new_deg_per_turn_deg = baseline_deg_per_turn_deg * 90 / measured_degrees
```

Repeat on the intended surface with the payload attached. Track slip and uneven
debris make encoder travel an estimate of chassis travel. Both calibration
values must be finite and within `[0.001, 100000]`; this is a numeric safety bound,
not an expected physical range. Calibrate only while stopped. `CAL 18 2.5` restores
the initial approximate values. Record the final measured values in the host
profile as well, especially before rerunning a calibration that starts from it.

The versioned record uses the last 16 bytes of the Mega's EEPROM, contains both
floats and a checksum, and is read back before acknowledging a write. An absent,
corrupt, or out-of-range record falls back to the approximate defaults. A failed
write produces `ERR eeprom` and leaves motor output zero.

## Movement and safety behavior

The sketch uses the official `MeEncoderOnBoard` direct PWM API and encoder
interrupts. It takes atomic snapshots of the AVR's 32-bit pulse counters and
stops each wheel independently at its measured target. It does not hold position
with PID after a move. Normal output rises at a bounded rate and decreases
through the final quarter of encoder travel. A friction floor capped by the
requested speed keeps the final approach finite; each target still cuts output
immediately. This is a simple motion ramp, not closed-loop matched wheel speed.

The following are fixed firmware safety bounds or board facts, rather than route
tunables: the command ceilings, 20 ms control interval, 240 PWM units/second slew
bound, 25 PWM-unit friction floor from Makeblock's `PWM_MIN_OFFSET`, 1.5 second
communication watchdog, 1.5 second per-wheel encoder stall guard, and 30 second
absolute move deadline. A wrong encoder direction beyond two pulses also stops
the move. The host profile chooses speed and calibration within these bounds;
it can apply stricter limits.

A lost heartbeat produces `ERR watchdog`. A wheel without encoder changes
produces `ERR stall`. A move exceeding 30 seconds, even with heartbeats, produces
`ERR move_timeout`. Reversed encoder progression produces `ERR encoder_direction`.
Malformed commands also stop an active move before reporting their error.
The serial parser handles at most 64 bytes per iteration, so incoming data cannot
indefinitely prevent the motion watchdog from running. No movement code waits
for a target or sleeps.

`OK STOP` means motor output is disabled; it does not prove the chassis has
finished coasting. The host must allow debris and the chassis to settle before
opening detection. This USB watchdog is software protection, not a physical
emergency power disconnect. Humans must verify stopping distance, motor wiring,
direction, encoder operation, wheel slip, and the watchdog with the real rover.

## Sources and validation

API and wiring references were inspected at official Makeblock commit
`21b20bbd55bbaaaae6ac14a4580d0a238c8bbd4d`:

- [Auriga direct encoder example](https://github.com/Makeblock-official/Makeblock-Libraries/blob/21b20bbd55bbaaaae6ac14a4580d0a238c8bbd4d/examples/Me_On_Board_encoder/Me_Auriga_encoder_direct/Me_Auriga_encoder_direct.ino): SLOT1/SLOT2 interrupt setup and motor PWM timers.
- [Auriga position example](https://github.com/Makeblock-official/Makeblock-Libraries/blob/21b20bbd55bbaaaae6ac14a4580d0a238c8bbd4d/examples/Me_On_Board_encoder/Me_Auriga_encoder_pid_pos/Me_Auriga_encoder_pid_pos.ino): encoder pulse count, gear ratio, and native forward directions.
- [Encoder implementation](https://github.com/Makeblock-official/Makeblock-Libraries/blob/21b20bbd55bbaaaae6ac14a4580d0a238c8bbd4d/src/MeEncoderOnBoard.cpp): direct output, pulse/degree conversion, and motion modes.
- [Onboard LED ring example](https://github.com/Makeblock-official/Makeblock-Libraries/blob/21b20bbd55bbaaaae6ac14a4580d0a238c8bbd4d/examples/Me_RGBLed/MeAurigaOnBoardLEDRing/MeAurigaOnBoardLEDRing.ino): 12 LEDs on D44.
- [Auriga firmware](https://github.com/Makeblock-official/Makeblock-Libraries/blob/21b20bbd55bbaaaae6ac14a4580d0a238c8bbd4d/examples/Firmware_for_Auriga/Firmware_for_Auriga.ino): A4 battery conversion.

On 2026-11-07 this development machine had arduino-cli 1.5.1 and Arduino AVR core
1.8.8, but no Makeblock library. No toolchain/library was installed and no target
compile, upload, serial connection, or physical motion was attempted. A native
C++ harness compiled the actual sketch against fake serial, encoder, ring, and
EEPROM APIs with AddressSanitizer and UndefinedBehaviorSanitizer. It checked
protocol replies, EEPROM persistence/corruption, strict numeric parsing and
2,000 malformed-input cases, motion limits, signed moves, ramping, independent
wheel completion, STOP priority/cancellation, heartbeat expiry, stall handling,
and the absolute deadline. This does not replace the target compile or hardware
checks above.

## Restore the Ranger's original firmware

Stop the rover and close any Heartbeat process or serial terminal that owns its
USB port. Open mBlock, select/connect the **mBot Ranger / Me Auriga**, and use its
**Update firmware** action to restore the stock Ranger firmware. Follow mBlock's
completion instructions before disconnecting it. This replaces Heartbeat's
sketch. EEPROM calibration may survive reflashing; a later Heartbeat `CAL` can
overwrite it. Restoring stock firmware has not been tested on this rover.

## Stop–listen–move patrol

First exercise the complete route without any connected hardware:

```bash
uv run hb patrol --dry-run --route profiles/route.yaml
uv run pytest -q tests/test_rover.py tests/test_patrol.py
```

The synthetic survivor answers at Gap 2 only. The expected result table is
`NO_SIGNS`, `CONFIRMED`, `NO_SIGNS`, with the knock interview answers recorded
for Gap 2. `NO_SIGNS` means no positive response was collected at that stop.

After flashing, checking motor direction with the tracks raised, and calibrating
on the real surface, a human can start a connected patrol:

```bash
uv run hb patrol --rover /dev/ROVER --device stick --knocker /dev/KNOCKER --route profiles/route.yaml --dashboard
```

Each route leg runs with detection gated. The host waits for STOP, leaves the
motors off for the configured settling interval, then opens detection and runs
the deterministic dialog. It waits for the complete interview after confirmation.
Background noise history survives every gap reset. A timed-out prompt finishes
before the next movement begins. Movement never runs during an active prompt,
response window, pre-listen check, or interview.

Pause sends STOP immediately. Resume settles and listens at the actual stopped
position; it does not repeat an unknown portion of an interrupted drive or turn.
An interrupted leg marks `interrupted_move` and latches `arrival_unverified` for
that result and later relative route positions. Re-establish the rover's position
and start a new patrol if an exact arrival is needed. Abort and E-STOP send STOP
before recording the control action; E-STOP stays latched for the patrol.

Motor distance, turn accuracy, settling time, debris coupling, speaker feedback,
battery voltage, and physical emergency-stop latency still require hardware checks.

## Patrol dashboard

Try the dashboard without hardware, then open the local URL printed by the CLI:

```bash
uv run hb patrol --dry-run --route profiles/route.yaml --dashboard
```

The patrol panel keeps one row per gap with its status, odds, interview answers,
and operator approval. Choose **Approve dig** on a confirmed gap and enter your
initials; the approval names that gap and survives a page reload within the same
session. The main dialog controls are disabled while patrol owns the dialog.

The red **E-STOP** sends STOP immediately and latches the patrol against resuming.
It remains available during another request and inside the approval dialog. A
lost connection cannot guarantee delivery; use the rover's power switch if STOP
cannot be acknowledged. Terminal controls are `p` (pause), `r` (resume), `q`
(abort), and `e` or Space (E-STOP), followed by Enter; Ctrl-C also stops first.

The additive integration contracts are the `rover`, `patrol`, `gap_result`, and
`patrol_done` events; `approval.data.gap` for per-gap approval; an optional
`patrol` snapshot in `/api/state`; and `POST /api/rover` with
`{"name":"stop","args":{}}` (also `pause`, `resume`, `abort`, and `approve`).
Approval args are `{"gap":"Gap 2","operator":"AB"}`. The endpoint exposes no
arbitrary drive operation. Non-patrol sessions retain their existing controls.
