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

## Antenna-connected indoor recheck, 2026-11-08

The operator connected a u-blox ANN-MB5-00-00 through an SMA-to-u.FL adapter.
The operator confirmed the antenna was indoors away from a window and could
not move it to an open-sky position during this check.

- The first 60.37-second sample contained 722 valid sentences and zero invalid
  sentences. GPS satellites reported in view rose from two to a maximum of
  five. None were used in a position fix: quality 0, RMC V, HDOP 99.99.
- An additional 60.29-second observation contained 723 valid sentences and
  zero invalid sentences. GSV messages intermittently contained nonzero C/N0
  measurements from GPS satellites, with sampled strongest signals around
  18-26dB-Hz. At the end five GPS satellites had recent nonzero C/N0 reports.
  The receiver still had no fix and zero satellites used for positioning.

This establishes receiver communication and detectable satellite RF signals
with the connected antenna. It does not establish reliable antenna power,
adequate signal strength/geometry, position accuracy or an operational fix.
Indoor attenuation is a plausible limitation; an outdoor check is still needed.

The [ANN-MB5 datasheet](https://content.u-blox.com/sites/default/files/documents/ANN-MB5_Datasheet_UBX-22038811.pdf)
specifies an active L1/L5 antenna with a 3.0-5.0V supply requirement. The carrier
must provide suitable bias power through the RF feed; a mechanical adapter
alone does not supply power. The NEO-M9N can use the antenna's L1 coverage;
attaching a multiband antenna does not add L5 reception to the receiver.
No antenna supply voltage was measured during this test.

When placement is possible, put the antenna stationary with its top facing
open sky and repeat the existing diagnostic. If acquisition still fails there,
verify the exact carrier's antenna bias circuit and the SMA/u.FL connections.
Both software readers exited and released the serial port after these tests.

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


## GPS on the patrol dashboard

Start the normal patrol with `--gps-port auto` to select exactly one USB u-blox
receiver, or use `--gps-port /actual/gps/device` for an explicit port. Connect it
to the same Mac/Pi running patrol, separately from the rover and microphone.
Close the standalone GPS checker first: only one process may own the GPS port.
No firmware update is required. Without this option, GPS remains disabled.

The dashboard includes a local, north-up coordinate map with a metre grid, a
blue current-rover marker, and numbered green confirmation markers. It uses no
remote map service and sends no location data externally. There are no street
or building tiles. The local projection is an overview, not survey-grade or
long-distance navigation. GPS settings are under `rover.gps` in rubble.yaml.

Each deterministic CONFIRMED gap result stores a GPS snapshot in `location`
in events.jsonl. The result survives later travel and remains in dashboard
history during the session. It means **rover position at confirmation**, not
the person's exact location: one microphone does not give range or bearing.
The map does not steer the rover. A fresh positive GGA fix and noncontradictory
recent RMC status are required. No fix, disconnected receiver, or a fix older
than three seconds yields location unavailable; findings are never backfilled
with a later coordinate. HDOP is dimensionless, not a metre accuracy estimate.
Coordinates are now intentionally stored locally in the session's black box.

Example (from the repository directory, using the existing forward route):

```bash
uv run hb patrol --rover /dev/cu.usbserial-1130 --device stick \
  --profile clap-knocks --route /private/tmp/heartbeat-forward-longer.yaml \
  --knocker speaker --gps-port auto --dashboard --port 8001
```

An absent/ambiguous GPS at startup causes startup to fail before patrol starts;
a receiver disconnect after startup makes location unavailable. It does not
silently substitute a guessed position. On Raspberry Pi use the actual rover
and GPS `/dev/serial/by-id/` paths and a route file present on that Pi.


### Temporary two-knock mode

`clap-knocks` now sets `rover.audio_stop.verification_mode: two_knock`.
The first knock candidate stops travel; one fresh accepted knock after settling
records `reason: KNOCK_VERIFIED` and the current rover GPS snapshot, then resumes.
There is no changing-pattern challenge or interview in this mode. Person status
remains POSSIBLE, random-match odds are not assessed, and dig approval remains
unavailable. Missing a second knock still holds in REVIEW. Map entries are
labeled Knock verified; missing GPS is never replaced by a later fix.
Set verification_mode back to `challenge` to restore person confirmation.
