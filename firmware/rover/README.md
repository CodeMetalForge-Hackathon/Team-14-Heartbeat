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

If the installed Makeblock library reports duplicate `mePort` or `encoder_Port`
definitions from `MeMegaPi.cpp`, add `dot_a_linkage=true` to that library's
`library.properties` file. Arduino's [archive linking setting](https://docs.arduino.cc/arduino-cli/library-specification/)
lets the linker select the required objects and retain the sketch's Auriga
pin map. The isolated Pi build in `runs/noecho-firmware-toolchain` uses this
setting with Makeblock commit `21b20bb` and AVR core 1.8.8.

Use the actual port shown by `board list`; a Pi usually uses `/dev/ttyUSB0` or
`/dev/ttyACM0`. Uploading or opening USB serial may reset the Auriga. The sketch
boots with motor output zero, the ring off, and exactly `READY rover v1`.

The host retries identity PING on the same connection every 0.25 seconds, up
to a five-second startup deadline (`rover.connect_retry_s` and
`rover.connect_timeout_s`). This tolerates the AVR bootloader discarding an
early request after USB opens. Only `PONG rover v1` completes the handshake;
READY alone, silence, or another firmware's identity never authorizes a move.
Movement commands are not retried automatically.

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
| `CAPS` | `CAPS guarded=1 cruise=1 noecho=1 approach=1`; supported guarded features |
| `RANGE <port>` | `RANGE port=<port> cm=<distance> valid=<0/1>`; stationary only |
| `NAVCFG <port> <stop_cm> <clear_cm> <step_cm> <turn_deg> <max_turns> <poll_ms> <settle_ms> <turn_clearance_cm> <range_retry_ms> <range_good_reads>` | `OK NAVCFG ...`; validated settings, stationary only, RAM only |
| `NOECHO <0\|1>` | `OK NOECHO ...`; opt in to camera-directed guarded motion on missing echoes, after NAVCFG and while stopped |
| `APPROACH <cm> <speed_pct> <max_ms> <stop_cm>` | `OK APPROACH ...`; short guarded debris approach, completing at the encoder target or close range target |
| `EXPLORE <seconds> <speed_pct>` | `OK EXPLORE <seconds>` only when the bounded period ends; STOP cancels it |
| `DRIVE <cm> <speed_pct>` | `OK DRIVE <cm>` after both encoder targets are reached within tolerance; negative distance reverses |
| `TURN <deg> <speed_pct>` | `OK TURN <deg>` after both encoder targets are reached within tolerance; in-place turn |
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

`NOECHO 1` applies only to host-directed STEP, PIVOT and CRUISE commands. Unknown
echoes then defer the movement decision to the host camera policy; valid close
readings still stop or reject motion. Continuous no-echo travel streams
`NAV NO_ECHO -1.00`, retaining an explicitly unknown distance. Watchdog, encoder,
stall, action duration and STOP guards still apply. The default is `NOECHO 0`,
and every successful NAVCFG and boot restores that default. MCU-only EXPLORE
retains its existing stopped no-echo retry behavior. Use matching host and MCU
versions: the host refuses to arm this mode without `CAPS noecho=1`.

APPROACH is a separate close-positioning primitive: forward distance at most
5 cm, power at most 35%, duration at most 2000 ms, and stopping gap 3–15 cm.
It samples usable front range before and during travel and requires NAVCFG.
The normal 30 cm cutoff does not apply to APPROACH; its own target does. A
missing echo aborts it even with NOECHO enabled. Reaching the measured gap stops
both motors immediately and acknowledges the exact accepted arguments. A
completed encoder step alone does not prove arrival: the host must obtain a
fresh image and close reading before listening. STOP, watchdog, stall, encoder
direction and action deadlines still apply. There is no bumper/contact input.
The new host checks `approach=1` before arming close-debris mode; update matching
host and firmware together. This close-approach revision was built and checked
without hardware; it has not been uploaded to the disconnected rover.

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
negative and SLOT2 positive, toward this assembled Ranger's ultrasonic sensor.
This corrects the observed backward movement with the prior mapping.
`DRIVE_FORWARD_POLARITY` is the firmware's mechanical orientation constant.
Negative drives reverse both directions. Positive turns still run both slots
positive. Check a short forward move after uploading before enabling exploration;
turn direction and angles still need checking on the actual assembly.

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
stops each wheel independently at its measured target for bounded moves. It does not hold position
with PID after a move. Normal output rises at a bounded rate. Straight drives and exploration turns
retain requested power through the final approach: the earlier distance slowdown
caused observed low-PWM stalls. Standalone TURN keeps its original slowdown.
Each encoder target cuts output independently within the bounded tolerance.
This controls PWM, not closed-loop matched wheel speed; test turn accuracy and
coasting distance after changing power behavior.

Movement completion permits at most **two encoder pulses** of shortfall per
motor, additionally capped at **1% of the requested pulse target**, rounded down.
Targets below 100 pulses still require exact completion. This avoids a false
stall when a nearly finished motor cannot overcome track friction at the final
low PWM. At the fallback calibration, two pulses correspond to roughly 1.13 mm
of commanded straight travel; this is encoder tolerance, not a claim of physical
distance accuracy. Both motors must independently enter tolerance before success.
A motor outside tolerance still has the same stall/watchdog/deadline protections.

The following are fixed firmware safety bounds or board facts, rather than route
tunables: the command ceilings, 20 ms control interval, 240 PWM units/second slew
bound, 25 PWM-unit friction floor from Makeblock's `PWM_MIN_OFFSET`, 1.5 second
communication watchdog, 1.5 second per-wheel encoder stall guard, and 30 second
absolute move/checkpoint deadline. During continuous exploration, the deadline
renews only after both wheels actually pass an encoder checkpoint; the per-wheel
stall timers never reset merely because a checkpoint rolled over. A wrong
encoder direction beyond two pulses also stops
the move. The host profile chooses speed and calibration within these bounds;
it can apply stricter limits.

A lost heartbeat produces `ERR watchdog`. A wheel without encoder changes
produces `ERR stall`. A move exceeding 30 seconds, even with heartbeats, produces
`ERR move_timeout`. Reversed encoder progression produces `ERR encoder_direction`.
Malformed commands also stop an active move before reporting their error.
The serial parser handles at most 64 bytes per iteration, so incoming data cannot
indefinitely prevent the motion watchdog from running. No movement code waits
for an encoder target or sleeps. Opt-in ultrasonic sampling has an explicit
24 ms pulse timeout and a 12 microsecond trigger pulse.

`OK STOP` means motor output is disabled; it does not prove the chassis has
finished coasting. The host must allow debris and the chassis to settle before
opening detection. This USB watchdog is software protection, not a physical
emergency power disconnect. Humans must verify stopping distance, motor wiring,
direction, encoder operation, wheel slip, and the watchdog with the real rover.

## Tracing a motor stall

Updated firmware includes a diagnostic snapshot in the existing ERR reply:

```text
ERR stall slots=1 progress=163,177 target=177 pwm=29,0 idle_ms=1500,1540 elapsed_ms=3012
```

This is an illustrative example, not a measurement from the robot. Both motor
outputs are disabled before printing. The 1.5-second stall limit and movement
power settings are unchanged. Reflash the updated sketch to receive these fields;
an old sketch continues to report only `ERR stall`.

- `slots`: unfinished motor slots whose encoder stopped changing (1, 2, or both).
- `progress`: SLOT1,SLOT2 encoder pulses since this movement began, normalized
  to the requested direction. These are not centimeters or absolute positions.
- `target`: required pulse count for each motor.
- `pwm`: last commanded signed PWM for SLOT1,SLOT2, on a -255 to 255 scale.
  This is commanded power, not measured torque/current. A completed motor has 0.
- `idle_ms`: time since each encoder last changed. A completed motor can have a
  large idle time without being stalled; use `slots` and `progress` together.
- `elapsed_ms`: controller time since this move began, not the audio stream clock.

Close patrol before a standalone test so only one process owns the serial port.
On an unobstructed test surface, a human can capture a short movement:

```bash
uv run hb rover --port /dev/cu.YOUR_RANGER drive 10 --speed 30 2>&1 | tee /tmp/rover-drive-trace.log
```

The same detailed error is printed and recorded in `events.jsonl` during patrol.
Progress near target with low PWM suggests the final approach may not overcome
track friction. Zero progress on a motor needs a power/wiring/mechanical/encoder
check. Encoder counts cannot distinguish a physically stuck motor from a failed
encoder; observe the tracks as well. Do not infer a cause from `stall` alone.

## Sources and validation

API and wiring references were inspected at official Makeblock commit
`21b20bbd55bbaaaae6ac14a4580d0a238c8bbd4d`:

- [Auriga direct encoder example](https://github.com/Makeblock-official/Makeblock-Libraries/blob/21b20bbd55bbaaaae6ac14a4580d0a238c8bbd4d/examples/Me_On_Board_encoder/Me_Auriga_encoder_direct/Me_Auriga_encoder_direct.ino): SLOT1/SLOT2 interrupt setup and motor PWM timers.
- [Auriga position example](https://github.com/Makeblock-official/Makeblock-Libraries/blob/21b20bbd55bbaaaae6ac14a4580d0a238c8bbd4d/examples/Me_On_Board_encoder/Me_Auriga_encoder_pid_pos/Me_Auriga_encoder_pid_pos.ino): encoder pulse count, gear ratio, and native forward directions.
- [Encoder implementation](https://github.com/Makeblock-official/Makeblock-Libraries/blob/21b20bbd55bbaaaae6ac14a4580d0a238c8bbd4d/src/MeEncoderOnBoard.cpp): direct output, pulse/degree conversion, and motion modes.
- [Onboard LED ring example](https://github.com/Makeblock-official/Makeblock-Libraries/blob/21b20bbd55bbaaaae6ac14a4580d0a238c8bbd4d/examples/Me_RGBLed/MeAurigaOnBoardLEDRing/MeAurigaOnBoardLEDRing.ino): 12 LEDs on D44.
- [Auriga firmware](https://github.com/Makeblock-official/Makeblock-Libraries/blob/21b20bbd55bbaaaae6ac14a4580d0a238c8bbd4d/examples/Firmware_for_Auriga/Firmware_for_Auriga.ino): A4 battery conversion.

The updated sketch compiled for `arduino:avr:mega:cpu=atmega2560` with Arduino
AVR core 1.8.8 and the pinned Makeblock library. `tools/check_rover_firmware.py`
compiles this same sketch against fake Arduino I/O with address/undefined-behavior
sanitizers and exercises navigation and stop guards without hardware. No
autonomous navigation upload or physical run was performed by the coding agent.

For the installed Makeblock library, `dot_a_linkage=true` in its
`library.properties` is needed to avoid linking unused MegaPi globals alongside
Auriga globals. The local test toolchain uses that archive-link setting.

## Autonomous exploration

See [ROVER-AUDIO](../../docs/ROVER-AUDIO.md#autonomous-local-exploration-ultrasonic-port-6)
for sensor validation and the CLAP exploration command. Navigation configuration
comes from YAML through NAVCFG; there is no movement or retained navigation mode
on boot. Ports 6–10 are accepted; the user's ultrasonic sensor is now on 6. Port 9's
line sensor is not used. EXPLORE requires configuration and lasts 1–600 seconds.
It emits `NAV FORWARD <cm>`, `NAV TURN <cm>`, and `NAV OBSTACLE <cm>` progress lines.
Forward exploration now rolls encoder checkpoints without cutting motor power;
turns still finish and settle before a fresh front reading. Internal movement
does not emit completion acknowledgements. STOP cancels
the entire exploration transaction and prevents a delayed OK EXPLORE reply.

Unknown echo, blocked search, overly close front obstacle, motor stall, wrong
encoder direction and lost heartbeat all disable outputs. Unknown echo enters a
bounded motor-off retry (`NAV SENSOR_WAIT -1`) instead of immediately aborting.
Two agreeing valid readings (profile default) produce `NAV SENSOR_READY <cm>`;
the fresh distance then determines forward/turn/too-close behavior. Invalid
readings reset recovery agreement, never its deadline. Expiry produces
`ERR range_unknown`. STOP/heartbeat loss/overall expiry cancel recovery.

NAVCFG has eleven arguments; its last two settings are the retry duration
(200–5000 ms) and required valid-reading count (2–5). The retry duration must
exceed settling plus poll time for all required readings plus the 24 ms echo
timeout. Host and firmware enforce the same bounds. Older nine-argument NAVCFG
is rejected so a partial upgrade cannot enable incompatible motion behavior.
The front sensor does not establish side clearance during turns or detect drops.

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
