# FIX-02: stick port claim + handshake

Read AGENTS.md. Bug in P07 (`heartbeat/stickmic.py`, `sources.py`, `cli.py`):
`uv run hb listen --device stick:/dev/cu.usbserial-81520161EE` fails with
“Serial port is already in use”, but no other process holds the port (`lsof`
is empty, and a plain pyserial exclusive open works). `claim_port()` is being
called twice in the same process for the same port: find where the first claim
happens (open_input, probing, the knocker auto-scan, or a retry path) and fix it
so one source claims the port exactly once and releases it on close. Add a
regression test.

Also: after the flash, sending `PING\n` at 921600 with `dtr=False/rts=False`
got only `0x00` bytes back, no `READY` and no `PONG stickmic v1`. Check whether
the firmware boots straight into streaming, and whether the host handles a
stick that is already streaming (send `STOP`, flush, then `PING`). Add a
`--debug` option that prints the raw first 200 bytes.

Test with the real stick yourself: you may open that serial port for this fix.
Commit as `FIX-02: stick port claim + handshake`.

User clarification during diagnosis: the stick display says **Awaiting host**.

Additional user instruction: update `heartbeat/doctor.py`, whose
`_BAUDS = (921600, 115200)` still probes the stick at 921600. Use
`cfg.audio.stick_baud` (500000) for the stick probe, keep 115200 for the
knocker/rover, and add a test.
