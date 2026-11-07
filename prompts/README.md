# Prompts

Every prompt we gave coding agents lives here, as the hackathon requires. Claude (Opus 5.5) planned and wrote them; Codex executed them. Each prompt assumes the agent has read `../AGENTS.md`. Codex loads that file automatically from the repo root.

## Order and lanes
Run two Codex sessions in parallel. They never edit the same files.

| Lane A | Lane B |
|---|---|
| **P01** scaffold, audio, onset detector, recorder | **P02** servo knocker firmware (start now) |
| **P03** dialog engine, stats, prompters, `hb sim` (needs P01) | **P05** classifier and data tools (needs P01, plus some recorded data) |
| **P04** black box and dashboard (needs P03) | |
| **P06** Pi deploy, doctor, bench (needs P04) | |

Later prompts get written after we see these results:
- P07: rover bridge for the mBot Ranger (stop → listen → move)
- P08: LoRa link
- P09: mmWave breathing
- P10: SLM field brain and orchestrator
- P11: avalanche, mine and hull profiles

## How to run one
In the repo root, start Codex and say:

> Read AGENTS.md and prompts/P01-scaffold-detector.md, then do P01. Follow AGENTS.md rules exactly.

When it finishes:
1. Paste its report, plus any failing output, back to Claude. Claude reviews it and writes fixes or the next prompt.
2. Save each fix prompt Claude writes as `prompts/P0X-fix-N.md`, so the record stays complete.

Two rules for every task:
- Codex runs only the tests. Humans run anything that touches the mic, the serial port, the speaker or the Pi.
- If Codex drifts, tell it: "Re-read AGENTS.md. Stay inside the files your prompt owns."
