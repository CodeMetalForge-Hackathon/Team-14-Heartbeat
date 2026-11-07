# Heartbeat: guide for coding agents

Read this whole file before every task. Then do only the task in the prompt you were given.

## Mission
Code Metal Forge hackathon, "Operation Lost and Found" (sponsor: USSOCOM). The release candidate is due Sun Nov 8 at 10:00 and the final deadline is 12:00.

The repo is `CodeMetalForge-Hackathon/Team-14`, on branch `main`. It is private now and becomes public after the event. The license is Apache-2.0; keep the existing `LICENSE` file.

Heartbeat finds people trapped under rubble. Mines, ship hulls and avalanches come later. A **sentinel** is a Pi 5 or laptop with three parts:
- a contact microphone pressed on the debris
- a servo "knocker" that strikes the debris
- a small speaker

The sentinel knocks, asks questions and listens for a human knocking back.

Core claim: **"Noise doesn't answer. People do."**
- An AI classifier separates knocks from other noise.
- A deterministic challenge–response dialog shows that the knocks are a person *answering*, and reports the chance that the match is random.

Judges score four things: mission relevance, a working live demo, deployability (Pi 5 CPU, no GPU, path off the laptop) and novelty. **A reliable demo beats more features.**

## System map
```
contact mic ─► AudioSource (16 kHz mono) ─► OnsetDetector ─► Classifier ─► DialogEngine ◄─► Prompter (servo knocks, voice)
                                                   │                          │
                                                   └──────── EventBus ◄───────┘
                                                               ├─► BlackBox (hash-chained log, clips, raw audio)
                                                               └─► Dashboard (FastAPI + WebSocket)
```
**Later.** Build these only when a prompt asks for them:
- the mBot Ranger rover carrying the sentinel (stop → listen → move)
- a LoRa link to the command post
- mmWave radar to detect breathing
- a small LLM "field brain" on the rover and an orchestrator at the command post. Both *call DialogEngine commands as tools*.

Keep interfaces friendly to these future pieces.

## Repo layout
```
AGENTS.md              this file
prompts/               every prompt given to coding agents (required for submission; never delete)
docs/                  human guides
heartbeat/             python package
  config.py events.py audio.py onset.py classify.py synth.py runner.py cli.py   (P01)
  stats.py dialog.py prompter.py                                               (P03)
  blackbox.py server.py                                                        (P04)
  classifiers/                                                                 (P05)
dashboard/             static web UI, no build step, no CDN (P04)
firmware/knocker/      Arduino/ESP32 servo knocker (P02)
profiles/*.yaml        every tunable number
tools/                 data and model scripts
assets/voice/          pre-generated voice prompt WAVs
models/                small model files; big ones are fetched by tools/fetch_models.py
data/raw/ data/clips/  recordings (git-ignored)
runs/                  black-box sessions (git-ignored)
tests/
```

## Contracts
Other agents build against these in parallel. Keep them stable.

**Audio**
- 16000 Hz, mono, float32 in [-1, 1]. The processing hop is 256 samples (16 ms).
- **Stream time:** time is measured in seconds since session start, computed from the sample count. Logic never uses the wall clock; the wall clock appears only in logs. This keeps WAV replay and tests deterministic.
- `AudioSource` yields `AudioBlock(t0: float, x: np.ndarray)`.

**Detection**
- The `OnsetDetector` emits an `Onset` with these fields:
  - `t`, `peak`, `snr_db`
  - `gated: bool`
  - `clip`: 15600 samples starting 0.1 s before `t`. 15600 is the YAMNet input size.
- `Classifier.classify(onset) -> Detection` adds these fields:
  - `score` in [0, 1]
  - `is_knock` (score ≥ profile threshold)
  - `classifier` (its name)
  - `top`: up to 5 `[label, score]` pairs for the log
- Classifiers register in `classify.make_classifier(cfg)` by name, so new ones never require editing callers.

**Events**
- Everything reports through `EventBus.publish(type, t, **data)`.
- An event is `{"type", "t", "wall", "data"}`. It must be JSON-serializable: no numpy types, no audio arrays.
- Audio goes to files. Clip arrays travel to the black box through a side channel, never inside `data`.
- Event types:
  - `session_start`, `session_end`
  - `level` (10 Hz meter: `dbfs`, `floor_db`)
  - `sound` (one per onset, after classification: `snr_db`, `peak`, `gated`, `score`, `is_knock`, `classifier`, `top`, `clip`)
  - `prompt_start`, `prompt_end`
  - `window_open`, `round_result`, `state`, `answer`
  - `operator`, `approval`, `error`

**Dialog**
- `DialogEngine` is **pure**: no I/O, no threads, no sleep, no wall clock.
- API, where each call returns a `list[Action]`:
  - `on_sound(det)`
  - `tick(t)`
  - `on_prompts_finished(t)`
  - `command(name, t, **args)`
- Actions: `PromptVoice(key)` and `PromptKnocks(n, interval_s)`.
- The engine reports through an injected `emit(type, t, data)` callback.
- Status is one of `NO_SIGNS`, `POSSIBLE`, `CONFIRMED`. **Never use the word "clear":** no response does not mean nobody is there.

**Prompter**
- `Prompter.run(action)` blocks until done and runs on a worker thread.
- Implementations: Manual, Speaker, SerialKnocker, Composite.
- **Gating:** from prompt start until prompt end + `gate_tail_s`, sounds are marked `gated` and never counted as answers, though they are still logged. This keeps us from hearing our own knocks.

**Knocker serial protocol** (`firmware/knocker`): 115200 baud, ASCII lines ending in `\n`.

| Command | Reply |
|---|---|
| `PING` | `PONG knocker v1` |
| `K <n> <interval_ms>` | `OK K <n>`, sent after the last strike |
| `TEST` | `OK TEST` |
| `CAL <rest_deg> <strike_deg> <hold_ms>` | `OK CAL ...` |
| anything invalid | `ERR <reason>` |

On boot the firmware prints `READY knocker v1 ...`.

**Black box** (`runs/<YYYYmmdd-HHMMSS>/`)
- `events.jsonl` holds one record per line: `{"seq","t","wall","type","data","prev","hash"}`.
  - `hash = sha256_hex(prev + canonical_json({seq,t,wall,type,data}))`, where canonical JSON is `sort_keys`, separators `(",",":")`.
  - The first record's `prev` is 64 zeros.
- `clips/<seq>.wav`: its path and sha256 are added to `data` before hashing.
- `raw.wav`: the whole session.
- `meta.json`

## CLI
All commands are under `uv run hb`:

| Command | Prompt | What it does |
|---|---|---|
| `devices` | P01 | list audio devices |
| `record` | P01 | record labeled training audio |
| `listen` | P01 | print detected sounds |
| `synth` | P01 | write a synthetic test WAV |
| `run` | P03 | full sentinel |
| `sim` | P03 | simulated survivor, end to end |
| `verify` | P04 | check black-box integrity |
| `replay` | P04 | re-run detection on a recorded session |
| `report` | P04 | summarize a session |
| `doctor` | P06 | hardware and setup checks |
| `bench` | P06 | speed measurements |

## Rules
1. Use Python **3.11** (pinned to match Raspberry Pi OS), managed with **uv**. `uv run pytest -q` must pass before you finish.
2. Code must run on macOS (dev) and on Raspberry Pi 5 (Linux aarch64), on the **CPU**.
   - No CUDA, no full TensorFlow, no PyTorch.
   - Allowed: numpy, scipy, sounddevice, soundfile, pyyaml, fastapi, uvicorn, pyserial, scikit-learn, joblib, ai-edge-litert, pytest, httpx.
   - **Ask before adding anything else.**
3. **Tests never need hardware, a microphone or the network.** Use `heartbeat/synth.py` and fakes. Humans run the commands that touch the mic, serial port, speaker or Pi. Do not run them yourself.
4. **Stay inside the files your prompt says you own.** If a shared contract must change, explain it in your report instead of silently changing it.
5. Tunable numbers belong in `profiles/*.yaml`, each with a one-line comment, not hard-coded.
6. Keep it hackathon-simple: few files, readable code, no extra frameworks.
   - **Log, don't crash.** A hardware failure becomes an `error` event and the demo keeps running.
7. A language model never drives motors and never decides `CONFIRMED`. Only the deterministic engine does.
8. Commit when tests pass:
   - Stage only your files: `git add <paths>`, never `git add -A`.
   - Message: `P0X: <summary>`.
9. End with a short **report**:
   - what you built
   - exact commands for a human to try it
   - test results
   - what is unverified (anything that needs hardware)
   - any contract changes or open questions
