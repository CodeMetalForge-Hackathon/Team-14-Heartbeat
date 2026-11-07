# FIX-04: rover connect handshake

Read `AGENTS.md` first.

**Files you own:** `heartbeat/rover.py`, `tests/test_rover.py`, and the `rover` section of `profiles/rubble.yaml`.

## Bug (verified on real hardware)
`hb rover --port /dev/cu.usbserial-2120 ping` fails with "Rover timed out waiting for PING". The firmware itself is fine:
- A plain pyserial open of the same port prints `READY rover v1` after about 1.5 s.
- `PING` then gets `PONG rover v1`.

**Cause:** opening the port resets the Me Auriga (ATmega2560 auto-reset), so the board spends about 1.5–2 s in its bootloader. `RoverLink._connect()` sends a single `PING` immediately and waits only `connect_timeout_s` (2.0). That PING is lost during boot.

## Fix
In `_connect()`:
1. After opening, wait for `READY rover v1` for up to `boot_timeout_s` (default 4.0, in the profile).
2. If `READY` never arrives, for example because the board didn't reset, **resend `PING` every 0.5 s** until `PONG rover v1` arrives or `boot_timeout_s` expires.
3. Then continue as before.

The same applies to `hb rover calibrate` and `hb patrol`, since they share `_connect`.

## Tests
With a fake serial port:
- a board that boots silently for 1.6 s, then prints `READY`
- a board that never prints `READY` but answers the second `PING`
- a board that never answers: a clear timeout error, and the port is released

The full suite must pass.

**Commit:** `FIX-04: rover boot handshake`.
