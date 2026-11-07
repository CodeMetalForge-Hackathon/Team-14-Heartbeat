# Heartbeat stick microphone

For **M5StickC Plus SE, ESP32-PICO-D4, 135×240 LCD, SPM1423 PDM microphone, no IMU**. It sends 16 kHz mono signed 16-bit PCM over the USB-to-serial bridge at **500000 baud**. This is a serial source, not a USB audio device. The built-in speaker and IMU initialization are disabled.

The SE's CH552 bridge supports 500000 but not the original 921600 setting; unsupported rates can produce zeros or garbled UART data. Host `audio.stick_baud` and firmware `SERIAL_BAUD` must both be 500000. Reflash older 921600 firmware before using the updated host. See [M5Stack's supported baud rates](https://docs.m5stack.com/en/core/StickC-Plus_SE).

## Install, compile, upload

Run these commands from the repository root. FIX-02 includes an explicitly authorized real-stick diagnostic; its build, upload, and hardware results are recorded in [reports/FIX-02.md](../../reports/FIX-02.md).

The ESP32 board-manager URL is <https://espressif.github.io/arduino-esp32/package_esp32_index.json>. The usual latest-version commands are `arduino-cli core install esp32:esp32` and `arduino-cli lib install M5Unified`; for this sketch use the following reproducible **core 3.x / M5Unified 0.3.0 / M5GFX 0.3.0** combination. Version 0.3.0 supplies the microphone buffer-release callback and continuous-buffer handling used here; older M5Unified releases are unsupported.

```bash
arduino-cli config add board_manager.additional_urls https://espressif.github.io/arduino-esp32/package_esp32_index.json
arduino-cli core update-index
arduino-cli core install esp32:esp32@3.3.12
arduino-cli lib update-index
arduino-cli lib install M5GFX@0.3.0
arduino-cli lib install M5Unified@0.3.0
arduino-cli board listall | grep -i stick
arduino-cli compile --fqbn esp32:esp32:m5stack_stickc_plus:DebugLevel=none firmware/stickmic

# Replace this with the stick's USB serial port, never the knocker's port.
STICK_PORT=/dev/cu.usbserial-REPLACE_ME
# On Raspberry Pi this will commonly be /dev/ttyUSB0.
arduino-cli upload --fqbn esp32:esp32:m5stack_stickc_plus:DebugLevel=none --upload-property upload.speed=115200 --port "$STICK_PORT" firmware/stickmic
arduino-cli monitor --port "$STICK_PORT" --config baudrate=500000
```

The board candidate `esp32:esp32:m5stack_stickc_plus` is confirmed in [Espressif's board definitions](https://github.com/espressif/arduino-esp32/blob/3.3.12/boards.txt). Confirm it with `board listall` after installation. The sketch fixes the display/board selection to StickC Plus and sets `internal_imu=false`, avoiding a dependency on an IMU being present on the SE.

Close the serial monitor before using `hb`; exactly one process should own the port. Use 115200 for the bootloader upload; its switch to 500000 failed on the tested SE. This upload rate is separate from the firmware's 500000-baud audio connection. The host configures DTR/RTS low before opening, but the tested macOS bridge still restarted the stick on open. ROM boot text at 115200 therefore appears garbled at 500000 and can run directly into `READY`. The host allows up to three seconds for startup and resends newline/STOP/PING after that banner to discard any command received at the old boot baud.

## Protocol

ASCII uppercase command lines end in LF; CRLF is accepted. Only printable ASCII and spaces are accepted, with at most 127 bytes before the newline. Overlong or malformed lines are discarded in full. Free text is limited to 40 ASCII bytes on the LCD; replace Unicode punctuation with ASCII on the host.

| Text-mode command | Result |
| --- | --- |
| Boot | `READY stickmic v1 rate=16000` |
| `PING` | `PONG stickmic v1` |
| `START` | Start capture; first response is a binary frame, **no textual ACK**. |
| `STOP` | Stop capture and discard pending frames; **no ACK**. Safe when already stopped. |
| `STATUS NO_SIGNS <text>` | Gray `NO SIGNS` screen; no ACK. |
| `STATUS POSSIBLE <text>` | Amber `POSSIBLE` screen; no ACK. |
| `STATUS CONFIRMED <text>` | Red `CONFIRMED` screen; no ACK. |
| `MAG <n>` | Persist integer `1..255`, reply `OK MAG <n>`; default 16. |
| Anything else, including knocker `K`, `CAL`, `TEST`, or `MODE PIEZO` | `ERR command` |

`MAG` changes M5Unified's integer microphone multiplier, not decibels. Use it in text mode, then `START`; high values can clip. Persistence uses the ESP32 core's Preferences/NVS namespace `hb-stickmic`, key `mag`. Invalid values return `ERR mag_range_1_255`; failed persistence returns `ERR mag_save` without changing the multiplier. The LCD footer displays the current value. It survives reboot and normal upload unless flash/NVS is erased.

The firmware boots **idle**, displays `Awaiting host`, and streams only after `START`. While streaming, only `STOP` and valid `STATUS` commands take effect. **No textual responses are emitted while streaming**, including for malformed/unknown commands, `PING`, repeated `START`, or `MAG`. A `STOP` is processed between complete frames; queued UART bytes can still arrive afterward. To recover from a previous host crash without resetting the stick, send `\nSTOP\n` (the leading newline terminates any partial old command), drain and flush pending input, send `PING\n`, and only send `START\n` after receiving `PONG stickmic v1`. The host performs this handshake without depending on the one-time `READY` line.

Use `uv run hb listen --device stick:/dev/cu.<port> --debug` to print the first 200 raw received bytes, including bytes drained during the handshake. Short or failed sessions print all available bytes up to that limit. Serial timeout is configured before opening and kept fixed while reading, avoiding repeated custom-baud reconfiguration on macOS.

Each frame is exactly 520 bytes:

| Offset | Size | Field |
| --- | --- | --- |
| 0 | 2 | Magic bytes `A5 5A` |
| 2 | 2 | Sequence, uint16 little-endian |
| 4 | 2 | Sample count, uint16 little-endian, always 256 |
| 6 | 512 | 256 signed int16 little-endian PCM samples |
| 518 | 2 | CRC, uint16 little-endian |

CRC is **CRC-16/CCITT-FALSE**: polynomial `0x1021`, initial value `0xFFFF`, no input/output reflection, final XOR `0x0000`. It covers only bytes 2 through 517 (sequence + count + PCM), excluding magic and CRC. Check vector: ASCII `123456789` → `0x29B1`. Sequence starts at 0 on every successful `START` and wraps `65535 → 0`. A frame represents 16 ms, giving 32,500 payload bytes/s including framing (325,000 UART bits/s with 8N1).

## Capture and screen

Two 256-sample buffers are queued to `M5.Mic`. Its completion callback copies PCM into a bounded eight-frame transmit queue, then immediately requeues the released buffer while the other is recording. The callback never writes the UART or LCD, saves flash, or waits for transmit-queue space. If the queue fills, a frame is dropped and its sequence is still consumed, allowing the host to insert exactly 256 zero samples for the gap. `TX dropped` is shown on the LCD.

The mic task runs on core 0 at priority 4. A separate core-1 UI task updates the level/status screen at most 10 Hz and polls battery voltage once per second. The bar retains the largest sample peak between updates. Button A toggles the backlight; button B is unused. Battery readings come from `M5.Power.getBatteryVoltage()` in millivolts; an unavailable reading displays `--.--V`. Display work does not gate audio capture. `MIC ERROR: STOP then START` indicates a capture failure; the stream stops without injecting text, and the host should time out and report an error. Send `STOP`, inspect/reconnect, then retry `START`.

Hardware and wire constants, queue capacity, and screen cadence are named at the top of the sketch. Host gain and detection settings remain in `profiles/*.yaml`; the MCU does not read host YAML.

## Mounting

Find the **microphone pinhole** on your stick; verify it against the device enclosure rather than assuming every hole is a microphone. Press that face onto the slab. Seal a ring of Blu-Tack around the pinhole, leaving the hole itself open to the tiny sealed cavity. Secure the stick, relieve USB cable strain, then cover it with foam or a folded cloth to reduce room sound. Compare taps through the slab against room speech before the demo.

## Human test checklist

1. Compile and upload with the commands above. At 500000 baud confirm `READY`, `PING` → `PONG stickmic v1`, and `K 1 200` / `CAL 90 120 40` → `ERR command`.
2. Send `MAG 16`, confirm `OK MAG 16`, reboot, and check `MAG 16` on the LCD. Check `MAG 0`, `MAG 256`, and nonnumeric values are rejected. Send all three `STATUS` words with short text and check the colors, two-line text, battery voltage, and button-A backlight toggle.
3. Close the monitor. Run the sequence below; confirm slab knocks appear in the host, samples are 16 kHz, and the screen changes with the dialog. Inspect a recording for clipping and compare mounted/unmounted room-noise pickup.
4. Leave capture running for several minutes while repeatedly updating status and toggling the backlight. Check CRC/gap warnings and the LCD's `TX dropped` count. Kill/reopen the host once to check `STOP`/`PING` recovery. After a normal close, confirm `PING` works again.

```bash
uv run hb devices
uv run hb listen --device stick
uv run hb record --device stick --label knock --surface table --desc test --seconds 15
uv run hb run --device stick --knocker <knocker port>
```

FIX-02 was compiled, flashed, and tested on the attached SE with explicit user authorization. Idle PONG, live PCM, repeated open/close, already-streaming STOP/PING, discovery, and `hb listen --debug` worked; see [the measured results](../../reports/FIX-02.md). Some captures showed recoverable CRC/gap warnings, so long-duration UART reliability remains unverified. Pi hardware, calibrated sample cadence, battery accuracy, mounting performance, button/backlight behavior, and physical knock sensitivity still need human checks.

## Optional piezo path: UNTESTED, not implemented

This release implements the built-in PDM mic only. `MODE PIEZO` and `MODE PDM` are unsupported; in text mode they return `ERR command`. No ADC or GPIO33 configuration is performed. A future GPIO33/ADC1 path must use core-3.x continuous ADC at 16 kHz, remove DC, scale to int16, and keep the same wire protocol.

The supplied experimental circuit was: two 100 kΩ resistors from 3V3 to GND making a 1.65 V midpoint; midpoint → 10 kΩ series resistor → GPIO33; piezo between midpoint and GND; 1 MΩ across the piezo. **This circuit is untested.** It loads the stated midpoint with the piezo/shunt and specifies no input clamps; its bias and voltage protection need bench validation before connection. It is not a validated wiring recommendation. Keep the primary PDM path for the demo.

## API references

- [M5Unified 0.3.0 release](https://github.com/m5stack/M5Unified/releases/tag/0.3.0) and [M5GFX 0.3.0 release](https://github.com/m5stack/M5GFX/releases/tag/0.3.0).
- [Mic API](https://github.com/m5stack/M5Unified/blob/0.3.0/src/utility/Mic_Class.hpp): `record(int16_t*, array_len)`, two queued requests, buffer-release ownership and callback requeue rules.
- [Mic implementation](https://github.com/m5stack/M5Unified/blob/0.3.0/src/utility/Mic_Class.inl) and [M5Unified board/mic configuration](https://github.com/m5stack/M5Unified/blob/0.3.0/src/M5Unified.inl): StickC Plus PDM data GPIO34/clock GPIO0 and capture-task behavior.
- [Power API](https://github.com/m5stack/M5Unified/blob/0.3.0/src/utility/Power_Class.hpp) and [M5GFX display detection API](https://github.com/m5stack/M5GFX/blob/0.3.0/src/M5GFX.h).
