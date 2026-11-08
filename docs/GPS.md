# NEO-M9N USB connection and fix check

Use the GNSS receiver for location measurements only after confirming a current
position fix. This diagnostic does not implement navigation or motor control.

## Test on this Pi, 2026-11-08

- Linux enumerated USB device `1546:01a9`, u-blox GNSS receiver, at `/dev/ttyACM0`.
- A status/identity-only UBX poll reported `MOD=NEO-M9N`, `FWVER=SPG 4.04`,
  `PROTVER=32.01`, software `EXT CORE 4.04 (7f89f7)`, hardware `00190000`.
- A 30.47-second receive-only sample contained 361 checksum-valid NMEA
  sentences (RMC, VTG, GGA, GSA, GSV, GLL) and no invalid sentences.
- It reported GGA quality 0, RMC status V, GSA fix type 1, zero satellites used
  and zero reported visible satellites across GPS/GLONASS/Galileo/BeiDou.
  A NAV-PVT status poll also reported no fix and `gnssFixOK=false`.
- **Result: USB/receiver communication works; usable positioning is not yet
  demonstrated.** Accuracy estimates are not meaningful without a valid fix.
- The operator subsequently confirmed **no antenna is attached**. Satellite
  reception and positioning cannot be validated until an appropriate antenna
  is present. Some carrier boards include their own patch antenna; identify
  the carrier before selecting an external antenna/connector or bias supply.

No receiver configuration or saved settings were changed. Status polls request
information; the saved diagnostic below only reads the stream. Coordinates
and raw location recordings are not included in this report or its output.

The saved checker was also run end to end for 30.24 seconds: 361 valid NMEA
sentences, no invalid sentences, no fix, zero satellites, and exit code `2` as
documented. Its 14 hardware-free parser checks passed as part of the full
826-test suite.

## Repeat the check (human-operated)

Connect the GNSS antenna required by the carrier board and provide a clear view
of the sky. USB enumeration alone does not establish antenna reception. Verify
the carrier's antenna instructions before selecting an active or passive antenna.
Allow acquisition time; an indoor test cannot certify outdoor positioning.

From `~/Team-14`:

```bash
uv run python tools/gps_check.py
```

The default check lasts 30 seconds and automatically selects exactly one USB
receiver with u-blox's vendor ID. It never probes arbitrary serial ports or
sends receiver commands. To observe longer or choose the stable USB path:

```bash
uv run python tools/gps_check.py --seconds 60 \
  --port /dev/serial/by-id/usb-u-blox_AG_-_www.u-blox.com_u-blox_GNSS_receiver-if00
```

Exit codes: `0` = fresh receiver-reported position fix; `2` = valid NMEA data
but no current fix; `1` = connection/data/configuration error. A successful fix
report does not measure absolute accuracy against a known reference position.
The parser requires a recent positive GGA quality, valid position fields and
satellites in use, and rejects a contradictory recent RMC void status. Stale
fixes and estimated/dead-reckoning-only positions do not pass.

Settings are in `profiles/gps-check.yaml`, which is a diagnostic settings file,
not an `hb --profile` audio profile. The baud setting is the module's UART
default; native USB CDC transfers do not use it as their USB bit rate. This
checker expects the receiver's default NMEA output, not UBX-only output.

The older `~/gps_test.py` targets a different PA1010D I2C GPS. Do not use its
I2C address, wiring instructions or driver for this USB NEO-M9N connection.

**Serial-port identity:** this GPS currently occupies `/dev/ttyACM0`. When the
rover or microphone is connected, identify each by `/dev/serial/by-id/` and its
actual device identity. Do not assume `/dev/ttyACM0` is the rover or microphone.

Primary references: [NEO-M9N datasheet](https://content.u-blox.com/sites/default/files/NEO-M9N-00B_DataSheet_UBX-19014285.pdf)
and [integration manual](https://content.u-blox.com/sites/default/files/NEO-M9N_Integrationmanual_UBX-19014286.pdf).

Hardware-free parsing checks:

```bash
uv run pytest -q tests/test_gps_check.py
```
