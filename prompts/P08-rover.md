# P08: rover bridge and stop–listen–move patrol (mBot Ranger)

Read `AGENTS.md` first. **Lane B.** P06 may be running in lane A and editing `cli.py`, so **re-read shared files right before every patch** and keep shared edits small.

## Why
The sentinel works standing still. The rover carries it to gaps people can't reach:
1. Drive to a gap.
2. **Stop the motors.**
3. Let the debris settle.
4. Run the knock dialog.
5. Report the result for that gap.
6. Move on.

The motors are **never** on while we listen: motor noise drowns out knocks.

Hardware: a Makeblock **mBot Ranger** in the tracked "Land Raider" form, built on a **Me Auriga** board (ATmega2560) with two onboard encoder motors and a 12-LED RGB ring. It connects over USB serial to the laptop or Pi. On the rover it carries:
- the M5StickC Plus SE mic (P07)
- the Seeeduino servo knocker (P02)
- possibly the Pi and a power bank

## Files you own
- New: `firmware/rover/rover.ino`, `firmware/rover/README.md`
- New: `heartbeat/rover.py`, `heartbeat/patrol.py`
- New: `profiles/route.yaml`
- New: `tests/test_rover.py`, `tests/test_patrol.py`
- Minimal edits:
  - `heartbeat/cli.py`: add the `hb rover` and `hb patrol` commands
  - `dashboard/`: add a patrol panel and an E-STOP button
  - `heartbeat/server.py`: one endpoint for rover commands
  - a `rover` section in `profiles/rubble.yaml`. If `config.py` rejects the new section, add it as a free-form dict section, like `dialog`.

## Phase 1: firmware and link (commit `P08a`)
**Library**
- Use the Makeblock library (`MeAuriga.h`) from `https://github.com/Makeblock-official/Makeblock-Libraries`.
- The README must document installing it with arduino-cli:
  ```bash
  arduino-cli config set library.enable_unsafe_install true
  arduino-cli lib install --git-url https://github.com/Makeblock-official/Makeblock-Libraries.git
  ```
  If the repo layout needs a zip install instead, document that.
- FQBN: `arduino:avr:mega:cpu=atmega2560`.
- Use the onboard encoder motors (`MeEncoderOnBoard` on SLOT1 and SLOT2, with their interrupt setup), following the official Auriga examples.

**Serial protocol** (115200 baud, ASCII lines)

| Command | Reply |
|---|---|
| boot | `READY rover v1` |
| `PING` | `PONG rover v1` |
| `DRIVE <cm> <speed_pct>` | `OK DRIVE <cm>` after an encoder-measured distance. Negative cm drives backward. |
| `TURN <deg> <speed_pct>` | `OK TURN <deg>`, turning in place |
| `STOP` | `OK STOP`, **immediately**, interrupting any move |
| `HB` | heartbeat, no reply |
| `STATUS` | `STATUS batt=<V> moving=<0/1> enc=<l>,<r>` |
| `LED <r> <g> <b>` | sets the ring color |
| `CAL <deg_per_cm> <deg_per_turn_deg>` | saves the calibration to EEPROM |

**Safety**
- Limits, enforced in firmware **and** on the host: `|cm| ≤ 100`, `|deg| ≤ 180`, `speed_pct ≤ 60`.
- Ramp the speed up and down.
- **Watchdog:** while moving, if no `HB` or other command arrives for 1.5 s, stop and send `ERR watchdog`.
- Any invalid input gets `ERR <reason>` and nothing moves.

**Host: `heartbeat/rover.py`, `RoverLink(port)`**
- Open the port with `dtr`/`rts` handling like P07.
- A heartbeat thread sends `HB` every 0.5 s while a move is active.
- Methods: `drive`, `turn`, `stop`, `status`, `led`. Each blocks until `OK` or a timeout, and a timeout sends `STOP`.
- **A safety gate validates every command.** It refuses to move while the sentinel is listening; the patrol passes a callable such as `engine_is_listening()`.
- Publish `rover` events for every command, reply and error.

**CLI**
```bash
hb rover --port P ping
hb rover --port P drive 20
hb rover --port P turn 90
hb rover --port P stop
hb rover --port P status
hb rover --port P led 255 0 0
hb rover --port P calibrate
```
`calibrate` is interactive:
1. Drive 50 cm by encoder.
2. The human measures the real distance.
3. Compute `deg_per_cm` and save it with `CAL`. Do the same for turning.

## Phase 2: patrol (commit `P08b`)
**`profiles/route.yaml`:** a list of gaps, each with a name and moves from the previous stop. For example: `Gap 1: [[drive, 40]]`, then `Gap 2: [[turn, 90], [drive, 30]]`.

**`heartbeat/patrol.py`, `Patrol(rover, pipeline, engine, prompter, route, cfg)`.** For each gap:
1. Gate detection and execute the moves.
2. `STOP`, then settle for `settle_s` (2.0) with detection still gated.
3. Open the gate, then `engine.command("reset")` followed by `engine.command("challenge")`. `reset` keeps the noise history, per FIX-01.
4. Wait until the dialog finishes: `CONFIRMED` (including the interview, if set), `NO_RESPONSE`, or `TOO_NOISY` exhausted. Time out at `gap_timeout_s` (120).
5. Publish `gap_result` with: gap, status, odds, answers, duration, noisy flag.
6. Set the LED ring: gray for no signs, amber for possible, red for confirmed.
7. Continue to the next gap.

At the end, publish `patrol_done` with a table of all gaps.

**Rules:**
- **Never move while the engine is in `PROMPTING`, `RESPONSE_WINDOW` or `INTERVIEW`.** Assert this in code and in tests.
- The operator can pause, resume or abort; abort sends `STOP`.
- **E-STOP** from the keyboard or the dashboard always sends `STOP` first.

**CLI:**
```bash
hb patrol --rover P --device stick --knocker K --route profiles/route.yaml [--dashboard] [--dry-run]
```
`--dry-run` uses a `FakeRover` and the simulated survivor, so the whole patrol runs without hardware.

## Phase 3: dashboard (commit `P08c`)
- A patrol panel: one row per gap with its status color, odds and answers, plus the rover's state (moving / settling / listening / battery).
- A big red **E-STOP** button, which posts `rover stop`.
- An **"Approve dig"** button per confirmed gap. The `approval` event must include the gap name.

## Tests (no hardware)
- **Firmware protocol** (Python parser): all replies parse correctly.
- **`RoverLink`** with a fake serial port:
  - limits rejected on the host
  - a timeout sends `STOP`
  - heartbeats are sent during a move
  - the gate refuses to move while listening
- **Patrol** with `FakeRover` and the simulated survivor at Gap 2 only:
  - results are `NO_SIGNS` / `CONFIRMED` / `NO_SIGNS`
  - no move command is issued while the engine is listening (assert on the event log)
  - E-STOP mid-move stops immediately
  - abort ends cleanly
- The full suite passes.

## Done when
- The tests pass.
- `uv run hb patrol --dry-run --route profiles/route.yaml` prints a 3-gap result table.
- The firmware compiles if the Makeblock library is installed. If it isn't, say so; **don't install toolchains without asking.**

Commit each phase with only your files. Your report covers:
- flashing and calibration steps for a human
- how to restore the Ranger's original firmware: mBlock's "Update firmware"
