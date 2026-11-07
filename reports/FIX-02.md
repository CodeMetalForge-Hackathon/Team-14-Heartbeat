# FIX-02: stick port claim + handshake

## Findings and changes

- The instrumented failing `listen --device stick:<port>` path made its first and only claim in `StickMicSource._open`, called by iteration in the pipeline worker. `open_input` did not probe or claim an explicit port, and `listen` did not run a knocker scan. The reservation set was empty when the claim failed. The actual false-busy result came from treating a 0.1-second `lsof` timeout as proof of ownership; an empty real `lsof` took 0.184 seconds. An ownership check now requires evidence of an owner, and the exclusive serial open still enforces OS ownership.
- There were also real duplicate-claim paths: pre-opening a source and then iterating it, and retrying a source whose existing connection had closed. Successful open is now idempotent. A disconnected source terminates and releases its reservation; a fresh source can reopen. Failed direct opens and repeated closes release resources exactly once.
- Firmware boots idle (`protocolStreaming=false`, `captureEnabled=false`) and displays `Awaiting host`. It starts PCM only after `START`. While streaming it ignores PING. Host and discovery now send newline/STOP, flush/drain pending bytes, reset the input buffer, and require PONG before START.
- This SE restarted on fresh serial open despite preconfigured low DTR/RTS. ROM output at 115200 became garbled at the application baud, sometimes sharing a line with READY. Commands sent during boot could also leave partial input. The host recognizes that READY, resends newline/STOP/PING, and still requires a valid PONG. Discovery uses the same bounded three-second startup allowance as the source.
- Serial timeout stays fixed after open; reads no longer repeatedly reconfigure the UART. `hb listen --debug` prints exactly the first 200 received raw bytes (or fewer on an early close/failure), including handshake drain bytes, once to stderr.
- `doctor` uses the profile's `audio.stick_baud` for the stick and retains 115200 for the knocker/rover. Its stick probe shares the recovery handshake. The internal `probe_serial_ports(profile, settings)` helper now receives the selected profile; its callers and fakes were updated.

## Baud contract change

The StickC-Plus SE's documented CH552 bridge rates include **500000**, but not **921600**. Firmware `SERIAL_BAUD`, the Python default, and `profiles/rubble.yaml` now agree on 500000. Older firmware must be reflashed. PCM framing, CRC, 16 kHz mono float32 audio, sample-count timestamps, and event contracts are unchanged; no dependencies were added.

References: [M5Stack SE baud-rate documentation](https://docs.m5stack.com/en/core/StickC-Plus_SE), [M5Stack CH55x FTDI-compatible bridge implementation](https://github.com/m5stack/M5_CH55x/blob/master/ftdi.c).

## Verification

Hardware-free regressions cover lazy explicit source selection, one claim/open/START across pre-open plus iteration, terminal cleanup after disconnect/open failure, slow `lsof`, positive owners, abandoned PCM and partial commands, delayed/binary-prefixed READY, mandatory PONG, fixed read timeout, bounded raw previews, CLI plumbing, and configured doctor baud selection.

`UV_CACHE_DIR=/tmp/heartbeat-uv-cache uv run pytest -q`: **637 passed, 1 warning, 30.62 seconds**. The warning is Starlette's existing httpx TestClient deprecation. Focused suites passed 83 stick tests and 75 doctor tests. `git diff --check` passed.

Firmware compiled using ESP32 core 3.3.12, M5Unified 0.3.0, M5GFX 0.3.0 and `esp32:esp32:m5stack_stickc_plus:DebugLevel=none`: 506283 bytes of program storage and 28192 bytes of globals. Upload at 500000 failed before writing; retry at **115200 upload baud** succeeded with flash hashes verified. Application transport remains **500000**.

Explicitly authorized real-stick checks used only `/dev/cu.usbserial-81520161EE`:

| Check | Result |
| --- | --- |
| Idle identification | Valid `PONG stickmic v1` |
| Pre-open twice, then iterate, then repeated close | 250 blocks / 4 seconds; one claim and release; no CRC/gap warnings |
| Fresh source after close | 250 blocks / 4 seconds; one claim and release; no CRC/gap warnings |
| STOP/flush/PING during live PCM, without closing/resetting | Valid PONG |
| Abandoned stream followed by fresh source | 250 delivered blocks / 4 seconds; two CRC warnings, three missing blocks replaced with silence; one claim/release |
| Targeted discovery | Correctly identified the stick at 500000 |
| Doctor serial probe, restricted to the authorized port | Used profile baud 500000, identified the stick, no errors, released its claim |
| Exact `hb listen --device stick:<port> --debug` command | Raw 200-byte preview, live audio meter, CPU classifier; stopped with SIGINT, exit 0 |
| Port after CLI exit | Empty `lsof`; no remaining owner |

Two earlier four-second captures also had zero CRC/gap warnings; another had one recovered CRC error and one inserted silence block. These are short functional checks, not a claim of lossless transport under all conditions.

## Commands to try

The attached stick has already been flashed. From the repository root:

```sh
uv run hb listen --device stick:/dev/cu.usbserial-81520161EE --debug
uv run hb doctor --device stick:/dev/cu.usbserial-81520161EE
uv run pytest -q
```

For another SE or older firmware:

```sh
arduino-cli compile --fqbn esp32:esp32:m5stack_stickc_plus:DebugLevel=none firmware/stickmic
arduino-cli upload --fqbn esp32:esp32:m5stack_stickc_plus:DebugLevel=none --upload-property upload.speed=115200 --port /dev/cu.usbserial-REPLACE_ME firmware/stickmic
```

Long-duration UART reliability, Raspberry Pi hardware, calibrated sample cadence, physical knock sensitivity/mounting, battery accuracy, and button/backlight behavior remain unverified. Some real captures produced recoverable transport warnings. Discovery can take up to three seconds per unresponsive candidate to accommodate bridge-triggered boot.
