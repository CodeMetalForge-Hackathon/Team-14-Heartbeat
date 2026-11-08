# CLAP-05: a CLAP hiccup must not end the demo

Read `AGENTS.md` first, then `docs/CLAP.md` and `docs/DISTRESS.md`.

## Why
Today any CLAP failure **stops the audio session**: a timeout, a refused connection, a full queue (`max_pending`), a result older than `max_lag_s`, or an invalid reply. Startup also fails if `/health` doesn't answer.

In the live demo the listener runs on the Pi and the CLAP service may sit on a Mac over a phone hotspot. A Wi-Fi blip, a slow Pi CPU, or a service restart would kill the run in front of the judges.

The dialog engine already makes the confirmation decision, so a weaker classifier for a few seconds is safe. A dead session is not.

**Keep the original rule:** an impact is never turned into a knock without being classified. Degrading means classifying it with the fallback classifier, and saying so loudly.

**The Pi makes this likely, not rare.** P06 measured about 0.46 s per `/classify` and 0.5 s per `/observe` on the Pi 5. The single inference worker can therefore handle roughly 2 onsets per second, while the detector can emit up to about 8 per second (`refractory_s` 0.12).

Today **every** onset is sent to CLAP, including **gated** ones. That covers our own voice prompts, the servo's knocks, rover track noise, and nearby talking. Any of these can fill `max_pending` (4) within about a second, and that stops the session. The first spoken prompt on the Pi could be enough.

## Files you own
- `heartbeat/runner.py`: CLAP failure paths only
- `heartbeat/classifiers/clap.py`
- `heartbeat/clap_settings.py`
- `heartbeat/audio_monitor.py`: pause and resume only
- `heartbeat/doctor.py`: the new CLAP check
- `dashboard/`: the banner
- `profiles/clap.yaml`, `profiles/clap-distress.yaml`: the new keys
- tests

**Don't touch** `dialog.py`, `tools/clap_service.py` or the service protocol.

## Changes
1. **New profile keys** under `classifier.clap`:
   - `on_failure: fallback | stop`, default `fallback`
   - `fallback: heuristic`: any registry name except `clap`
   - `retry_s: 10`: how often to probe `/health` while degraded
   - Validate them like the other keys. `stop` keeps today's behavior exactly.
2. **Degrade on any CLAP error, timeout or lag** when `on_failure: fallback`:
   - Every onset that is queued, in flight, or arrives while degraded is classified **with the fallback**, keeping its original stream time. Nothing is dropped, and the dialog keeps running.
   - `Detection.classifier` shows which classifier decided each sound, so the black box records it.
   - Emit one `error` event with `code: clap_degraded`, the reason and `severity: warning`. Don't emit one per onset.
   - A late CLAP result for an onset already decided by the fallback is ignored, so nothing is counted twice.
3. **Recover:** while degraded, probe `/health` every `retry_s` without blocking the audio loop. When identity matches again, return to CLAP for new onsets, and emit `status` with `code: clap_restored`.
4. **Startup:** if `/health` fails at start and `on_failure: fallback`, start degraded with the warning, not an exception. A **model or description mismatch** is still a hard startup error, because it means a wrong profile, not a network blip.
5. **Gated onsets never go to CLAP.**
   - An onset with `gated=True` (our own prompt, plus the gate tail) is classified by the fallback, for the log only.
   - It's still published as a gated `sound`, so the dialog and patrol behave exactly as before. Gated sounds are already excluded from answers and cues.
   - This works in both normal and degraded mode.
   - **Test:** a 4-second voice prompt producing 12 gated onsets on a slow fake CLAP makes no CLAP calls, and doesn't stop or degrade.
6. **Queue pressure:** when `max_pending` is reached, overflow onsets go to the fallback **individually**, without entering degraded mode. Emit one rate-limited `status` event per 10 s: "CLAP busy: N sounds classified by heuristic". Only real errors, timeouts or lag enter degraded mode.
7. **Distress monitor:** pause new `/observe` windows while degraded, resume on restore, and emit one status line each way. Monitor errors still never become knocks.
8. **Dashboard:** a visible banner, "CLAP offline: using heuristic (retrying)", that clears on restore.
9. **`hb doctor` with a CLAP profile:**
   - Check `/health` and the identity match.
   - Time 5 `/classify` calls with a synthetic knock from `heartbeat.synth`.
   - Print the median and max ms.
   - **Pass** if the max is under half of `timeout_s`; otherwise warn: "CLAP too slow for live use here: raise timeout_s or run the service on a faster machine."

## Tests (fake request function, no network)
- A timeout mid-session: the session continues, the next onsets get `classifier: heuristic`, and exactly one `clap_degraded` event is emitted.
- A full queue sends the overflow onsets to the fallback without degrading. The max-lag path degrades instead of stopping.
- Recovery after `/health` succeeds: new onsets use CLAP again, and `clap_restored` is emitted.
- A late CLAP result after the fallback decision is ignored.
- Service down at startup gives a degraded start. An identity mismatch at startup is still an error.
- `on_failure: stop` behaves exactly as before; the existing tests keep passing.
- A full `hb sim` dialog with a fake CLAP that fails halfway still reaches `CONFIRMED`.
- The doctor check, with fake fast and slow services.
- The full suite passes.

## Done when
- The tests pass.
- Commit: `CLAP-05: degrade to fallback classifier instead of stopping`.
- Report the new keys, the dashboard banner text, and the doctor output for a fake service.
