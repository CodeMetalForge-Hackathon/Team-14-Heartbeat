# FIX-01: noise guards and fast human knocks

We red-teamed the detector with already-accepted periodic knocks and generated
human-speed knock groups. The dialog now checks for quiet before every round,
requires an answer to start promptly and finish in silence, and retains its
background-rate history across operator resets. DSP features use a short local
impact window; YAMNet still receives the complete 15,600-sample clip.

## Before/after: periodic accepted events

Each row replays 300 seconds using `simulate_dialog_events`, with challenges
every 60 seconds and the real prompt asset durations. The baseline is commit
`4529c32`; the after measurement uses this change's profile. These fixtures
bypass the acoustic classifier and measure dialog robustness, not a venue WAV
false-alarm rate.

| Period (seconds) | Before confirmations | After confirmations |
| --- | ---: | ---: |
| 0.7 | 0 | 0 |
| 1.0 | 0 | 0 |
| 1.5 | 5 | 0 |
| 2.5 | 0 | 0 |
| 3.0 | 0 | 0 |
| 5.0 | 0 | 0 |

The 1.5-second baseline confirms at 37.5260625, 97.5260625, 157.5260625,
217.5260625 and 277.5260625 seconds, reproducing P05's adverse finding.

## Before/after: fast-knock acceptance

The same fixture is measured before and after: six independent seeded scenes
per spacing, three knocks per scene, first knock at 0.6 seconds, gain -6 dB,
background -50 dBFS, and actual onset-detector clips. All 18 onsets are detected
at each spacing. These repeatable baseline counts differ slightly from the
original prompt's separate heuristic measurement.

| Spacing (seconds) | Heuristic before | Heuristic after | Head before | Head after |
| --- | ---: | ---: | ---: | ---: |
| 0.30 | 15/18 (83.3%) | 18/18 (100%) | 7/18 (38.9%) | 18/18 (100%) |
| 0.45 | 15/18 (83.3%) | 18/18 (100%) | 6/18 (33.3%) | 18/18 (100%) |
| 0.60 | 12/18 (66.7%) | 18/18 (100%) | 8/18 (44.4%) | 18/18 (100%) |
| 1.10 | 18/18 (100%) | 18/18 (100%) | 18/18 (100%) | 18/18 (100%) |

A broader check uses 100 seeds per spacing, gains sampled from -9 to -1 dB and
background levels from -60 to -40 dBFS. The heuristic accepts 300/300 at every
spacing. The head accepts **273/300 (91%) at 0.30 seconds** and **290/300
(96.7%) at each other spacing**. Thus the required fixed-condition fixtures
pass, but the head still falls below 95% for very fast knocks with this broader
gain/noise variation. The small fixed fixture is not a general recall guarantee.

## Classifier changes and calibration

DSP uses the 0.1-second pre-roll plus up to 0.25 seconds after onset. A later
envelope rise ends the window early, after sustained decay-floor quiet, so
resonant beating within one knock is not mistaken for a new impact. The
analyzed duration is also a feature, allowing the head to use that early-stop
evidence. Feature spec **v2** contains 40 DSP features plus all 521 YAMNet class
scores. The model exposes no embedding. Old feature specs fail compatibility
checks with an explicit warning and `auto` continues to the next classifier.

New `classifier.features` keys are `feature_post_s: 0.25`,
`feature_next_rise_db: 8.0`, `feature_next_min_s: 0.03`, and
`feature_next_quiet_s: 0.012`. Heuristic `attack_max_s` changes to 0.008 and
`decay_max_s` to 0.11. Invalid nonfinite rule limits or weights are rejected.

The synthetic training recipe uses 96 independent recordings for each of five
sound kinds (480 total), with source-recording-disjoint fitting, calibration,
and grouped assessment. New `classifier.training.synthetic` keys are
`interval_min_s: 0.3`, `interval_max_s: 1.2`, `steady_fraction: 0.5`, and
`spacing_jitter_s: 0.04`. Half the recordings use steady tempos with jitter;
the rest use independently varied gaps. All negative classes, including drops,
remain in training. Segmentation produces 3,117 clips: 576 knocks and 2,541
negatives. Training data SHA-256:
`7cd17c09c4a94b94bbe809619a9c6634f7d2590223f2a29daf4487254e899bcf`.

The chosen profile and artifact threshold is **0.3681996792882003**. This is an
explicit sensitivity tradeoff: `classifier.training.max_false_positive_rate`
changes from 0.02 to **0.05**. The strict 2% calibration target rejected too many
fast knocks. At the selected operating point:

| Partition | True knocks accepted | Other sounds accepted | Recall | FPR |
| --- | ---: | ---: | ---: | ---: |
| Final calibration, 120 recording groups | 136/144 | 33/661 | 94.44% | 4.99% |
| Grouped assessment, each fold calibrates its own threshold | 420/432 | 82/1,880 | 97.22% | 4.36% |

These synthetic rates do not establish real contact-microphone performance.
The final model is saved in `models/knock_head.joblib`; no model download,
TensorFlow, PyTorch, CUDA, or hardware access was used for this change.

## Dialog behavior and profile settings

All settings below are in `profiles/rubble.yaml`, with comments. New dialog
settings are:

| Key | Value | Behavior |
| --- | ---: | --- |
| `require_silence_after` | `true` | A match must close strictly before the response deadline. |
| `response_start_max_s` | `4.0` | Latest first response knock, measured from window opening. |
| `pre_listen_s` | `3.0` | Ungated passive listening before every challenge round. |
| `max_pre_knocks` | `2` | Pre-listen count that blocks prompting. |
| `mu_max` | `1.0` | Upper bound on expected background events per nominal response window. |
| `noisy_retry_s` | `5.0` | Delay before another passive noise check. |
| `max_noisy_tries` | `6` | Consecutive failed checks before `NO_RESPONSE`. |
| `reject_bounce` | `true` | Reject successively shrinking response gaps. |
| `bounce_gap_ratio` | `0.7` | Each gap must shrink by more than 30% to count as a bounce pattern. |

`lambda_used = max(long_run_rate, pre_count / pre_listen_s, lambda_floor)` is
frozen at the completed pre-listen check. Every round probability uses that
rate and the full nominal window; every match also requires
`lambda_used * window_s <= mu_max`. With these defaults, even one pre-listen
knock exceeds the expected-noise limit and requests quiet.

`dialog.window_s` changes from 6.0 to **6.4 seconds**. A tired survivor starting
after 2.5 seconds and knocking three times at 0.8-second intervals needs about
6.1 seconds including the required two seconds of silence. The extra margin
allows the specified jitter. The probability calculation uses 6.4 seconds too;
`p_confirm` stays at 0.0001. Slow four-knock rounds may still time out and retry.

`prompter.fake_knock_interval_s` is **0.55**, with `fake_jitter_s` **0.08**.
The engine, classifier, and event APIs retain their existing signatures.

## Dashboard integration

- `PRE_LISTEN` means a challenge was accepted and the engine is checking for
  quiet; the challenge command returns no prompt actions until a later tick.
- `TOO_NOISY` emits a state event with `text: "Too noisy: call for quiet."`.
  The dashboard should display that text and show that the engine will retry.
- Existing snapshot keys remain. New fields are `noisy` (boolean), `mu`
  (expected background count), and `lambda_used_per_min` (the selected rate).
- Round records also include `noisy`, `closed_early`, `starts_promptly`,
  `bounce`, `mu`, and `lambda_used_per_min` so rejected answers are inspectable.
- A nonempty response that runs out of time is noisy and unmatched; it does
  not count as an empty window. `NO_RESPONSE` never establishes absence.
- Reset still removes rounds, answers, and approval, but keeps noise history.

No dashboard code or shared detection/audio contracts were changed by FIX-01.
The evaluator now counts accepted pre-listen challenges and handles their
deferred prompts and cooldown transitions.

## Human commands

Run from the repository root using Python 3.11. These commands use generated
audio and fakes, without touching a microphone, speaker, serial port, or Pi.

```sh
uv run pytest -q
uv run --extra ml pytest -q
uv run hb sim --people 2 --can-move yes --hurt no
uv run --extra ml hb sim --people 2 --can-move yes --hurt no
uv run --extra ml hb sim --no-survivor --noise
uv run pytest -q tests/test_noise_guards.py
uv run pytest -q tests/test_sim.py -k human_speed
```

The fast simulation with `--people 2 --can-move yes --hurt no` confirms at
**27.664 seconds** and completes all three answers at **57.456 seconds**, for
both the heuristic and actual head. The exact `hb sim --people 2` command
(default `hurt=yes`) also succeeds, finishing at **58.032 seconds**.
The tired fixture (0.8-second spacing, ±0.15-second jitter, 2.5-second start
delay) confirms at **31.120 seconds** and finishes the interview at **64.832
seconds**; its test allows 90 seconds for the longer interview. Both classifiers
complete the 600-second noise-only control with **zero confirmations and zero
errors**, without falling back to another classifier.

## Verification and parallel-lane status

Python 3.11.15 was used through `uv`, with only the project's dependencies.
The complete suite against committed baseline `4529c32` plus **only FIX-01**
changes passes:

- `uv run pytest -q`: **304 passed, 15 skipped** (optional ML coverage).
- `uv run --extra ml pytest -q`: **319 passed**, no skips.
- The FIX-01 dialog/statistics/noise-guard subset: **76 passed**.
- The simulation tests with actual ML extras: **30 passed**.

The isolated validation copy is `/tmp/heartbeat-fix01-scope-g2o78y7h`; it
contains the committed baseline plus exactly the owned source, tests, model,
and classifier/dialog/fake profile settings. The validation commands used
`UV_PROJECT_ENVIRONMENT=/tmp/heartbeat-fix01-scope-venv` and
`UV_CACHE_DIR=/tmp/heartbeat-p05-uv-cache` to keep the other lanes' environments
untouched. They ran offline; tests never accessed hardware or the network.

At the shared-workspace integration check, **411 tests passed and one failed**:
`tests/test_config.py::test_named_profile_and_reserved_sections` still expects
`profile.server == {}`, while P04's in-progress profile adds server settings.
That test and the server section are outside FIX-01 ownership and were left
untouched. P04 needs to update this obsolete assertion before the combined
working tree's full suite can pass. The FIX-01-only commit does not contain
P04 or P07 work.

## Reproduce the synthetic head

The existing segmentation and training tools are unchanged. With the local
YAMNet model from P05 and ML extras installed, this regenerates the recordings,
reviewable training report, and saved head without audio devices or network
access. The original fixed-spacing P05 recipe does not consume the new mixed
spacing keys; use this recipe for FIX-01.

```sh
uv run --extra ml python - <<'PY'
from pathlib import Path
import numpy as np
import soundfile as sf
from heartbeat import synth
from heartbeat.config import load_profile
from tools.segment import segment
from tools.train_head import train_index

cfg = load_profile()
s = cfg.classifier.training['synthetic']
root = Path('/tmp/heartbeat-fix01-data')
raw = root / 'raw'
raw.mkdir(parents=True, exist_ok=True)
rng = np.random.default_rng(cfg.classifier.training['random_seed'])
steady_count = round(s['recordings_per_kind'] * s['steady_fraction'])
tempos = np.linspace(s['interval_min_s'], s['interval_max_s'], steady_count)
for kind in s['kinds']:
    for recording in range(s['recordings_per_kind']):
        label = 'knock' if kind == 'knock' else 'other'
        surface = f'synthetic{recording % 3}'
        times = [s['first_event_s']]
        for _ in range(1, s['events_per_recording']):
            if recording < steady_count:
                gap = float(np.clip(
                    tempos[recording] + rng.uniform(-s['spacing_jitter_s'], s['spacing_jitter_s']),
                    s['interval_min_s'], s['interval_max_s']))
            else:
                gap = float(rng.uniform(s['interval_min_s'], s['interval_max_s']))
            times.append(times[-1] + gap)
        events = [(t, kind, float(rng.uniform(s['gain_min_db'], s['gain_max_db'])))
                  for t in times]
        audio, _ = synth.scene(
            times[-1] + s['tail_s'], events,
            bg_dbfs=float(rng.uniform(s['background_min_dbfs'], s['background_max_dbfs'])),
            seed=int(rng.integers(0, 2**31)), cfg=cfg.audio.synth)
        sf.write(raw / f'{label}__{surface}__{kind}__{recording:03}.wav',
                 audio, 16000, subtype='FLOAT')
segment(raw, root / 'clips', cfg)
report = train_index(
    root / 'clips' / 'index.csv', cfg.classifier,
    output='models/knock_head.joblib', report_path=root / 'classifier.md',
    provenance='SYNTHETIC ONLY: heartbeat.synth; steady human-tempo and irregular mixed '
               '0.3–1.2 s spacing, recording-disjoint fit/calibration/assessment; '
               'no real contact recordings')
print('threshold:', report['final_threshold'])
print('data hash:', report['training_data_hash'])
PY
```

The committed head and all acceptance fixtures are synthetic. Real contact
recordings, venue noise, hardware gating, serial operation, speaker playback,
and Raspberry Pi 5 speed remain unverified. The reported Poisson likelihoods
assume independent background events; they are neither a posterior probability
of a person nor a measured session-wide false-alarm rate.
