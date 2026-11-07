# FIX-01: dialog noise guards and fast human knocks

Read `AGENTS.md` first. **Run this in a third Codex session (lane C).** It runs in parallel with P04 (lane A: server, blackbox, cli/runner wiring) and P07 (lane B: stick mic, `prompter.py` auto-scan, `audio` config).

**Re-read every shared file right before you patch it.**

## Why: two problems from the P03 and P05 reviews
**1. Periodic noise can be confirmed as a person.**
- P05 injected an accepted knock every 1.5 s and got **5 CONFIRMED in 300 s**. See "Periodic-event stress finding" in `reports/P05.md`, which also has the reproduction script.
- **Cause:** with a drip every 1.5 s, the response window never sees 2 s of silence, so it runs the full 6 s and "hears 4". That matches k=3 (with tolerance) and k=4 exactly. Meanwhile λ sits near the 1-per-minute prior, because the challenge starts with no passive observation, and `reset` clears the background estimate.
- **P03's own known limitation:** at λ = 1/s, four exact matches still confirm.

**2. Human-speed knocks get rejected.**
- Claude measured the heuristic classifier on synthetic knocks in groups of 3:

  | Spacing | Accepted |
  |---|---|
  | 1.1 s | 18/18 |
  | 0.6 s | 11/18 |
  | 0.45 s | 12/18 |
  | 0.3 s | 14/18 |

- People copy our prompt at about 0.4–0.7 s spacing. The 0.975 s clip contains the *next* knock, which breaks decay and other DSP features. `hb sim` hid this by spacing the survivor's knocks 1.1 s apart.
- In a live demo this makes us hear "2" when the person knocked "3", so rounds fail.

## Files you own
- `heartbeat/dialog.py`, `heartbeat/stats.py`
- `heartbeat/classifiers/features.py`, `heuristic.py`, `head.py`
- `tools/eval_background.py`: only if needed for the new states
- `tests/test_dialog.py`, `tests/test_stats.py`, `tests/test_classifiers.py`, `tests/test_sim.py`
- New: `tests/test_noise_guards.py`
- `profiles/rubble.yaml`: the `dialog` and `classifier` sections, plus the `fake_*` keys in `prompter`
- New: `reports/FIX-01.md`

**Do not edit** `cli.py`, `runner.py`, `server.py`, `prompter.py` code, `audio.py`, `config.py` or `dashboard/`.

## Part A: dialog noise guards (all values in the profile, with comments)
1. **A real answer ends in silence.**
   - A round can match only if its window **closed early**: `early_close_s` of silence after the last counted knock, before `window_s` ran out.
   - If the window times out while knocks are still arriving, record the round as `noisy`: not matched, and not "empty."
   - Setting: `require_silence_after: true`.
2. **A real answer starts promptly.**
   - The first counted knock must arrive within `response_start_max_s` (4.0) of the window opening; otherwise the round doesn't match.
3. **Check for quiet before asking.**
   - Before every challenge round, listen passively for `pre_listen_s` (3.0) and count knock detections.
   - Compute `λ_used = max(λ_long_run, pre_count / pre_listen_s, lambda_floor)`.
   - If `pre_count >= max_pre_knocks` (2), or `μ = λ_used * window_s > mu_max` (1.0), **don't prompt**:
     - Enter state `TOO_NOISY` and emit a `state` event whose text says "Too noisy: call for quiet."
     - Retry after `noisy_retry_s` (5.0).
     - After `max_noisy_tries` (6), go to `NO_RESPONSE`.
   - Rescue teams really do call "all quiet" before listening, so this is a feature, not a hack.
4. **Use μ in every round's p.** Every round's p uses `λ_used`. A round can never match while `μ > mu_max`; this closes P03's λ = 1/s limitation.
5. **Keep the noise memory.**
   - `BackgroundRate` keeps observing during all ungated time outside response windows: cooldown, pre-listen, interview gaps.
   - `reset` clears rounds, answers and approval, **not** the noise history.
6. **Optional, only if quick:** reject bounce patterns. A response whose gaps between knocks shrink by more than 30% each time (a dropped object bouncing) doesn't match. Setting: `reject_bounce: true`.
7. **Snapshot changes.** `snapshot()` gains `noisy: bool`, `mu` and `lambda_used_per_min`. Existing keys stay unchanged, because P04's dashboard reads them.

## Part B: fast knocks in the classifiers
1. **Narrow the DSP feature window.** In `features.py`, compute the DSP features (attack, decay, spectra, crest) on a **short window around the onset**:
   - from 0.1 s before the onset to `feature_post_s` (0.25 s) after it
   - stop early at the next onset inside the clip: find it with a simple envelope rise above the decay floor

   Keep the full clip for YAMNet.
2. **Re-tune the heuristic** on synthetic data with **mixed spacing** (0.3–1.2 s).
3. **Retrain the synthetic head** with mixed spacing, and save `models/knock_head.joblib` again, with its threshold.

   Keep the feature spec versioned: if an old head's feature spec doesn't match, `auto` must fall back with a clear warning instead of crashing.
4. **Make the simulated survivor human-speed.** Set `prompter.fake_knock_interval_s: 0.55` and `fake_jitter_s: 0.08` in the profile.

## Tests (no hardware)
**Periodic noise never confirms.** Run P05's stress fixture (`simulate_dialog_events`) with challenges as in `reports/P05.md`. Every period must give **0 confirmations in 300 s**:

| Period (s) | Confirmations |
|---|---|
| 0.7 | 0 |
| 1.0 | 0 |
| 1.5 | 0 |
| 2.5 | 0 |
| 3.0 | 0 |
| 5.0 | 0 |

**Noise guards behave correctly:**
- At λ = 1/s, the engine reaches `TOO_NOISY` and never `CONFIRMED`.
- A pre-listen window with 2 knocks blocks the prompt, and the engine retries after `noisy_retry_s`.
- `reset` keeps λ.

**Fast knocks are accepted.** Groups of 3 at 0.3, 0.45, 0.6 and 1.1 s spacing:
- at least 95% accepted by `heuristic`
- at least 95% accepted by `head`, when the ML extras are installed; skip otherwise

**Real survivors still get through:**
- `hb sim --people 2` still confirms within 60 s, and the answers are correct.
- A "tired survivor" (0.8 s spacing, ±0.15 s jitter, start delay 2.5 s) also confirms.

**Noise-only runs:**
- `hb sim --no-survivor --noise` gives 0 confirmations over 10 minutes.

**Regressions:** the full suite passes.

## Done when
1. All tests pass.
2. `reports/FIX-01.md` has a **before/after table**: confirmations per period, and fast-knock acceptance per classifier.

   This is pitch material: "we red-teamed our own detector."
3. Commit only your files: `FIX-01: dialog noise guards + fast-knock features`.

Report the new profile keys and any behavior the dashboard should show. P04 or a later prompt will display `TOO_NOISY`.
