from copy import deepcopy
from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from heartbeat.config import load_profile
from heartbeat.dialog import DialogConfig, DialogEngine, PromptKnocks, PromptVoice
from heartbeat.stats import round_p


def make_engine(*, seed=1, full_profile=False, **overrides):
    profile = load_profile()
    values = dict(profile.dialog)
    values.update(auto_interview=False)
    values.update(overrides)
    events = []
    cfg = replace(profile, dialog=values) if full_profile else values
    engine = DialogEngine(cfg, lambda kind, t, data: events.append({"type": kind, "t": t, "data": data}), seed)
    return engine, events


def sound(t, *, knock=True, gated=False):
    return SimpleNamespace(t=t, is_knock=knock, gated=gated)


def start_challenge(engine, start=0):
    assert engine.command("challenge", start) == []
    assert engine.state == "PRE_LISTEN"
    t = start + engine.cfg.pre_listen_s
    assert engine.tick(t) == [PromptVoice("copy_knocks"), PromptKnocks(3, engine.cfg.interval_s)]
    return t


def answer_round(engine, start, heard=None):
    """Complete an already-returned prompt, then deliver timestamped answers."""
    if engine.state == "PRE_LISTEN":
        start = max(start, engine._pre_started + engine.cfg.pre_listen_s)
        assert engine.tick(start)
    engine.on_prompts_finished(start)
    expected = engine._expected
    count = expected if heard is None else heard
    times = [start + 1 + index * 0.4 for index in range(count)]
    for t in times:
        assert engine.on_sound(sound(t)) == []
    end = min(start + engine.cfg.window_s, times[-1] + engine.cfg.early_close_s) if times else start + engine.cfg.window_s
    actions = engine.tick(end)
    return end, actions


def confirm(engine, start=0):
    start = start_challenge(engine, start)
    t, actions = answer_round(engine, start + 1, 3)
    assert actions == [] and engine.state == "PRE_LISTEN"
    t += engine.cfg.pre_listen_s
    assert engine.tick(t) == [PromptKnocks(2, engine.cfg.interval_s)]
    t, actions = answer_round(engine, t + 1, 2)
    return t, actions


def test_correct_three_then_two_confirms_with_tiny_combined_probability():
    engine, events = make_engine()
    _, actions = confirm(engine)
    snapshot = engine.snapshot()
    assert snapshot["state"] == snapshot["status"] == "CONFIRMED"
    assert snapshot["combined_p"] < 1e-4
    assert [record["expected"] for record in snapshot["rounds"]] == [3, 2]
    assert actions == [PromptVoice("we_hear_you")]
    assert len([event for event in events if event["type"] == "round_result"]) == 2


def test_first_pattern_and_later_patterns_are_seeded_and_never_repeat():
    counts = []
    for _ in range(2):
        engine, _ = make_engine(max_empty_windows=10, max_rounds=8)
        start_challenge(engine)
        sequence = [engine._expected]
        t = engine._clock + 1
        for _ in range(7):
            t, _ = answer_round(engine, t, heard=0)
            t += engine.cfg.pre_listen_s
            assert engine.tick(t)
            sequence.append(engine._expected)
            t += 1
        counts.append(sequence)
    assert counts[0] == counts[1]
    assert counts[0][0] == 3
    assert all(left != right for left, right in zip(counts[0], counts[0][1:]))


def test_confirmation_requires_distinct_expected_counts_even_if_rounds_match():
    engine, _ = make_engine(min_matched_rounds=1)
    engine.command("challenge", 0)
    _, actions = answer_round(engine, 1, heard=3)
    assert engine.status == "POSSIBLE"
    assert engine.state == "PRE_LISTEN" and actions == []


def test_two_empty_windows_lead_to_no_response_then_passive_cooldown():
    engine, events = make_engine()
    engine.command("challenge", 0)
    t, _ = answer_round(engine, 1, heard=0)
    t, actions = answer_round(engine, t + 1, heard=0)
    assert actions == []
    assert engine.state == "NO_RESPONSE" and engine.status == "NO_SIGNS"
    observed = engine.background.observed_s
    assert observed == 2 * engine.cfg.pre_listen_s
    engine.tick(t + engine.cfg.cooldown_s + 2)
    assert engine.state == "LISTENING"
    assert engine.background.observed_s == observed + engine.cfg.cooldown_s + 2
    assert all(event["data"].get("status") in (None, "NO_SIGNS", "POSSIBLE", "CONFIRMED") for event in events)


def test_failed_challenge_retains_possible_if_any_round_matched():
    engine, _ = make_engine(max_rounds=3)
    engine.command("challenge", 0)
    t, _ = answer_round(engine, 1, heard=3)
    t, _ = answer_round(engine, t + 1, heard=0)
    _, _ = answer_round(engine, t + 1, heard=0)
    assert engine.state == "NO_RESPONSE" and engine.status == "POSSIBLE"


def test_gated_rejected_and_outside_window_detections_do_not_count():
    engine, _ = make_engine()
    start = start_challenge(engine, 1)
    engine.on_sound(sound(start + 0.5))  # Still prompting.
    engine.on_prompts_finished(start + 1)
    engine.on_sound(sound(start + 0.9))  # Delayed prompt-time onset.
    engine.on_sound(sound(start + 1.2, gated=True))
    engine.on_sound(sound(start + 1.3, knock=False))
    engine.on_sound(sound(start + 2))
    engine.tick(start + 4)
    assert engine.rounds[0]["heard"] == 1
    assert engine.rounds[0]["knock_times"] == [start + 2]


def test_response_early_close_uses_last_knock_and_nominal_probability_duration():
    engine, _ = make_engine()
    start = start_challenge(engine)
    used = engine.snapshot()["lambda_used_per_min"] / 60
    engine.on_prompts_finished(start + 1)
    times = [start + value for value in (2, 2.5, 3)]
    for t in times:
        engine.on_sound(sound(t))
    assert engine.tick(start + 4.99) == []
    engine.tick(start + 5)
    assert engine.rounds[0]["knock_times"] == times
    assert engine.rounds[0]["p"] == pytest.approx(round_p(3, 3, engine.cfg.window_s, used))


def test_later_detection_cannot_extend_a_window_past_existing_quiet_deadline():
    engine, _ = make_engine()
    start = start_challenge(engine)
    engine.on_prompts_finished(start + 1)
    engine.on_sound(sound(start + 2))
    actions = engine.on_sound(sound(start + 4.1))
    assert actions == [] and engine.state == "PRE_LISTEN"
    assert engine.rounds[0]["knock_times"] == [start + 2]
    assert engine.background.count == 1  # The later sound belongs to pre-listening.


def test_watermark_and_delayed_detections_do_not_lose_in_window_answers():
    engine, _ = make_engine()
    start = start_challenge(engine)
    engine.on_prompts_finished(start + 2)
    assert engine.tick(start + 1.125) == []  # Prompt completion used a newer actual clock.
    for t in (start + 3, start + 3.6, start + 4.2):
        engine.on_sound(sound(t))
    assert engine.tick(start + 6.199) == []
    engine.tick(start + 6.2)
    assert engine.rounds[0]["heard"] == 3
    assert engine.rounds[0]["matched"]


def test_passive_rate_excludes_prompts_response_windows_and_gate_tail():
    engine, _ = make_engine(full_profile=True)
    engine.tick(5)
    engine.on_sound(sound(1))  # Delayed, historical passive observation.
    engine.command("reassure", 5)
    engine.on_prompts_finished(7)
    engine.tick(10)
    assert engine.background.count == 1
    assert engine.background.observed_s == pytest.approx(5 + 3 - engine.gate_tail_s)
    start = start_challenge(engine, 10)
    engine.on_prompts_finished(start + 2)
    engine.on_sound(sound(start + 3))
    engine.tick(start + 5)
    assert engine.background.count == 1
    assert engine.background.observed_s == pytest.approx(8 - engine.gate_tail_s + engine.cfg.pre_listen_s)


def test_external_open_gate_survives_reset_then_closes_for_passive_exposure():
    engine, _ = make_engine()
    engine.tick(2)
    engine.gate_until(float("inf"))
    engine.command("reset", 3)
    engine.tick(5)
    assert engine.background.observed_s == 2
    engine.gate_until(6)
    engine.tick(8)
    assert engine.background.observed_s == 4


def test_spontaneous_burst_becomes_possible_and_auto_challenges_after_quiet():
    engine, _ = make_engine(auto_challenge=True)
    engine.on_sound(sound(1))
    engine.on_sound(sound(2))
    assert engine.status == "POSSIBLE" and engine.state == "LISTENING"
    assert engine.tick(3.49) == []
    actions = engine.tick(3.5)
    assert actions == [] and engine.state == "PRE_LISTEN"
    actions = engine.tick(3.5 + engine.cfg.pre_listen_s)
    assert actions == [PromptVoice("copy_knocks"), PromptKnocks(3, engine.cfg.interval_s)]
    assert engine.background.count == 0
    assert engine.snapshot()["candidate_times"] == [1, 2]
    assert engine.background.observed_s == 3.5 + engine.cfg.pre_listen_s


def test_dripping_pipe_for_five_minutes_never_confirms_and_calls_for_quiet():
    engine, events = make_engine(auto_challenge=True)
    pending_finish = None
    for index in range(3001):
        t = index / 10
        if pending_finish is not None and t >= pending_finish:
            engine.on_prompts_finished(t)
            pending_finish = None
        if index % 15 == 0:
            actions = engine.on_sound(sound(t))
            if actions:
                pending_finish = t + 0.5
        if index == 100:
            actions = engine.command("challenge", t)
            if actions:
                pending_finish = t + 0.5
        actions = engine.tick(t)
        if actions:
            pending_finish = t + 0.5
        assert engine.status != "CONFIRMED"
    assert any(event["type"] == "state" and event["data"]["state"] == "TOO_NOISY" for event in events)
    assert not any(event["type"] == "window_open" for event in events)


def test_high_background_rate_blocks_challenge_before_prompting():
    engine, _ = make_engine(prior_events=60, prior_s=60)
    engine.command("challenge", 0)
    assert engine.tick(engine.cfg.pre_listen_s) == []
    assert engine.state == "TOO_NOISY"
    assert engine.status != "CONFIRMED"
    assert engine.combined_p > engine.cfg.p_confirm
    assert engine.snapshot()["mu"] > engine.cfg.mu_max


def test_persistently_high_rate_stops_after_bounded_noisy_retries():
    engine, _ = make_engine(prior_events=60, prior_s=60)
    engine.command("challenge", 0)
    t = engine.cfg.pre_listen_s
    for attempt in range(engine.cfg.max_noisy_tries):
        assert engine.tick(t) == []
        if attempt + 1 < engine.cfg.max_noisy_tries:
            t += engine.cfg.noisy_retry_s
            engine.tick(t)
            t += engine.cfg.pre_listen_s
    assert engine.state == "NO_RESPONSE" and engine.status == "NO_SIGNS"
    assert engine.rounds == []


def test_auto_interview_maps_people_yes_and_no_and_returns_confirmed():
    engine, events = make_engine(auto_interview=True)
    t, actions = confirm(engine)
    assert engine.state == "INTERVIEW" and engine.status == "CONFIRMED"
    assert actions == [PromptVoice("we_hear_you"), PromptVoice("q_people")]
    for key, count in (("q_people", 2), ("q_move", 2), ("q_hurt", 1)):
        t += 1
        engine.on_prompts_finished(t)
        for index in range(count):
            engine.on_sound(sound(t + 1 + index * 0.4))
        t += 1 + (count - 1) * 0.4 + engine.cfg.question_early_close_s
        actions = engine.tick(t)
    assert actions == [] and engine.state == "CONFIRMED"
    assert engine.answers == {"q_people": 2, "q_move": "yes", "q_hurt": "no"}
    assert [event["data"]["question"] for event in events if event["type"] == "answer"] == ["q_people", "q_move", "q_hurt"]


@pytest.mark.parametrize("heard,value", [(0, "unclear"), (1, "no"), (2, "yes"), (3, "unclear")])
def test_manual_ask_never_guesses_a_boolean_answer(heard, value):
    engine, _ = make_engine()
    t, _ = confirm(engine)
    engine.on_prompts_finished(t + 1)
    assert engine.command("ask", t + 2, q="q_move") == [PromptVoice("q_move")]
    start = t + 3
    engine.on_prompts_finished(start)
    for index in range(heard):
        engine.on_sound(sound(start + 1 + index * 0.4))
    engine.tick(start + engine.cfg.question_window_s)
    assert engine.answers["q_move"] == value


def test_people_zero_means_no_answer():
    engine, _ = make_engine()
    t, _ = confirm(engine)
    engine.on_prompts_finished(t + 1)
    engine.command("ask", t + 2, q="q_people")
    engine.on_prompts_finished(t + 3)
    engine.tick(t + 3 + engine.cfg.question_window_s)
    assert engine.answers["q_people"] == "no answer"


def test_approval_is_only_valid_confirmed_and_records_operator():
    engine, events = make_engine()
    assert engine.command("approve", 0, operator="AB") == []
    assert events[-1]["type"] == "error" and not engine.approved
    t, _ = confirm(engine)
    engine.command("approve", t, operator="AB")
    assert engine.approved
    assert any(event["type"] == "approval" and event["data"]["operator"] == "AB" for event in events)


@pytest.mark.parametrize("command,args", [
    ("unknown", {}), ("ask", {"q": "q_move"}), ("interview", {}),
    ("challenge", {"unexpected": 1}), ("approve", {"operator": []}),
])
def test_invalid_commands_and_states_emit_error_without_raising(command, args):
    engine, events = make_engine()
    assert engine.command(command, 0, **args) == []
    assert events[-1]["type"] == "error"


def test_reset_restores_search_and_ignores_earlier_detections():
    engine, _ = make_engine()
    t, _ = confirm(engine)
    engine.command("approve", t)
    background = engine.background
    engine.command("reset", t + 1)
    engine.on_sound(sound(t))
    snapshot = engine.snapshot()
    assert snapshot["state"] == "LISTENING" and snapshot["status"] == "NO_SIGNS"
    assert snapshot["rounds"] == [] and snapshot["answers"] == {} and not snapshot["approved"]
    assert engine.background is background
    assert snapshot["lambda_per_min"] == pytest.approx(background.lambda_per_s * 60)


def test_snapshot_and_events_are_json_serializable_and_do_not_expose_mutable_state():
    engine, events = make_engine()
    confirm(engine)
    snapshot = engine.snapshot()
    assert set(snapshot) == {"state", "status", "round", "rounds", "combined_p", "odds",
                             "answers", "approved", "lambda_per_min", "profile",
                             "noisy", "mu", "lambda_used_per_min", "candidate", "candidate_times", "coaching"}
    json.dumps(events, allow_nan=False)
    json.dumps(snapshot, allow_nan=False)
    snapshot["rounds"][0]["knock_times"].append(999)
    assert 999 not in engine.rounds[0]["knock_times"]


def test_constructor_is_pure_with_complete_dict_and_rejects_unknown_or_missing_fields():
    cfg = deepcopy(load_profile().dialog)
    parsed = DialogConfig.parse(cfg)
    DialogEngine(parsed, lambda *_: None, 1)
    with pytest.raises(ValueError, match="Unknown dialog"):
        DialogEngine({**cfg, "misspelled": 1}, lambda *_: None, 1)
    cfg.pop("window_s")
    with pytest.raises(ValueError, match="Missing dialog"):
        DialogEngine(cfg, lambda *_: None, 1)


def test_announce_count_speaks_the_expected_count_after_each_pattern():
    engine, _ = make_engine(announce_count=True)
    assert engine.command("challenge", 0) == []
    t = engine.cfg.pre_listen_s
    assert engine.tick(t) == [PromptVoice("copy_knocks"), PromptKnocks(3, engine.cfg.interval_s),
                              PromptVoice("count_3")]
    t, actions = answer_round(engine, t + 1, 1)
    t += engine.cfg.pre_listen_s
    later = engine.tick(t)
    assert later == [PromptKnocks(engine._expected, engine.cfg.interval_s), PromptVoice(f"count_{engine._expected}")]


def test_announce_count_rejects_counts_without_a_voice():
    with pytest.raises(ValueError, match="announce_count"):
        DialogConfig.parse({**load_profile().dialog, "announce_count": True, "patterns": [3, 6]})


def test_announce_count_is_optional_and_off_by_default():
    values = dict(load_profile().dialog)
    values.pop("announce_count", None)
    assert DialogConfig.parse(values).announce_count is False


def test_one_required_match_confirms_after_a_single_unlikely_round():
    engine, _ = make_engine(min_matched_rounds=1, p_confirm=0.001, patterns=[3, 4, 5])
    start = start_challenge(engine)
    t, actions = answer_round(engine, start + 1, 3)
    assert engine.rounds[-1]["matched"] and engine.rounds[-1]["p"] <= 0.001
    assert engine.status == "CONFIRMED"


def test_one_required_match_still_needs_the_probability_ceiling():
    engine, _ = make_engine(min_matched_rounds=1, p_confirm=1e-9, patterns=[3, 4, 5])
    start = start_challenge(engine)
    answer_round(engine, start + 1, 3)
    assert engine.rounds[-1]["matched"] and engine.status != "CONFIRMED"
