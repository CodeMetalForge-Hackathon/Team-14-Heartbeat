"""Automatic voice policy: sample-clock fakes only, no devices or network."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from heartbeat.config import load_profile
from heartbeat.events import EventBus
from heartbeat.prompter import FakePrompter, PromptVoice
from heartbeat.runner import DialogSession, SimulationSource, prepare_runtime


class ControlledPipeline:
    def __init__(self, profile):
        self.profile, self.bus, self.t = profile, EventBus(), 0.0
        self.on_sound, self.on_block, self.on_end, self.messages = [], [], [], []
        self.detector = SimpleNamespace(gate=lambda *args: None)

    @property
    def detection_watermark(self):
        return self.t

    def call_soon(self, callback):
        self.messages.append(callback)

    def step(self, t):
        self.t = t
        while self.messages:
            self.messages.pop(0)()
        for callback in self.on_block:
            callback(t)


@pytest.fixture
def response_session():
    profile = load_profile("clap-distress")
    profile = replace(profile, dialog=dict(profile.dialog, auto_challenge=True))
    pipeline = ControlledPipeline(profile)
    events = []
    pipeline.bus.subscribe(events.append)
    prompter = FakePrompter(cfg=profile.prompter, no_survivor=True)
    session = DialogSession(pipeline, prompter)
    pipeline.step(3.1)
    yield pipeline, session, events
    session.close()


def observe(pipeline, *, end=None, start=None, label="SCREAM", **overrides):
    end = pipeline.t if end is None else end
    data = dict(start=end - 3 if start is None else start, label=label,
                score=.9, alert=True, possible_distress=True, matched_phrases=[])
    data.update(overrides)
    pipeline.bus.publish("audio_observation", end, **data)


def triggers(events):
    return [e for e in events if e.type == "operator"
            and e.data.get("action") == "distress_challenge"]


@pytest.mark.parametrize("label", ["SCREAM", "SHOUT", "CRYING"])
def test_fresh_distress_requests_challenge_on_processing_boundary(response_session, label):
    pipeline, session, events = response_session
    observe(pipeline, label=label)
    assert session.engine.state == "LISTENING"
    pipeline.step(3.2)
    assert session.engine.state == "PRE_LISTEN"
    assert len(triggers(events)) == 1
    assert session.engine.status != "CONFIRMED"
    assert not [e for e in events if e.type in {"sound", "answer", "round_result"}]
    pipeline.step(3.2 + pipeline.profile.dialog["pre_listen_s"])
    assert any(isinstance(action, PromptVoice) and action.key == "copy_knocks"
               for action in session.prompter.actions)


@pytest.mark.parametrize("label", ["SPEECH", "BREATHING", "OTHER", "UNCERTAIN"])
def test_ordinary_labels_do_not_start_voice_even_with_wrong_distress_flag(response_session, label):
    pipeline, session, events = response_session
    observe(pipeline, label=label)
    pipeline.step(3.2)
    assert session.engine.state == "LISTENING" and not triggers(events)


@pytest.mark.parametrize("change", [
    {"alert": False}, {"possible_distress": False}, {"end": .5, "start": 0},
    {"end": 4, "start": 1}, {"start": -1}, {"start": True},
    {"start": float("nan")}, {"start": 3.5},
])
def test_invalid_suppressed_stale_or_future_observations_do_not_respond(response_session, change):
    pipeline, session, events = response_session
    observe(pipeline, **change)
    pipeline.step(3.2)
    assert session.engine.state == "LISTENING" and not triggers(events)


@pytest.mark.parametrize("enabled", [False, True])
def test_help_phrase_only_requests_a_challenge_when_asr_is_enabled(response_session, enabled):
    pipeline, session, events = response_session
    session._monitor_cfg["asr"]["enabled"] = enabled
    observe(pipeline, label="SPEECH", matched_phrases=["help me"])
    pipeline.step(3.2)
    assert bool(triggers(events)) is enabled
    assert session.engine.status != "CONFIRMED"


@pytest.mark.parametrize("switch", ["auto_challenge", "monitor"])
def test_existing_manual_and_patrol_modes_keep_automatic_voice_disabled(response_session, switch):
    pipeline, session, events = response_session
    if switch == "auto_challenge":
        pipeline.profile.dialog["auto_challenge"] = False
    else:
        session._monitor_cfg["enabled"] = False
    observe(pipeline)
    pipeline.step(3.2)
    assert session.engine.state == "LISTENING" and not triggers(events)


def test_own_prompt_and_tail_windows_cannot_start_a_response(response_session):
    pipeline, session, events = response_session
    observe(pipeline)
    pipeline.bus.publish("prompt_start", pipeline.t)
    pipeline.step(3.2)  # The queued observation loses to the new prompt gate.
    pipeline.bus.publish("prompt_end", 3.2)
    observe(pipeline, end=3.2, start=.2)
    pipeline.step(3.3)
    assert not triggers(events)
    start = 3.2 + pipeline.profile.prompter["gate_tail_s"]
    pipeline.step(start + 3)
    observe(pipeline, start=start)
    pipeline.step(start + 3.1)
    assert len(triggers(events)) == 1


def test_reset_discards_queued_and_pre_reset_audio(response_session):
    pipeline, session, events = response_session
    observe(pipeline)
    session.command_result("reset")
    pipeline.step(3.2)
    observe(pipeline, start=.2)
    pipeline.step(3.3)
    assert session.engine.state == "LISTENING" and not triggers(events)


def test_repeated_distress_does_not_restart_active_dialog_and_respects_cooldown(response_session):
    pipeline, session, events = response_session
    observe(pipeline)
    observe(pipeline)
    pipeline.step(3.2)
    observe(pipeline)
    pipeline.step(3.3)
    assert len(triggers(events)) == 1
    session.command_result("reset")
    pipeline.step(6.3)
    observe(pipeline, start=3.3)
    pipeline.step(6.4)
    assert not session.prompter.actions and len(triggers(events)) == 1
    pipeline.step(3.2 + session._monitor_cfg["cooldown_s"] + .1)
    observe(pipeline)
    pipeline.step(pipeline.t + .1)
    assert len(triggers(events)) == 2


def test_closed_session_cannot_start_a_late_response(response_session):
    pipeline, session, events = response_session
    observe(pipeline)
    session.close()
    pipeline.step(3.2)
    assert not triggers(events)


@pytest.mark.parametrize("initial_knocks,answers", [(True, True), (False, True), (False, False)],
                         ids=["knocks-and-replies", "scream-and-replies", "scream-without-replies"])
def test_complete_automatic_voice_flow(initial_knocks, answers):
    profile = load_profile("clap-distress")
    # The policy sees explicitly scripted vocal labels. Synthetic knocks use
    # the real onset/dialog pipeline; no recognition accuracy is claimed.
    profile = replace(profile, classifier=replace(profile.classifier, name="none"),
                      dialog=dict(profile.dialog, auto_challenge=True, auto_interview=True))
    runtime = prepare_runtime(
        profile,
        lambda: SimulationSource(profile, duration_s=70, no_survivor=not initial_knocks),
        lambda pipeline: FakePrompter(pipeline.bus, cfg=profile.prompter,
                                     clock=lambda: pipeline.t, no_survivor=not answers),
        stop_when_finished=answers, collect_events=True,
    )
    if not initial_knocks:
        sent = []

        def scream(t):
            if not sent and t >= 3.1:
                sent.append(t)
                observe(runtime.pipeline)

        runtime.pipeline.on_block.append(scream)
    try:
        result = runtime.run()
        voices = [a.key for a in runtime.session.prompter.actions if isinstance(a, PromptVoice)]
        assert "copy_knocks" in voices
        assert ("we_hear_you" in voices) is answers
        if answers:
            assert result.snapshot["status"] == "CONFIRMED"
            assert set(result.snapshot["answers"]) == {"q_people", "q_move", "q_hurt"}
            assert {"q_people", "q_move", "q_hurt"} <= set(voices)
            matched = [e for e in result.events if e.type == "round_result" and e.data["matched"]]
            assert len({e.data["expected"] for e in matched}) >= 2
        else:
            assert result.snapshot["status"] != "CONFIRMED"
            assert not [e for e in result.events if e.type == "answer"]
        assert bool(triggers(result.events)) is not initial_knocks
        assert not [e for e in result.events if e.type in {"rover", "patrol", "error"}]
    finally:
        runtime.close()
