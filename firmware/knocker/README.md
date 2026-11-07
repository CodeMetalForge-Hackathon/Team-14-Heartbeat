# Servo knocker

The sentinel sends a serial command; the board drives a short servo pattern,
returns the hammer to rest, and detaches the servo to stop idle hum. The primary
target is an Uno-class ATmega328P board with a Grove shield. The same sketch also
supports ESP32 with Arduino core 3.x.

## 1. Wiring

Disconnect power while wiring. These instructions and the commands below are for
a human at the bench; automated checks do not open a serial port or move a servo.

| Connection | Uno / Grove shield | ESP32 |
| --- | --- | --- |
| Servo signal | D5; plug a Grove servo into Grove port D5 | GPIO 13 |
| Optional status LED | D4 / Grove LED | GPIO 2 |
| Optional button | D2 / Grove button, active HIGH | GPIO 0 / BOOT, active LOW |

- **Bare SG90:** brown to GND, red to **5V, never 3.3 V**, orange to D5
  (GPIO 13 on ESP32).
- Keep a common ground between the controller and servo supply. Use a regulated
  5V servo supply if the board's supply cannot provide enough current.
- If the board resets when the servo moves, put a **470 µF capacitor** across the
  servo's 5V and GND, observing the capacitor's polarity.
- A bare LED needs a series resistor; the Grove LED module includes one.
- The optional Uno button is disabled by default so an unconnected D2 cannot
  trigger strikes. Set `KNOCKER_ENABLE_BUTTON` to `1` in the Uno section near the
  top of `knocker.ino` before compiling with a Grove button attached. A bare
  active-HIGH button needs an external **10 kΩ pull-down from D2 to GND** and
  connects D2 to 5V when pressed. Disable the feature again if removing it.
- The ESP32 button uses the internal pull-up and is enabled by default. Press
  BOOT only after startup; holding GPIO 0 LOW during reset enters the bootloader.
  A button press runs the same three strikes as `TEST`. Holding it does not
  repeat the pattern; release it before pressing again.

## 2. Mechanical build

1. Zip-tie a popsicle stick to the servo horn. Secure an **M4–M6 bolt or nut** to
   its end as the hammer head.
2. Hot-glue or tape the servo to a block so the hammer rests **1–2 cm above the
   slab**. Keep the mount rigid and the swinging head away from fingers.
3. Fit the horn so the default rest angle is a useful starting point. Tune the
   swing with `CAL`; valid angles alone do not guarantee mechanical clearance.
4. Place the knocker at least 30 cm from the contact mic, then check that the
   mic hears the impact without clipping.

Default calibration is `rest=60`, `strike=95`, `hold=70` milliseconds. Calibration
is stored in EEPROM on Uno and Preferences on ESP32, and loaded at startup.
Invalid or absent stored calibration falls back to those defaults. `CAL` saves
settings without moving the servo; use a subsequent `knock` to try the swing.

## 3. Flashing

### Arduino IDE

1. Open `firmware/knocker/knocker.ino` in Arduino IDE.
2. For the kit board, select **Arduino Uno** (or the actual compatible board)
   and its serial port. Use the Arduino `Servo.h` library and the AVR core's
   EEPROM support.
3. For ESP32, select the matching board from **esp32 by Espressif Systems,
   version 3.x**, and install **ESP32Servo** through Library Manager.
   Preferences comes with the ESP32 core.
4. Configure the optional Uno button as described above, then Verify and Upload.
5. Open Serial Monitor at **115200 baud** with a newline or carriage-return
   ending. On reset, expect:

   ```text
   READY knocker v1 rest=60 strike=95 hold=70
   ```

   Saved calibration values replace the example values. Close Serial Monitor
   before running the Python bench tool so it can own the port.

### arduino-cli

With `arduino-cli`, the target core, and the appropriate servo library already
installed, run these commands from the repository root. Replace `<port>` with
the board's port before running the upload command.

```sh
arduino-cli compile --fqbn arduino:avr:uno firmware/knocker
arduino-cli upload -p <port> --fqbn arduino:avr:uno firmware/knocker
```

For ESP32:

```sh
arduino-cli compile --fqbn esp32:esp32:esp32 firmware/knocker
arduino-cli upload -p <port> --fqbn esp32:esp32:esp32 firmware/knocker
```

Uno-class boards typically appear as `/dev/cu.usbmodem*` or
`/dev/cu.usbserial*` on macOS, and `/dev/ttyACM*` or `/dev/ttyUSB*` on a Pi.
ESP32 USB-UART adapters can use the same `usbserial` / `ttyUSB` families.
If a Pi user cannot open the port, check the user's serial-device permissions.

## 4. Bench test

Run from the repository root using Python **3.11** and `uv`. The tool's only
third-party dependency is **pyserial**. Until the shared project includes it,
use the `--with pyserial` form below. Substitute your actual port for
`/dev/cu.usbmodem1101`.

1. Check the reply parser without hardware or pyserial:

   ```sh
   uv run --python 3.11 python tools/knocker_test.py --self-test
   ```

2. Connect the flashed board and find its port:

   ```sh
   uv run --python 3.11 --with pyserial python tools/knocker_test.py --list
   ```

3. Check the link. The tool waits about two seconds for reset, requires and
   prints the `READY` line, then prints replies with elapsed time.

   ```sh
   uv run --python 3.11 --with pyserial python tools/knocker_test.py --port /dev/cu.usbmodem1101 ping
   ```

   Expect `PONG knocker v1`. Some USB serial adapters, including ESP32 boards,
   do not reset on open. For those, press RESET / EN during the tool's startup
   wait (within about four seconds of opening the port). If `READY` does not
   arrive, the tool exits nonzero without sending a command; rerun and reset
   during that wait. Do not hold ESP32 BOOT while resetting.

4. Secure the hammer and command three strikes, 450 ms apart:

   ```sh
   uv run --python 3.11 --with pyserial python tools/knocker_test.py --port /dev/cu.usbmodem1101 knock 3 --interval 450
   ```

   Expect three impacts, LED on during each strike, return to rest, and
   `OK K 3`. After the pattern, the servo must stop humming.

5. Check the preset and, if connected and enabled, the optional button:

   ```sh
   uv run --python 3.11 --with pyserial python tools/knocker_test.py --port /dev/cu.usbmodem1101 test
   ```

   `TEST` and a button press each produce **three strikes at 450 ms** and
   `OK TEST`. A button held down must not repeatedly strike.

6. Save calibration, then test the new swing:

   ```sh
   uv run --python 3.11 --with pyserial python tools/knocker_test.py --port /dev/cu.usbmodem1101 cal 60 95 70
   uv run --python 3.11 --with pyserial python tools/knocker_test.py --port /dev/cu.usbmodem1101 knock 1 --interval 450
   ```

   Expect `OK CAL 60 95 70`. Repeat with mechanically suitable values, then
   power-cycle the board and verify the `READY` banner contains the saved values.

7. In Serial Monitor, check invalid commands such as `K 0 450`, `K 3 249`,
   `CAL 60 181 70`, and `CAL 60 95 29`. Each must return `ERR <reason>` with
   **no movement**. Also check ` ping `, `k 1 450`, CR-only, LF-only, and CRLF
   line endings. The Python tool rejects invalid arguments before sending them,
   so use Serial Monitor to exercise the firmware's own validation.

The bench tool exits nonzero on an `ERR`, an unexpected reply, serial failure,
or a response timeout. It allows enough time for the longest supported pattern.

## Serial protocol and timing

Use **115200 baud**, ASCII commands, and a CR, LF, or CRLF line ending. Commands
are case-insensitive; surrounding whitespace is ignored. An empty or
whitespace-only command returns `ERR empty command`. The LF following CR in
a CRLF pair is treated as part of the same terminator, without an extra reply.

| Command | Reply | Limits |
| --- | --- | --- |
| `PING` | `PONG knocker v1` | No arguments |
| `K <n> <interval_ms>` | `OK K <n>` | `n`: 1–9; interval: 250–2000 ms |
| `TEST` | `OK TEST` | Three strikes at 450 ms |
| `CAL <rest_deg> <strike_deg> <hold_ms>` | `OK CAL <rest_deg> <strike_deg> <hold_ms>` | Angles: 0–180; hold: 30–200 ms |
| Invalid command or value | `ERR <reason>` | No motion |

Each strike attaches the servo, commands the strike angle, holds for `hold_ms`,
commands the rest angle, and waits the remainder of `interval_ms`. The LED is
on during the hold. After the final interval, the board waits another **150 ms**
and detaches before acknowledging completion. Pattern time is approximately
`n * interval_ms + 150 ms`; the sentinel can keep its response gate closed
through the completion reply and its configured gate tail.

Patterns are blocking. Commands arriving during one are handled after it
finishes, subject to the board's small serial receive buffer. **Send one command
and wait for its reply before sending the next.** Do not press the test button
during a host-driven dialog. Disconnecting the host does not cancel a pattern
already running; remove servo power if necessary.

P02 owns only the sketch, this guide, and the bench tool. Its required defaults,
limits, and timing values are named constants in those files; calibration is
adjusted through `CAL`. No shared host profiles or protocol contracts are changed.
