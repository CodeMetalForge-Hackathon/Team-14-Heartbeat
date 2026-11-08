# Prompts

Every prompt we gave coding agents lives here, as the hackathon requires. Claude (Opus 5.5) planned and wrote them; Codex executed them. Each prompt assumes the agent has read `../AGENTS.md`. Codex loads that file automatically from the repo root.

## Order and lanes
Run two Codex sessions in parallel. They never edit the same files.

| Lane A | Lane B |
|---|---|
| **P01** scaffold, audio, onset detector, recorder | **P02** servo knocker firmware (start now) |
| **P03** dialog engine, stats, prompters, `hb sim` (needs P01) | **P05** classifier and data tools (needs P01, plus some recorded data) |
| **P04** black box and dashboard (needs P03) | **P07** M5StickC Plus SE as the microphone (needs P05) |
| **P06** Pi deploy, doctor, bench (needs P04) | **P08** rover bridge + patrol (needs P07) |

**Lane C** (third session, in parallel): **FIX-01**, dialog noise guards and fast-knock features, from the P03/P05 review.

**Demo hardening** (Saturday evening; the two can run in parallel because they touch different files):
- **CLAP-05:** a CLAP failure degrades to the heuristic instead of stopping the session.
- **EVAL-01:** a scorecard on the real stick recordings. Run it on the laptop that has `data/raw/`.

The hookup guide is `docs/DEMO-HOOKUP.md`.

**Rover recovery** (Sunday early morning, on the Pi, operator at the rover):
- **PI-04:** find out why the Auriga stopped answering and a track ran by itself. Checks power, the port and a possible competing service before **one** clean upload. Go/no-go is at 08:00.
- **PI-05:** continue recovery on the replacement Pi 5 with NVMe M.2 HAT+;
  start with [the migration handoff](../docs/NEW-PI-HANDOFF.md). Recovery is
  still pending; the preserved firmware HEX is included for the new host.

**Demo polish** (on the laptop; it touches only `dashboard/`, so it can run alongside the Pi work):
- **UI-01:** a command-post dashboard with a status hero, a call-and-response strip, a heartbeat-style trace, presentation mode and a route-schematic search map. Works offline; no fake GPS.

Later prompts get written after we see these results:

- P09: LoRa link, or a wireless (Wi-Fi) stick node
- P10: mmWave breathing
- P11: SLM field brain and orchestrator
- P12: avalanche, mine and hull profiles

## How to run one
In the repo root, start Codex and say:

> Read AGENTS.md and prompts/P01-scaffold-detector.md, then do P01. Follow AGENTS.md rules exactly.

When it finishes:
1. Paste its report, plus any failing output, back to Claude. Claude reviews it and writes fixes or the next prompt.
2. Save each fix prompt Claude writes as `prompts/P0X-fix-N.md`, so the record stays complete.

Two rules for every task:
- Codex runs only the tests. Humans run anything that touches the mic, the serial port, the speaker or the Pi.
- If Codex drifts, tell it: "Re-read AGENTS.md. Stay inside the files your prompt owns."
