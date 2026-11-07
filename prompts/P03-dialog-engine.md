# P03: knock dialog engine, statistics, prompters, `hb run` and `hb sim`

Read `AGENTS.md` first. **Lane A, after P01 is committed.** P05 may be running in parallel and owns `heartbeat/classifiers/` and `tools/`. Don't touch those.

## Goal
The heart of the project. The sentinel challenges with knocks, counts the answers, works out how likely a match is to be chance, confirms a person, then interviews them with knock answers.

A simulated survivor proves the whole loop end to end with no hardware.

## Files you own
- `heartbeat/stats.py`, `dialog.py`, `prompter.py`
- `assets/voice/*.wav`
- `tools/make_voice.py`
- Wiring-only edits to `heartbeat/runner.py`, `heartbeat/cli.py`, and the `dialog` and `prompter` sections of `profiles/rubble.yaml`
- `tests/test_stats.py`, `tests/test_dialog.py`, `tests/test_sim.py`

## `stats.py`
- **`BackgroundRate`:**
  - Counts knock detections heard while the engine is **passively listening**. Exclude anything during prompts, gates or response windows.
  - Uses a sliding window of `rate_window_s` (600).
  - `λ = (count + prior_events) / (observed_s + prior_s)` with `prior_events = 1` and `prior_s = 60`. This starts conservative at 1 per minute and sharpens as quiet time accumulates. It never drops below `lambda_floor` (1/600 per s).
- **`round_p(expected k, heard n, window_s, lam)`** returns a probability, or None for no match.
  - A round matches if `|n - k| <= tol`, where `tol` is 0 when k ≤ 2 and 1 otherwise.
  - `p = P(k-d <= N <= k+d)` for `N ~ Poisson(lam * window_s)` and `d = |n - k|`.
- **`combine(ps)`** is the product of the round probabilities.
- **`format_odds(p)`** returns plain words: "1 in 40", "1 in 2,300", "1 in 2.3 million", or "1 in over a billion".
- **Docstring:** state the assumption (independent background events) and its limits honestly. Judges will ask about this.

## `dialog.py`: `DialogEngine(cfg, emit, rng_seed)`
Pure, as AGENTS.md requires.

**States:** `LISTENING → PROMPTING → RESPONSE_WINDOW → (next round | CONFIRMED | NO_RESPONSE)`. After `CONFIRMED`, the engine moves to `INTERVIEW` (one sub-step per question) and then back to `CONFIRMED`.

**LISTENING**
- Feeds `BackgroundRate`.
- A spontaneous burst (≥ 2 knocks within 4 s) sets status `POSSIBLE`.
- If `auto_challenge` is set, a challenge starts after 1.5 s of quiet following the burst.
- The operator command `challenge` also starts one.

**Rounds**
- Round 1 uses `first_pattern` (3) and emits `[PromptVoice("copy_knocks"), PromptKnocks(3, interval_s)]`.
- Later rounds pick `k` at random from `patterns` ([2, 3, 4]), never repeating the previous `k`, and emit `[PromptKnocks(k)]` only.
- **Varying counts is the point:** a periodic noise source can't track them.

**Response window**
- Opens at `on_prompts_finished(t)` and lasts `window_s` (6.0).
- If at least one knock has arrived, it closes early `early_close_s` (2.0) after the last knock.
- Only knocks where `is_knock and not gated` are counted.
- When the window closes, emit `round_result` with these fields: `round`, `expected`, `heard`, `knock_times`, `p` (or null), `matched`, `combined_p` and `odds`.

**CONFIRMED** requires all three:
- at least `min_matched_rounds` (2) matched rounds
- at least 2 *different* expected counts among the matched rounds
- `combined_p <= p_confirm` (1e-4)

**NO_RESPONSE** happens after `max_rounds` (4), or after 2 consecutive empty windows.
- Status stays `POSSIBLE` if any round matched; otherwise it is `NO_SIGNS`.
- Return to `LISTENING` after `cooldown_s` (5).

**Interview** (when `auto_interview` is set, or on the `interview` command)
- On `CONFIRMED`, first emit `PromptVoice("we_hear_you")`.
- Then ask each question:

  | Key | Answer |
  |---|---|
  | `q_people` | the number of knocks; 0 means "no answer" |
  | `q_move` | 2 means yes, 1 means no, anything else is "unclear" |
  | `q_hurt` | 2 means yes, 1 means no, anything else is "unclear" |

- Each question gets a window of `question_window_s` (10), closing early 3 s after the last knock.
- Emit an `answer` event with `question`, `heard` and `value`.
- **Never guess:** an unclear answer stays "unclear".

**Operator commands:** `challenge`, `interview`, `ask` (with `q=<key>`), `reassure`, `reset`, `approve` (with optional `operator=<initials>`).
- `approve` is valid only in `CONFIRMED`. It records `approved=True` and emits `approval`.
- An invalid command or state returns `[]` and emits `error`. Never raise.
- Keep names and arguments JSON-friendly. An LLM agent will call these as tools later.

**`snapshot()`** returns a dict with these fields:
- `state`, `status`
- `round`, `rounds` (a list)
- `combined_p`, `odds`
- `answers`, `approved`
- `lambda_per_min`, `profile`

Emit `state` on every change.

## `prompter.py`
`run(action)` blocks.
- **`ManualPrompter(bus)`:** prints `OPERATOR: knock 3 times now` and emits an `operator` event. It waits `n * interval + 0.5` s, and an estimated length for voice prompts.
- **`SpeakerPrompter(device)`:** plays `assets/voice/<key>.wav` with sounddevice, blocking. For `PromptKnocks` it plays `knock.wav` `n` times at `interval_s`, as an acoustic fallback.
- **`SerialKnocker(port)`:**
  - Uses pyserial. Sends `K n interval_ms` and waits for `OK K n`, with a timeout of `n * interval + 2` s.
  - On open it waits for `READY`, or about 2 s.
  - On failure it emits `error` and falls back to Manual for that action. It reconnects next time.
- **`CompositePrompter(voice, knocks)`.**
- **`make_prompter(spec)`:**
  - `manual`: Manual.
  - `speaker`: Speaker for both voice and knocks.
  - `/dev/...` or `COMx`: Speaker for voice, SerialKnocker for knocks.
  - `auto`: scan the serial ports for `PONG`; else use speaker; else manual.

## Runner wiring (`runner.py`, `cli.py`)
- **The engine lives on the processing thread.** `on_sound` and `on_block` call `engine.on_sound` and `engine.tick`.
- **Actions** go to one prompt worker thread, run in order:
  - Before the batch starts, call `detector.gate(t_now, inf)` and emit `prompt_start`.
  - After the batch, compute `t_end` (stream time) and set the gate end to `t_end + gate_tail_s` (0.6).
  - Call `engine.on_prompts_finished(t_end)` **on the processing thread**, using a queue message.
  - Emit `prompt_end`.
- **`hb run`:** `hb run [--device D | --wav PATH] [--knocker auto|manual|speaker|PORT] [--speaker-device D] [--auto] [--profile rubble]`
  - Prints a console transcript: state changes, rounds with odds, and answers.
  - Keys (a stdin thread, Enter after each): `c` = challenge, `i` = interview, `r` = reset, `a` = approve, `q` = quit.
- **`hb sim [--people 2] [--can-move yes] [--hurt yes] [--no-survivor] [--noise] [--realtime] [--seed 1]`:** a simulated survivor in the audio stream.
  - **Source:** a generator that streams synthetic background plus distractors. With `--noise`, add drops, footsteps and speech.
  - **Survivor:** a `FakePrompter` that, when asked for k knocks, injects k synthetic knocks into the stream about 1.0–1.5 s after the prompt ends, with human-like jitter of ±80 ms per knock. It answers interview questions from its script.
  - **Speed:** runs faster than real time by default. `--realtime` paces it so a human can watch it, and so P04's dashboard can show it.
  - Prints the transcript and exits 0 on the expected outcome.

## Voice assets
`tools/make_voice.py` generates 16 kHz mono WAVs into `assets/voice/`:

| Key | Text |
|---|---|
| `copy_knocks` | "Rescue team. If you can hear us, knock back the same number of times." |
| `we_hear_you` | "We hear you. Help is coming. Save your strength." |
| `q_people` | "Knock once for each person with you." |
| `q_move` | "Can you move? Knock two times for yes. One time for no." |
| `q_hurt` | "Are you hurt? Knock two times for yes. One time for no." |
| `reassure` | "We are still here. Knock if anything changes." |
| `knock` | a synthetic knock from `synth.py` |

- On macOS use `say` and `afconvert`; on Linux use `espeak-ng`.
- **Commit the WAVs**, so the Pi needs no text-to-speech.

## Tests
- **stats:**
  - Poisson values match a hand calculation.
  - `format_odds` cases.
  - With no quiet time observed, the prior gives 1 per minute.
- **dialog** (fake clock, scripted detections):
  - Correct answers to 3 then 2 reach `CONFIRMED` with `combined_p < 1e-4`.
  - A "dripping pipe" knock every 1.5 s for 5 minutes never confirms.
  - Silence leads to `NO_RESPONSE` with status `NO_SIGNS`.
  - Knocks inside gates are ignored.
  - Interview answers map correctly: 2 / 1 / other give yes / no / unclear.
  - An invalid command emits `error` and doesn't raise.
  - A high background rate (λ = 1 per s) prevents confirmation.
  - `approve` outside `CONFIRMED` is rejected.
- **sim:**
  - With `--people 2 --can-move yes --hurt no`, the run confirms within 60 s of stream time and the answers match.
  - `--no-survivor --noise` never confirms over 10 minutes of stream time.
  - Every emitted event is JSON-serializable.

## Done when
Tests pass, and these work:
- `uv run hb sim --people 2` prints a clean transcript ending in `CONFIRMED`, the odds, and `people=2`.
- `uv run hb sim --no-survivor --noise` ends without a confirmation.

Commit `P03: dialog engine, stats, prompters, hb run/sim`. Report, and include one sample transcript.
