# P07: M5StickC Plus SE as the sentinel microphone

Read `AGENTS.md` first. **Lane B, after P05.** P04 may be running in lane A and editing `heartbeat/cli.py` and `heartbeat/runner.py`, so keep your `cli.py` edits tiny, and **re-read the file right before every patch**.

## Why
- We have **no USB audio adapter**, and the Raspberry Pi 5 has **no audio input**.
- Our microphone is an **M5StickC Plus SE**:
  - ESP32-PICO-D4
  - built-in SPM1423 PDM MEMS mic
  - 135×240 LCD
  - buttons A and B
  - Grove port on G32/G33 (ADC1)
  - small battery
  - a USB-to-serial bridge (not native USB)
  - **no IMU**
- The stick plugs into the laptop or the Pi over USB and streams 16 kHz audio. Physically, its mic hole is sealed against the slab with putty, so it hears knocks *through the structure* and much less of the room.
- **Pitch value:** a $20 battery node with a screen is our "light at the edge" sentinel, while the Pi is the base.

## Files you own
- New: `firmware/stickmic/stickmic.ino`, `firmware/stickmic/README.md`
- New: `heartbeat/stickmic.py`, the host-side source and status sink
- New: `heartbeat/sources.py`, with `open_input(spec, cfg, bus)`
- `tests/test_stickmic.py`
- Minimal edits:
  - `heartbeat/cli.py`: replace the three direct `MicSource(...)` constructions with `open_input(...)`; make `hb devices` also list stick ports; in `hb run`, subscribe the status sink when the input is a stick.
  - `heartbeat/config.py`: **only** add `AudioConfig` fields with defaults: `stick_baud: int = 921600`, `stick_gain_db: float = 0.0`.
  - `profiles/rubble.yaml`: add the matching commented keys under `audio`.
  - `heartbeat/prompter.py`: only the knocker `auto` scan change described below.

## Firmware (`firmware/stickmic/stickmic.ino`)
**Platform**
- Arduino-ESP32 core 3.x with the **M5Unified** library.
- Find the board ID with `arduino-cli board listall | grep -i stick`. It's probably `esp32:esp32:m5stack_stickc_plus`.

**Serial protocol** (921600 baud)
- **Text mode** at boot:
  - Print `READY stickmic v1 rate=16000`.
  - `PING` → `PONG stickmic v1`
  - `START` switches to binary streaming.
  - Anything else → `ERR <reason>`, including the knocker's `K`/`CAL` commands.
- **Streaming mode:**
  - Send frames continuously.
  - Still accept `STOP` (back to text mode) and `STATUS <WORD> <free text>` lines. Read serial between frames, never blocking audio.
- **Frame format** (little-endian, 520 bytes per 16 ms, about 32.5 KB/s):

  | Bytes | Field |
  |---|---|
  | 2 | magic `0xA5 0x5A` |
  | 2 | `uint16` seq |
  | 2 | `uint16` n = 256 |
  | 512 | `int16` samples[256] |
  | 2 | `uint16` CRC-16/CCITT of seq + n + samples |

**Audio**
- Use `M5.Mic` at 16000 Hz mono, int16, recording continuously in 256-sample chunks with double buffering. Audio capture must never wait on the LCD.
- Expose `MAG <n>` to set the mic magnification, and save it to Preferences.

**Optional `MODE PIEZO`** (text command; mark it UNTESTED in the README)
- Instead of the PDM mic, sample **GPIO33** (Grove, ADC1) at 16 kHz using the core-3.x continuous ADC API.
- Remove DC with a running mean, scale to int16, and send the same frames.
- Document this bias circuit:
  - two 100 kΩ resistors from 3V3 to GND, making a 1.65 V midpoint
  - midpoint → 10 kΩ series resistor → GPIO33
  - piezo between the midpoint and GND
  - 1 MΩ across the piezo

  We may not have piezo discs; the built-in mic is the default.

**LCD**
- A full-screen status color: gray `NO SIGNS`, amber `POSSIBLE`, red `CONFIRMED`.
- The status word in big type, a line of free text (for example "2 people · can move"), a live mic level bar updated at 10 Hz or slower, a streaming indicator, and battery voltage in small type.
- Button A toggles the backlight to save battery.

**README** must cover:
1. Toolchain install: the ESP32 board-manager URL `https://espressif.github.io/arduino-esp32/package_esp32_index.json`, `arduino-cli core install esp32:esp32`, `arduino-cli lib install M5Unified`.
2. Compile and upload commands.
3. **Mounting:** find the mic pinhole; press that face onto the slab; seal a ring of Blu-Tack around the hole; cover the stick with foam or a folded cloth.
4. A test checklist.

## Host side (`heartbeat/stickmic.py`, `heartbeat/sources.py`)
**`StickMicSource(port, baud, gain_db, bus)`** implements `AudioSource`.
- **Opening the port:**
  - Create the `serial.Serial()` object without a port, set `dtr=False` and `rts=False`, then `open()` with `exclusive=True`. This avoids resetting the ESP32 and stops other code from grabbing the port.
  - Wait up to 3 s for `READY`. If it doesn't arrive, send `PING`.
  - Then send `START`.
- **Reading frames:**
  - Resync on the magic bytes, and verify the CRC.
  - Count bad frames.
  - **Fill missing seq numbers with zeros**, so stream time stays sample-exact, and publish a warning event with the counts. Handle seq wraparound.
- **Output:** convert to float32 (÷32768) and apply `gain_db`. Yield 256-sample `AudioBlock`s with `t0` taken from the sample count.
- **`close()`** sends `STOP` and closes the port.
- **`send_status(status, text)`** is thread-safe (a write lock).

**`StickStatusSink(source)`** is a bus subscriber.
- On `state`, `round_result` and `answer` events, it sends `STATUS <status> <short text>`. Keep the text 40 characters or less, for example `CONFIRMED 1 in 2.3M · 2 people`.
- It must only enqueue, with a small writer thread doing the writes, so the pipeline never blocks.

**`find_stick_ports()`** scans serial ports, sending `PING` at 921600 and expecting `PONG stickmic v1`.
- Skip any port that is already open.
- Keep the scan fast (≤ 0.5 s per port).

**`open_input(spec, cfg, bus)`:**

| `spec` | Opens |
|---|---|
| None, an int, or a device name | `MicSource` (unchanged behavior) |
| `stick` | auto-find with `find_stick_ports()`; a clear error if none is found |
| `stick:/dev/cu.xxx` | `StickMicSource` on that port |

The existing `--wav` option stays as it is.

**Knocker `auto` scan** (in `prompter.py`)
- Accept an `exclude_ports` set, and pass the stick's port from `hb run`.
- Never open a port that is in use.
- This prevents the scan from resetting or hijacking the stick.

## Tests (no hardware)
Build a fake serial byte stream and check that:
- clean frames give exact samples and exact stream time
- a seq gap inserts the right number of zero samples and a warning
- a bad CRC frame is dropped and filled with zeros
- garbage bytes between frames cause a resync
- partial reads split across frame boundaries work
- seq wraparound at 65535 works

Also test:
- `open_input` parsing for every case
- `StickStatusSink` formats ≤ 40 characters and never blocks the publisher
- the knocker auto-scan skips `exclude_ports` and rejects `PONG stickmic v1`

## Done when
- The tests pass, and the full suite still passes.
- The firmware compiles with arduino-cli if the ESP32 core and M5Unified are installed. If they aren't, say so; **don't install toolchains without asking.**
- Commit only your files: `P07: M5StickC Plus SE mic source + firmware`.
- Your report includes the exact human test sequence:
  ```bash
  uv run hb devices
  uv run hb listen --device stick
  uv run hb record --device stick --label knock --surface table --desc test --seconds 15
  uv run hb run --device stick --knocker <knocker port>
  ```
