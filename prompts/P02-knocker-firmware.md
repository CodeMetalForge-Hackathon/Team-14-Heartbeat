# P02: servo knocker firmware and bench tool

Read `AGENTS.md` first. **Lane B, first task.** It runs in parallel with P01, so touch only the files listed here.

## Goal
A microcontroller that strikes the debris with a servo "hammer" when the sentinel tells it to, using the serial protocol in AGENTS.md.

## Files you own
- `firmware/knocker/knocker.ino`
- `firmware/knocker/README.md`
- `tools/knocker_test.py`

## Targets
- **Primary: the kit's Arduino-compatible board** (ATmega328P class, Uno-style, with a Grove shield).
  - Servo signal on **D5** (Grove port D5).
  - Optional status LED on **D4** (Grove LED).
  - Optional button on **D2** (Grove button): a press does 3 test strikes.
  - Use the built-in `Servo.h` and EEPROM.
- **Also ESP32** (Arduino core 3.x), behind `#if defined(ESP32)`.
  - Use the `ESP32Servo` library and `Preferences` for storage.
  - Servo on GPIO 13, LED on GPIO 2, button on GPIO 0.

## Behavior
- Implement the protocol exactly as specified in AGENTS.md.
- **Parsing:** trim whitespace, accept any case and either CR or LF line endings.
- **Range checks.** Anything out of range gets `ERR <reason>` and nothing moves:

  | Parameter | Range |
  |---|---|
  | `n` | 1–9 |
  | `interval_ms` | 250–2000 |
  | angles | 0–180 |
  | `hold_ms` | 30–200 |

- **One strike:**
  1. Attach the servo.
  2. Write the strike angle.
  3. Wait `hold_ms`.
  4. Write the rest angle.
  5. Wait out the rest of `interval_ms`.
- **After a pattern:** wait 150 ms, then **`detach()` the servo**. An attached idle servo hums, and the contact mic hears it.
- **LED** is on during each strike.
- **Calibration:**
  - Defaults: `rest=60`, `strike=95`, `hold=70`.
  - `CAL` saves new values to storage, and they load on boot.
- **On boot**, print `READY knocker v1 rest=<r> strike=<s> hold=<h>`.
- **During a pattern**, the simple blocking implementation is fine. Commands that arrive during a pattern are handled after it finishes; document this.

## `tools/knocker_test.py`
A pyserial CLI that depends only on pyserial. If P01 hasn't added pyserial yet, use `uv run --with pyserial python tools/knocker_test.py ...`.
```
python tools/knocker_test.py --list
python tools/knocker_test.py --port /dev/cu.usbmodem1101 ping
python tools/knocker_test.py --port /dev/cu.usbmodem1101 knock 3 --interval 450
python tools/knocker_test.py --port /dev/cu.usbmodem1101 test
python tools/knocker_test.py --port /dev/cu.usbmodem1101 cal 60 95 70
```
- After opening the port, wait about 2 s for the board to reset, and print the `READY` line.
- Print every reply with its round-trip time.
- Exit with a nonzero code on `ERR` or a timeout.
- Add `--self-test` that checks the reply parser against canned strings. It needs no hardware.

## `firmware/knocker/README.md`
Cover these sections:
1. **Wiring.**
   - Grove servo: plug it into D5.
   - Bare SG90: brown to GND, red to **5V** (never 3.3 V), orange to D5 (or GPIO 13 on ESP32).
   - Keep a common ground.
   - If the board resets when the servo moves, put a 470 µF capacitor across the servo's 5V and GND.
2. **Mechanical build.**
   - Zip-tie a popsicle stick to the servo horn, with an M4–M6 bolt or nut on its end as the hammer head.
   - Hot-glue or tape the servo to a block so the hammer rests 1–2 cm above the slab.
   - Tune the swing with `CAL`.
3. **Flashing.**
   - Arduino IDE.
   - arduino-cli: `arduino-cli compile --fqbn arduino:avr:uno firmware/knocker` and `arduino-cli upload -p <port> --fqbn arduino:avr:uno firmware/knocker`. For ESP32 the FQBN is `esp32:esp32:esp32`.
   - The Uno-class board shows up as `/dev/cu.usbmodem*` or `/dev/cu.usbserial*` on macOS, and `/dev/ttyACM*` or `/dev/ttyUSB*` on the Pi.
4. **Bench test:** a step-by-step checklist using `knocker_test.py`.

## Done when
- If `arduino-cli` is installed, the sketch compiles for `arduino:avr:uno`. If it isn't installed, say so; **do not install toolchains without asking.**
- `--self-test` passes.

Commit only your files: `P02: servo knocker firmware + bench tool`. Report what a human must do to flash the board and test it.
