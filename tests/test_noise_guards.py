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
    for t in (1, 2, 3, 4):
        knock(engine, t)
    before = (engine.background.count, engine.background.observed_s, engine.background.lambda_per_s)
    engine.command("reset", 4)
    after = (engine.background.count, engine.background.observed_s, engine.background.lambda_per_s)
    assert after == before
    assert engine.snapshot()["rounds"] == [] and engine.snapshot()["answers"] == {}


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
