# CLAP-02 report

Added optional continuous vocal monitoring using three-second windows at
one-second intervals. Separate SCREAM, SHOUT, CRYING, SPEECH, OTHER, and UNCERTAIN
observations appear in the console and dashboard activity log. Possible distress
flags request review and never feed the knock dialog or drive motors.

Added optional local Whisper English transcription and whole-word help-phrase
matching. ASR is off by default; model download requires the existing explicit
service option. Core project dependencies are unchanged.

Shared contracts: additive `audio_observation` and `audio_monitor_status` events;
optional CLAP `monitor` configuration; `/observe` endpoint with fixed-size PCM
and ordered prompt/model/settings identity. Existing detection fields, knock
scores, dialog APIs, and default profiles are unchanged. Prompt-overlap windows
and stale results are suppressed. The monitor shares the bounded inference
worker, retains only the newest pending window, and reports skipped coverage.

Validation: full suite 727 passed, 15 skipped; existing Starlette/httpx warning.
New tests cover continuous detection without impacts, bounded storage, silence,
prompt suppression, stale results, discontinuities, busy/failing inference,
strict response/config identity, whole-word matching, optional transcription
guards, HTTP body limits, and separation from knock dialog hooks. Dashboard
JavaScript passed `node --check`.

Real cached-CLAP smoke checks replayed existing saved M5Stick recordings without
hardware. The knock recording retained knock detections and generated OTHER
vocal observations. This verifies execution and class separation, not acoustic
accuracy. Real screaming/crying, ASR transcription, live prompt gating, long-run
stick reliability, and Pi/Jetson deployment remain unverified. ASR tests use a
fake recognizer; its weights were not downloaded.

Human commands and limitations are in `docs/DISTRESS.md`. Start the service with
`--profile profiles/clap-distress.yaml`; select `--profile clap-distress` in
`hb listen` or `hb run`. Port 8767 keeps this service separate from the original
knock-only service. Humans run all microphone/serial/speaker/motor commands.

Changes are in `/private/tmp/team14-clap-integration` on `codex/streaming-clap`.
The inaccessible Desktop repository remains unchanged. A cumulative patch and
an incremental CLAP-02 patch are supplied for transfer.
