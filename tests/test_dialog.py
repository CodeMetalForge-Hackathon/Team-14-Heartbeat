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


def answer_round(engine, start, heard=None):
    """Complete an already-returned prompt, then deliver timestamped answers."""
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
    actions = engine.command("challenge", start)
    assert actions == [PromptVoice("copy_knocks"), PromptKnocks(3, engine.cfg.interval_s)]
    t, actions = answer_round(engine, start + 1, 3)
    assert actions == [PromptKnocks(2, engine.cfg.interval_s)]
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
        engine.command("challenge", 0)
        sequence = [engine._expected]
        t = 1
        for _ in range(7):
            t, _ = answer_round(engine, t, heard=0)
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
    assert engine.state == "PROMPTING" and isinstance(actions[0], PromptKnocks)


def test_two_empty_windows_lead_to_no_response_then_passive_cooldown():
    engine, events = make_engine()
    engine.command("challenge", 0)
    t, _ = answer_round(engine, 1, heard=0)
    t, actions = answer_round(engine, t + 1, heard=0)
    assert actions == []
    assert engine.state == "NO_RESPONSE" and engine.status == "NO_SIGNS"
    assert engine.background.observed_s == 0
    engine.tick(t + engine.cfg.cooldown_s + 2)
    assert engine.state == "LISTENING"
    assert engine.background.observed_s == 2
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
    engine.command("challenge", 1)
    engine.on_sound(sound(1.5))  # Still prompting.
    engine.on_prompts_finished(2)
    engine.on_sound(sound(1.9))  # Delayed prompt-time onset.
    engine.on_sound(sound(2.2, gated=True))
    engine.on_sound(sound(2.3, knock=False))
    engine.on_sound(sound(3))
    engine.tick(5)
    assert engine.rounds[0]["heard"] == 1
    assert engine.rounds[0]["knock_times"] == [3]


def test_response_early_close_uses_last_knock_and_nominal_probability_duration():
    engine, _ = make_engine()
    engine.command("challenge", 0)
    engine.on_prompts_finished(1)
    for t in (2, 2.5, 3):
        engine.on_sound(sound(t))
    assert engine.tick(4.99) == []
    engine.tick(5)
    assert engine.rounds[0]["knock_times"] == [2, 2.5, 3]
    assert engine.rounds[0]["p"] == pytest.approx(round_p(3, 3, 6, 1 / 60))


def test_later_detection_cannot_extend_a_window_past_existing_quiet_deadline():
    engine, _ = make_engine()
    engine.command("challenge", 0)
    engine.on_prompts_finished(1)
    engine.on_sound(sound(2))
    actions = engine.on_sound(sound(4.1))
    assert isinstance(actions[0], PromptKnocks)
    assert engine.rounds[0]["knock_times"] == [2]


def test_watermark_and_delayed_detections_do_not_lose_in_window_answers():
    engine, _ = make_engine()
    engine.command("challenge", 0)
    engine.on_prompts_finished(2)
    assert engine.tick(1.125) == []  # Prompt completion used a newer actual clock.
    for t in (3, 3.6, 4.2):
        engine.on_sound(sound(t))
    assert engine.tick(6.199) == []
    engine.tick(6.2)
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
    engine.command("challenge", 10)
    engine.on_prompts_finished(12)
    engine.on_sound(sound(13))
    engine.tick(15)
    assert engine.background.count == 1
    assert engine.background.observed_s == pytest.approx(8 - engine.gate_tail_s)


def test_external_open_gate_survives_reset_then_closes_for_passive_exposure():
    engine, _ = make_engine()
    engine.tick(2)
    engine.gate_until(float("inf"))
    engine.command("reset", 3)
    engine.tick(5)
    assert engine.background.observed_s == 0
    engine.gate_until(6)
    engine.tick(8)
    assert engine.background.observed_s == 2


def test_spontaneous_burst_becomes_possible_and_auto_challenges_after_quiet():
    engine, _ = make_engine(auto_challenge=True)
    engine.on_sound(sound(1))
    engine.on_sound(sound(2))
    assert engine.status == "POSSIBLE" and engine.state == "LISTENING"
    assert engine.tick(3.49) == []
    actions = engine.tick(3.5)
    assert actions == [PromptVoice("copy_knocks"), PromptKnocks(3, engine.cfg.interval_s)]
    assert engine.background.count == 2
    assert engine.background.observed_s == 3.5


def test_dripping_pipe_for_five_minutes_never_confirms_and_exercises_rounds():
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
    assert any(event["type"] == "round_result" for event in events)
    assert any(event["type"] == "window_open" for event in events)


def test_high_background_rate_prevents_confirmation_after_two_correct_rounds():
    engine, _ = make_engine(prior_events=60, prior_s=60)
    t, _ = confirm(engine)
    assert engine.state == "PROMPTING"
    assert engine.status != "CONFIRMED"
    assert engine.combined_p > engine.cfg.p_confirm
    assert engine.background.lambda_per_s == 1


def test_realistic_high_rate_extra_counts_do_not_match_any_round():
    engine, _ = make_engine(prior_events=60, prior_s=60)
    engine.command("challenge", 0)
    t = 1
    for _ in range(engine.cfg.max_rounds):
        t, _ = answer_round(engine, t, heard=6)
        t += 1
    assert engine.state == "NO_RESPONSE" and engine.status == "NO_SIGNS"
    assert not any(record["matched"] for record in engine.rounds)


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
    engine.command("reset", t + 1)
    engine.on_sound(sound(t))
    snapshot = engine.snapshot()
    assert snapshot["state"] == "LISTENING" and snapshot["status"] == "NO_SIGNS"
    assert snapshot["rounds"] == [] and snapshot["answers"] == {} and not snapshot["approved"]
    assert snapshot["lambda_per_min"] == 1


def test_snapshot_and_events_are_json_serializable_and_do_not_expose_mutable_state():
    engine, events = make_engine()
    confirm(engine)
    snapshot = engine.snapshot()
    assert set(snapshot) == {"state", "status", "round", "rounds", "combined_p", "odds",
                             "answers", "approved", "lambda_per_min", "profile"}
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
