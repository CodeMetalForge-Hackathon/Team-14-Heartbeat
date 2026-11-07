# P04: black box log and live dashboard

Read `AGENTS.md` first. **Lane A, after P03 is committed.**

## Goal
Two pieces:
- **The black box** records everything the sentinel heard and decided, in a log that shows when it has been tampered with, and can replay a session.
- **The dashboard**, a live command-post view that works on a phone with no internet.

## Files you own
- `heartbeat/blackbox.py`, `heartbeat/server.py`
- `dashboard/`
- Wiring-only edits to `heartbeat/cli.py`, `heartbeat/runner.py` and the `server` section of `profiles/rubble.yaml`
- `tests/test_blackbox.py`, `tests/test_server.py`
- Add `fastapi`, `uvicorn` and `httpx` (dev) to `pyproject.toml`.

## `blackbox.py`
- **`BlackBox(run_dir, profile, argv)`** subscribes to the `EventBus`. It writes `runs/<YYYYmmdd-HHMMSS>/` as defined in AGENTS.md.
- **`events.jsonl`**
  - One hash-chained record per event, flushed on every line.
  - `level` events are saved at most once per second.
- **Clips:** for every `sound` event, write the `_clip` array to `clips/<seq>.wav` (16-bit). Add `clip` (the relative path) and `clip_sha256` to `data` before hashing.
- **`raw.wav`**
  - A continuous 16-bit recording of every audio block, written by a writer thread through a queue.
  - Flushed every second, so it survives Ctrl-C and crashes. Use `soundfile` in a mode that keeps a valid header.
- **`meta.json`:** profile, CLI args, git commit (if available), hostname, platform, Python and package versions, start time.
- **`hb verify RUN_DIR`:** recomputes the chain and every clip hash.
  - Prints `OK <n> events, <m> clips` or `TAMPERED at seq <k>: <reason>`.
  - Exits with code 1 on failure.
- **`hb replay RUN_DIR|WAV [--profile P] [--classifier NAME]`:** re-runs detection and classification offline on `raw.wav`. Prints matched, missing and new sounds versus the original log, within ±30 ms. Use it to try a new classifier on old sessions.
- **`hb report RUN_DIR`:** writes `RUN_DIR/report.md` with these sections:
  - duration
  - sounds and knocks
  - background rate
  - each round (asked, heard, p)
  - final odds, answers, approvals
  - errors

  This is our after-action review.

## `server.py`
- **Start it with** `hb run --dashboard` and `hb sim --dashboard --realtime`. Options: `--host 0.0.0.0`, `--port 8000`.
- **uvicorn** runs on the main thread; the pipeline stays on its own threads.
- **Endpoints:**
  - `GET /` serves the dashboard.
  - `GET /api/state` returns `engine.snapshot()` plus `run_id`, `profile`, `sentinel_name` and `uptime`.
  - `GET /api/events?since=<seq>&limit=200`.
  - `POST /api/command {name, args}` is routed to the engine on the processing thread through its queue. It returns the result; an invalid command gets 400 with the reason.
  - `WS /ws` pushes every event as JSON, including `level` at 10 Hz.
- **Bus to asyncio:** bridge with `loop.call_soon_threadsafe`. Give each client a bounded queue. A slow client loses messages; **the pipeline never blocks.**

## `dashboard/index.html`
- Vanilla JS, canvas and CSS. **No CDN, fonts or internet**, since the field has none.
- Dark theme by default with high contrast and big text. Works on a 375 px-wide phone.
- **Header:** sentinel name, profile, run id and a connection dot. It reconnects automatically: fetch `/api/state`, then stream.
- **Status card:**
  - `NO SIGNS` in gray, `POSSIBLE` in amber, `CONFIRMED` in red with a pulse.
  - The line "Chance this is random: **1 in 2.3 million**".
  - The current state, for example "Round 2: listening…".
  - An "Approved by XX" badge.
- **Live trace:** the last 20 s of level (dB) with the noise-floor line.
  - Markers: knock (tall bright tick), rejected sound (short gray tick; tap or hover to see the score and top label), our prompts (blue shaded), response windows (outlined).
- **Rounds table:** round, we knocked, heard, p, matched.
- **Answers:**
  - People: 2. Can move: yes. Hurt: unclear.
  - An unclear answer shows as unclear, never guessed.
- **Buttons:** Challenge, Interview, Reassure, Reset, **Approve dig**.
  - Approve dig is enabled only when `CONFIRMED`. It asks for operator initials, then sends `approve`.
- **Event log:** the last 50 events, human-readable.
- **Footer:** "Black box: N events, hash-chained."

## Tests
- **Black box:**
  - A run made from `hb sim` verifies `OK`.
  - Editing one JSON line, or one byte of one clip, is caught at the right seq.
  - `level` throttling works.
  - `raw.wav` duration matches the stream time within 1 s.
- **Server** (FastAPI `TestClient`):
  - `/api/state` has the right shape.
  - A command is routed to a fake engine.
  - An invalid command returns 400.
  - A WebSocket client receives a published event.

## Done when
- Tests pass.
- `uv run hb sim --people 2 --dashboard --realtime` lets a human watch the full rescue dialog at http://localhost:8000.
- `uv run hb verify runs/<latest>` prints OK.

Commit `P04: black box + dashboard`. Report, and say what a human should look at in the browser.

## Notes from the P01 review (read before starting)
- **`EventBus.publish` calls subscribers synchronously, on the publishing thread, under a lock.** A slow subscriber stalls the audio pipeline. The `BlackBox` subscriber must only enqueue: one writer thread does all file I/O, covering both the JSONL and the clip WAVs. The WebSocket bridge must only enqueue as well.
- **Clips.** The clip array is on `event.clip`, taken from `_clip`. `event.data["clip"]` is `None` until you fill in the path.
- **Operator commands** go to the engine through `Pipeline.call_soon` (added in P03).
