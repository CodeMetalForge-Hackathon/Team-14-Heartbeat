# Supervised 50 cm Decisions check

The operator requested tabletop testing with movement, camera/API decisions,
knocks and other audio signals. `pi-full-check` enables knock and vocal monitoring,
retains 35% drive power and the existing sensor/freshness/STOP guards, and caps
the sum of forward command distances at 50 cm. The profile now uses continuous
forward travel: the first accepted forward proposal dispatches one guarded
`CRUISE 50 35 15000` target. Camera/API decisions continue during motion, using
the firmware's streamed front-range readings. Continued forward proposals do
not issue another motor command or reset PWM. Interrupted or failed
targets consume their entire requested distance because partial travel is unknown.
Pause/resume does not reset this budget. A new process starts a new budget.

This is a command-distance limit, not calibrated physical travel. Track slip,
calibration error and coasting are not measured. The sensor cannot detect table
edges. Keep the whole intended path and stopping margin on the surface, with
the power switch reachable. A broad target about 100 cm in front allows a 50 cm
test while retaining the 45 cm start-clearance rule. Invalid range stops the run.
The API can choose HOLD or investigate audio before reaching the distance cap.
Turns stay disabled in the command below. GPS does not guide this controller.

Continuous mode requires firmware advertising `CAPS guarded=1 cruise=1`.
Older firmware is rejected before dispatch; there is no unguarded fallback.
The normal profile still uses 3 cm STEP commands. CRUISE retains local range,
encoder, stall, heartbeat and time limits, and stops at its encoder target.
HOLD, low-confidence proposals, stale camera/range evidence, API errors, audio
investigation and operator STOP interrupt it. These stops remain intentional;
only the routine stop every 3 cm has been removed. The host also stops renewing
movement permission if processing stops supplying fresh observations.

The test profile's motion confidence threshold is 0.50 (the default remains
0.70). This permits lower-scoring forward proposals through the confidence
check; explicit HOLD choices and other movement guards still apply.

Stop the existing CLAP service with Ctrl-C in its terminal, then restart it:

```bash
cd ~/Team-14
bash scripts/clap_pi.sh --profile profiles/pi-full-check.yaml
```

After `CLAP ready`, run this in another Pi terminal:

```bash
cd ~/Team-14
systemctl --user stop lifescout.service &&
uv run --no-sync --env-file .env.local hb patrol \
  --decisions --arm-motion --profile pi-full-check \
  --rover /dev/serial/by-id/usb-1a86_USB_Serial-if00-port0 \
  --device stick:/dev/serial/by-id/usb-Hades2001_M5stack_81520161EE-if00-port0 \
  --sensor-port 6 --knocker manual \
  2>&1 | tee ~/pi-full-pipeline.log
```

This sends camera images to the Decisions API using the existing local key.
Navigation ends after 60 seconds or the forward command budget; an active
stationary audio verification/dialog may continue afterward. `e` then Enter
requests E-STOP; Ctrl-C also stops. Use the physical switch if STOP fails.

Let movement begin before producing a short knock sequence. Accepted audio
causes STOP and fresh stationary verification; it can end travel before 50 cm.
Follow printed prompts during verification. `manual` prints prompts rather than
testing a speaker or physical knocker. Vocal monitoring classifies shout/scream/
crying; transcription and help-phrase recognition remain disabled. Monitor errors
or inference lag stop navigation. Combined live throughput is still unverified.

The run prints its new `runs/...` directory and saves events and raw audio there.
Measure physical travel from a marked chassis reference point before claiming
distance accuracy. A rejected or held proposal is not a failed motor test.

## First run and follow-up

`runs/20261108-020216` completed its 60-second budget without movement or logged
errors. All 62 completed proposals had FORWARD_SHORT available with front range
88.55–89.12 cm; the API chose HOLD 55 times and STOP_AND_LISTEN seven times.
It never selected forward. Lowering the motion-confidence threshold would not
turn those choices into forward actions. The test profile now explicitly asks
for forward progress when offered and the observed next step is supported and
unobstructed, while retaining the confidence and obstacle checks.

The run also exposed vocal-window starvation: transitions among OBSERVING,
DECIDING, HOLDING and MOVING reset the three-second audio buffer repeatedly.
Those states now share one monitoring phase; STOPPING, settling and physical
prompts still discard old audio and require fresh stationary evidence.
HOLD is now logged as model_hold regardless of its score, distinguishing an
API choice from a forward choice blocked by the confidence threshold.

The next operator run, `runs/20261108-022320`, completed 17 proposals: 16 HOLD
and one FORWARD_SHORT at confidence 0.62, blocked by the previous 0.70 threshold.
Knocks triggered stationary verification and a challenge, ending POSSIBLE with
reported odds of 1 in 247. Vocal windows produced observations throughout the
navigation phase. No motion command was dispatched. The requested 0.50 test
threshold allows that score through, while preserving explicit HOLD choices.

## Live repeat with the 0.50 threshold

`runs/20261108-023003` used the command above with the updated profile. Seven
forward proposals (scores 0.55–0.75) produced seven acknowledged
`STEP 3 35 1000` commands: 21 cm total requested travel, not a physical distance
measurement. The API also chose HOLD 27 times. At 35.28 seconds an invalid
front-range reading caused STOP and REVIEW; STOP was acknowledged. The run was
closed with `q` at 50.46 seconds without resuming movement. Its nonzero exit
status reflects the sensor error and operator abort, not a successful 50 cm run.

All 44 vocal observations were OTHER, and no knock investigation occurred in
this repeat. This establishes guarded forward-command execution with the lower
threshold; knock-response performance was not revalidated. The earlier run
above contains the knock/dialog evidence.

Software validation: `uv run pytest -q` passed all 920 tests, with one existing
Starlette/httpx deprecation warning. No shared audio or dialog contracts changed.

## Continuous-mode validation

The subsequent continuous-mode change passed 132 focused controller/serial
tests (`uv run --no-sync pytest -q tests/test_decision_distance.py
tests/test_rover.py`), plus the firmware simulation
(`uv run --no-sync python tools/check_rover_firmware.py`). Audio patrol checks
also passed during the focused check. The firmware simulation checks uninterrupted
power beyond 3 cm, final-target completion, obstacle/invalid-range stops,
watchdog expiry and command deadlines. The Mega 2560 build uses 26,140 bytes of
flash and 2,224 bytes of static RAM. The full suite was not repeated for this
change, following the operator's request to keep testing focused.

Installation is pending: USB upload timed out, and STOP/PING probes received
corrupted serial data instead of rover acknowledgements. Reconnect the rover
USB cable and reset the Auriga before retrying the upload. No live continuous
movement has been validated. This update adds CRUISE/CAPS protocol support;
it does not change the audio or deterministic dialog contracts.
