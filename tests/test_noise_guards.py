"""Adversarial accepted events: no classifier, microphone, network, or sleeps."""

from types import SimpleNamespace

import numpy as np
import pytest

from heartbeat.classify import Detection
from heartbeat.config import load_profile
from heartbeat.dialog import DialogEngine, PromptKnocks, PromptVoice
from heartbeat.stats import round_p
from tools.eval_background import simulate_dialog_events


def make_engine(**overrides):
    values = {**load_profile().dialog, "auto_interview": False, **overrides}
    events = []
    engine = DialogEngine(values, lambda kind, t, data: events.append((kind, t, data)), 1)
    return engine, events


def knock(engine, t, **values):
    return engine.on_sound(SimpleNamespace(t=t, is_knock=True, gated=False, **values))


def open_window(engine):
    engine.command("challenge", 0)
    start = engine.cfg.pre_listen_s
    assert engine.tick(start) == [PromptVoice("copy_knocks"), PromptKnocks(3, engine.cfg.interval_s)]
    engine.on_prompts_finished(start + 1)
    return start + 1


@pytest.mark.parametrize("period", [0.7, 1.0, 1.5, 2.5, 3.0, 5.0])
def test_periodic_accepted_knocks_never_confirm_for_five_minutes(period):
    profile = load_profile()
    events = [Detection(
        t=float(t), peak=0.8, snr_db=30.0, gated=False,
        clip=np.empty(0, dtype=np.float32), score=1.0, is_knock=True,
        classifier="periodic-stress", top=[["knock", 1.0]],
    ) for t in np.arange(period, 300, period)]
    result = simulate_dialog_events(events, 300, profile)
    assert result["false_confirmations"] == 0
    assert result["challenge_attempts"] == 5


def test_two_prelisten_knocks_block_prompt_then_retry_after_configured_delay():
    engine, events = make_engine()
    assert engine.command("challenge", 0) == []
    knock(engine, 1)
    knock(engine, 2)
    end = engine.cfg.pre_listen_s
    assert engine.tick(end) == []
    assert engine.state == "TOO_NOISY" and engine.round == 0
    state = events[-1][2]
    assert state["text"] == "Too noisy: call for quiet."
    assert state["noisy"] and state["mu"] > engine.cfg.mu_max
    assert state["lambda_used_per_min"] == pytest.approx(2 / end * 60)
    retry = end + engine.cfg.noisy_retry_s
    assert engine.tick(retry - 0.01) == [] and engine.state == "TOO_NOISY"
    assert engine.tick(retry) == [] and engine.state == "PRE_LISTEN"
    assert engine.tick(retry + engine.cfg.pre_listen_s)
    assert engine.state == "PROMPTING" and not engine.snapshot()["noisy"]


def test_one_prelisten_knock_can_independently_fail_expected_noise_limit():
    engine, _ = make_engine()
    engine.command("challenge", 0)
    knock(engine, 1)
    engine.tick(engine.cfg.pre_listen_s)
    assert engine.state == "TOO_NOISY"
    assert engine.background.count < engine.cfg.max_pre_knocks
    assert engine.snapshot()["mu"] > engine.cfg.mu_max


def test_each_round_uses_frozen_prelisten_rate_for_poisson_probability():
    engine, _ = make_engine(mu_max=3)
    engine.command("challenge", 0)
    knock(engine, 1)
    end = engine.cfg.pre_listen_s
    assert engine.tick(end)
    used = 1 / engine.cfg.pre_listen_s
    assert engine.background.lambda_per_s < used
    engine.on_prompts_finished(end + 1)
    for t in (end + 2, end + 2.5, end + 3):
        knock(engine, t)
    engine.tick(end + 3 + engine.cfg.early_close_s)
    record = engine.rounds[0]
    assert record["matched"]
    assert record["lambda_used_per_min"] == pytest.approx(used * 60)
    assert record["mu"] == pytest.approx(used * engine.cfg.window_s)
    assert record["p"] == pytest.approx(round_p(3, 3, engine.cfg.window_s, used))
    assert engine.state == "PRE_LISTEN"  # The next round also checks for quiet.


def test_continuing_knocks_time_out_as_noisy_and_never_count_as_empty():
    engine, _ = make_engine(max_rounds=1)
    start = open_window(engine)
    for offset in (1, 2.5, 4, 5.5):
        knock(engine, start + offset)
    assert engine.tick(start + engine.cfg.window_s) == []
    record = engine.rounds[0]
    assert record["heard"] == 4 and record["p"] is not None
    assert record["noisy"] and not record["closed_early"] and not record["matched"]
    assert engine._empty_windows == 0 and engine.combined_p == 1
    assert engine.snapshot()["noisy"] and engine.state == "NO_RESPONSE"


def test_silence_completed_exactly_at_deadline_does_not_close_early():
    engine, _ = make_engine()
    start = open_window(engine)
    final = start + engine.cfg.window_s - engine.cfg.early_close_s
    for t in (final - 1, final - 0.5, final):
        knock(engine, t)
    engine.tick(start + engine.cfg.window_s)
    assert engine.rounds[0]["noisy"] and not engine.rounds[0]["matched"]


def test_late_first_knock_cannot_match_despite_correct_count_and_early_silence():
    engine, _ = make_engine(response_start_max_s=1)
    start = open_window(engine)
    for offset in (2, 2.4, 2.8):
        knock(engine, start + offset)
    engine.tick(start + 2.8 + engine.cfg.early_close_s)
    record = engine.rounds[0]
    assert record["heard"] == 3 and record["closed_early"]
    assert not record["starts_promptly"] and not record["matched"]


@pytest.mark.parametrize("offsets,bounce", [([1, 1.8, 2.2], True), ([1, 1.8, 2.5], False)])
def test_shrinking_bounce_gaps_are_rejected_but_tired_human_timing_is_allowed(offsets, bounce):
    engine, _ = make_engine()
    start = open_window(engine)
    for offset in offsets:
        knock(engine, start + offset)
    engine.tick(start + offsets[-1] + engine.cfg.early_close_s)
    assert engine.rounds[0]["bounce"] is bounce
    assert engine.rounds[0]["matched"] is not bounce


def test_reset_preserves_noise_count_exposure_and_rate():
    engine, _ = make_engine()
    for t in (1, 6, 11, 16):  # Isolated events remain background rather than candidates.
        knock(engine, t)
    before = (engine.background.count, engine.background.observed_s, engine.background.lambda_per_s)
    assert before[0] == 4
    engine.command("reset", 16)
    after = (engine.background.count, engine.background.observed_s, engine.background.lambda_per_s)
    assert after == before
    assert engine.snapshot()["rounds"] == [] and engine.snapshot()["answers"] == {}


def test_twenty_two_spontaneous_knocks_in_twenty_one_seconds_do_not_block_challenge():
    engine, events = make_engine()
    for t in range(22):
        knock(engine, t)
    assert engine.status == "POSSIBLE" and engine.background.count == 0
    marked = {t for kind, _, data in events if kind == "state" and data["candidate"]
              for t in data["candidate_times"]}
    assert marked == set(range(22))  # Includes the first, provisionally isolated hit.
    assert not any(kind == "sound" for kind, _, _ in events)  # Existing sound log is annotated once.
    assert engine.command("challenge", 21) == []
    assert engine.tick(21 + engine.cfg.pre_listen_s) == [
        PromptVoice("copy_knocks"), PromptKnocks(3, engine.cfg.interval_s),
    ]
    assert engine.snapshot()["mu"] <= engine.cfg.mu_max
    assert not engine.snapshot()["noisy"]


def test_candidate_reclassification_keeps_isolated_knocks_in_noise_memory():
    engine, events = make_engine()
    knock(engine, 1)
    assert engine.background.count == 1 and not engine.snapshot()["candidate"]
    knock(engine, 2)
    assert engine.background.count == 0 and engine.snapshot()["candidate_times"] == [1, 2]
    assert events[-1][2]["candidate"]
    for t in (10, 20):
        knock(engine, t)
    assert engine.background.count == 2
    engine.command("challenge", 20)
    engine.tick(20 + engine.cfg.pre_listen_s)
    assert engine.snapshot()["lambda_used_per_min"] == pytest.approx(3 / (83) * 60)


def test_delayed_search_clip_can_complete_a_candidate_burst_after_challenge_command():
    engine, events = make_engine()
    knock(engine, 1)
    assert engine.background.count == 1
    engine.command("challenge", 2.5)
    knock(engine, 2)  # Clip lookahead delivered this historical onset after the command.
    assert engine.background.count == 0 and engine.status == "POSSIBLE"
    assert engine.snapshot()["candidate_times"] == [1, 2]
    assert engine._pre_times == []
    assert events[-1][2]["state"] == "PRE_LISTEN" and events[-1][2]["candidate"]
    assert engine.tick(2.5 + engine.cfg.pre_listen_s) == [
        PromptVoice("copy_knocks"), PromptKnocks(3, engine.cfg.interval_s),
    ]


@pytest.mark.parametrize("state", ["LISTENING", "PRE_LISTEN", "TOO_NOISY", "NO_RESPONSE"])
def test_all_quiet_resets_noise_and_starts_normal_guarded_challenge_from_idle_search(state):
    engine, events = make_engine(burst_knocks=100, max_noisy_tries=1 if state == "NO_RESPONSE" else 6)
    for t in range(22):
        knock(engine, t)
    assert engine.background.count == 22
    if state != "LISTENING":
        engine.command("challenge", 21)
    if state in {"TOO_NOISY", "NO_RESPONSE"}:
        engine.tick(21 + engine.cfg.pre_listen_s)
    assert engine.state == state
    t = engine._clock
    assert engine.command("all_quiet", t, operator="AB") == []
    assert engine.state == "PRE_LISTEN" and engine.round == 0
    assert engine.background.count == 0 and engine.background.observed_s == 0
    assert engine.snapshot()["lambda_per_min"] == 1
    operator = [data for kind, _, data in events if kind == "operator"][-1]
    assert operator == {"command": "all_quiet", "operator": "AB", "challenge": True}
    knock(engine, t - 1)  # A delayed detection from before the declaration stays historical.
    assert engine.background.count == 0
    assert engine.tick(t + engine.cfg.pre_listen_s) == [
        PromptVoice("copy_knocks"), PromptKnocks(3, engine.cfg.interval_s),
    ]


def test_all_quiet_can_record_a_declaration_without_starting_challenge():
    engine, events = make_engine()
    knock(engine, 1)
    assert engine.command("all_quiet", 1, challenge=False) == []
    assert engine.state == "LISTENING" and engine.background.count == 0
    assert engine.snapshot()["lambda_per_min"] == 1
    assert engine.tick(1 + engine.cfg.pre_listen_s) == []
    assert any(kind == "operator" and data["command"] == "all_quiet" and data["operator"] is None
               and data["challenge"] is False for kind, _, data in events)


@pytest.mark.parametrize("args", [{"operator": []}, {"challenge": 1}, {"challenge": "yes"}, {"other": True}])
def test_invalid_all_quiet_arguments_leave_noise_history_untouched(args):
    engine, events = make_engine()
    knock(engine, 1)
    assert engine.command("all_quiet", 1, **args) == []
    assert events[-1][0] == "error" and engine.background.count == 1
    assert engine.state == "LISTENING"


def test_all_quiet_cannot_interrupt_an_active_prompt_or_bypass_a_response_window():
    engine, events = make_engine()
    engine.command("challenge", 0)
    engine.tick(engine.cfg.pre_listen_s)
    engine.command("all_quiet", engine.cfg.pre_listen_s)
    assert events[-1][0] == "error" and engine.state == "PROMPTING"
    engine.on_prompts_finished(engine.cfg.pre_listen_s + 1)
    engine.command("all_quiet", engine.cfg.pre_listen_s + 1)
    assert events[-1][0] == "error" and engine.state == "RESPONSE_WINDOW"


def test_prelisten_coaches_a_candidate_then_confirms_once_they_comply():
    engine, events = make_engine()
    for t in (1, 2):
        knock(engine, t)
    engine.command("challenge", 3)
    for t in (4, 5):
        knock(engine, t)
    closed = 3 + engine.cfg.pre_listen_s
    assert engine.tick(closed) == [PromptVoice("stop_and_listen")]
    assert engine.round == 0 and engine.snapshot()["coaching"]
    assert events[-1][2]["text"] == "Hearing knocks: asking them to listen."
    engine.on_sound(SimpleNamespace(t=closed + 0.5, is_knock=True, gated=True))
    engine.on_prompts_finished(closed + 1)
    assert not engine.snapshot()["coaching"]
    retry = closed + engine.cfg.noisy_retry_s
    assert engine.tick(retry) == [] and engine.state == "PRE_LISTEN"
    prompt = retry + engine.cfg.pre_listen_s
    assert engine.tick(prompt) == [PromptVoice("copy_knocks"), PromptKnocks(3, engine.cfg.interval_s)]
    for expected in (3, 2):
        engine.on_prompts_finished(prompt + 1)
        answers = [prompt + 2 + index * 0.55 for index in range(expected)]
        for t in answers:
            knock(engine, t)
        close = answers[-1] + engine.cfg.early_close_s
        actions = engine.tick(close)
        if expected == 3:
            assert actions == [] and engine.state == "PRE_LISTEN"
            prompt = close + engine.cfg.pre_listen_s
            assert engine.tick(prompt) == [PromptKnocks(2, engine.cfg.interval_s)]
    assert engine.status == "CONFIRMED" and engine.combined_p <= engine.cfg.p_confirm
    assert all(record["closed_early"] and record["matched"] for record in engine.rounds)


def test_repeated_candidate_prelisten_noise_is_still_bounded_by_max_noisy_tries():
    engine, _ = make_engine(max_noisy_tries=2)
    for t in (1, 2):
        knock(engine, t)
    engine.command("challenge", 3)
    for t in (4, 5):
        knock(engine, t)
    end = 3 + engine.cfg.pre_listen_s
    assert engine.tick(end) == [PromptVoice("stop_and_listen")]
    engine.on_prompts_finished(end + 1)
    retry = end + engine.cfg.noisy_retry_s
    engine.tick(retry)
    for t in (retry + 1, retry + 2):
        knock(engine, t)
    assert engine.tick(retry + engine.cfg.pre_listen_s) == []
    assert engine.state == "NO_RESPONSE" and engine.status != "CONFIRMED"


def test_cooldown_knocks_update_noise_memory_while_response_knocks_do_not():
    engine, _ = make_engine(max_rounds=1)
    start = open_window(engine)
    knock(engine, start + 1)
    closed = start + 1 + engine.cfg.early_close_s
    engine.tick(closed)
    assert engine.state == "NO_RESPONSE" and engine.background.count == 0
    observed = engine.background.observed_s
    knock(engine, closed + 1)
    assert engine.background.count == 1
    assert engine.background.observed_s == observed + 1


def test_prelisten_waits_for_an_external_prompt_gate_to_finish():
    engine, _ = make_engine()
    engine.gate_until(float("inf"))
    engine.command("challenge", 0)
    assert engine.tick(10) == [] and engine.state == "PRE_LISTEN"
    engine.gate_until(11)
    assert engine.tick(11 + engine.cfg.pre_listen_s - 0.01) == []
    assert engine.tick(11 + engine.cfg.pre_listen_s)
    assert engine.background.observed_s == engine.cfg.pre_listen_s


@pytest.mark.parametrize("key,value", [
    ("pre_listen_s", 0), ("mu_max", float("nan")), ("response_start_max_s", -1),
    ("require_silence_after", 1), ("reject_bounce", "yes"), ("bounce_gap_ratio", 1),
    ("max_pre_knocks", 0), ("max_noisy_tries", 1.5), ("noisy_retry_s", 0),
])
def test_invalid_noise_guard_config_is_rejected(key, value):
    with pytest.raises(ValueError, match=key):
        make_engine(**{key: value})
