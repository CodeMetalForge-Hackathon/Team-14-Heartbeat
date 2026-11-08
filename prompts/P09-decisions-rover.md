# P09: Decisions API rover build

## Follow-up: Mac webcam preview

After the saved-photo command failed because the example file did not exist,
the user accepted the offered webcam capture option (verbatim):

> yeah

Follow-up questions (verbatim):

> so the first photo will tell the first decision ?

> can the first decision be just move ?

Added `decisions-preview --webcam`: a loopback browser page with explicit camera
permission, one JPEG per Capture & decide click, editable example range and
optional turning choices. The first recommendation may be FORWARD_SHORT, but
this preview cannot execute motor commands. It uses existing FastAPI/uvicorn and
browser camera APIs, adds no dependencies, and keeps the API key server-side.
No camera, hardware, live API request or tests were run while building it.

## Follow-up: local macOS usage (verbatim)

> how I can test locally on macOS ?

Added a one-image `decisions-preview` CLI path for local macOS API inspection.
It uses the already authorized project key, marks supplied range as example
telemetry, and cannot open hardware or execute motion. No tests or live API
requests were run while adding the command.

## Latest user build request (verbatim)

> All right, let's build this. What do you need? Do you need it? Do you need the 5-5 connected right now to fully build this? Should we connect it to the rover?

Follow-up authorizations (verbatim):

> also u can use gpt 6.1 sol xhigh as ur subagents if needed. u can use openai developers plugin for api key

> yeah u can create a new one i don't care, let's go.

Earlier persistent user constraint (verbatim):

> do not make test

## Accepted feature and authorization scope

Build the accepted Decisions API driven rover feature: camera plus sensor state
leads to a finite action recommendation, local validation, and brief interruptible
movement. Local obstacle/unknown-range, encoder/stall, watchdog, knock STOP and
E-STOP controls remain authoritative. Deterministic dialog alone decides
`CONFIRMED`. The model never supplies raw motor parameters.

The user authorized implementation and, if useful, `gpt-6.1-sol` subagents with
`xhigh` reasoning. The user also authorized the OpenAI Developers plugin for API
key setup and creation of a new key. The approved local destination is the
original repository's ignored `.env.local`; no secret value belongs in this
prompt, source, logs or commit. The selected key expires seven days after creation.

Create/run no tests. Make no live Decisions/API inference calls, operate no
hardware, and upload no firmware during this build. Source review, syntax checks
and firmware compilation are allowed. Build in staging; preserve earlier prompts.
The Pi 5 need not be connected to implement or compile the feature.

## Required implementation behavior

- Default to observation only; `--arm-motion` explicitly permits guarded forward
  execution. Offer turns only with `--allow-turns` after direction/side verification.
- Stop and settle before an observation; allow one in-flight request; stay stopped
  while waiting. Reject stale, invalid, canceled or unavailable actions locally.
- Use fixed profile bounds for forward/left/right choices, plus HOLD and
  STOP_AND_LISTEN. Preserve the independent audio STOP → settle → verify → dialog
  → operator-review flow and existing pause/abort/E-STOP behavior.
- Add a capability handshake and guarded STEP/PIVOT firmware primitives, fresh
  preflight and repeated front-range checks, both-wheel encoder completion and a
  maximum 2000 ms deadline. Unknown range/obstacle/timeout fails stopped with no
  autonomous fallback or restart. Preserve stall/direction/slew/watchdog guards.
- Old firmware must fail stopped without DRIVE/TURN fallback. `--dry-run` is
  incompatible because the existing fake cannot validate these guards.
- Add no dependencies; never log camera images, prompts, request bodies or keys.
- Runtime uses Pi CSI/rpicam-vid, USB Auriga, USB microphone, speaker and network.
  If CLAP remains on the Mac, the Pi profile must use the Mac's reachable LAN
  endpoint rather than Pi localhost; do not assume local PyTorch CLAP on Pi.
- Preserve the known unresolved encoder discrepancy and intermittent ultrasonic
  fault as readiness limitations. Do not present physical arming as ready.

Deliver a practical guide with human-only commands and explicit validation limits.

## Follow-up: hardware clarification and publication

The user clarified that the device connected to Auriga port 6 is the module
previously read through RANGE. It is an ultrasonic distance sensor, not a camera.
The user asked whether distance-only operation and Raspberry Pi deployment are
possible. Sensor-only Decisions was discussed but was not implemented after the
user said (verbatim):

> no i will just run it on rapsberrry pi

The existing --explore mode remains the available ultrasonic-only navigation
path. --decisions still requires camera input. Pi deployment still needs its
serial device paths and a reachable CLAP service; hardware faults remain open.
No camera relay or sensor-only Decisions changes are included in this commit.

Latest publication request (verbatim):

> ok, commit the changes and push

Publish the completed feature on the current branch, excluding .env.local and
unrelated .idea files. The user's earlier instruction not to create or run tests
remains in effect; do not represent this commit as tested on the rover or Pi.
