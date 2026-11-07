# CLAP-03 report

Added optional BREATHING to the continuous monitor and enabled it in
`profiles/clap-distress.yaml`. Descriptions cover breathing and panting; wind,
fans, and clothing rustling were added as comparison sounds. Breathing alone
does not trigger a distress alert, transcription, knock counting, or motors.
Older monitor profiles without BREATHING still validate. Observation event
fields are unchanged; BREATHING is an additive label. No new dependencies.

Validation: `uv run pytest -q` reports 729 passed, 15 skipped, with the existing
Starlette/httpx warning. Tests cover informational-only breathing observations,
no transcription on breathing, and older-profile compatibility. The restarted
local service on port 8767 includes BREATHING. A saved-recording smoke check
still produced SPEECH with no distress flag. No hardware was opened by the
agent. Actual breathing recognition, distance limits, and false-positive rates
remain unverified; the existing -50 dBFS quiet-window gate is unchanged.

A human can test from `/private/tmp/team14-clap-integration`:

```bash
uv run hb listen --device stick:/dev/cu.usbserial-81520161EE --profile clap-distress --debug
```

The updated service was left running. To start it later:

```bash
uv run --python 3.12 --script tools/clap_service.py --profile profiles/clap-distress.yaml
```

See `docs/DISTRESS.md` for test conditions and limitations. Changes remain in the
integration checkout on `codex/streaming-clap`; the Desktop checkout is unchanged.
Updated cumulative and incremental patches are supplied for transfer.
