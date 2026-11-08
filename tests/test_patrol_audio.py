"""Audio-driven motion transitions; no model, network, or physical devices."""

from dataclasses import replace
from types import SimpleNamespace
import threading

import pytest

from heartbeat.config import load_profile
from heartbeat.dialog import DialogEngine
from heartbeat.patrol import Patrol, prepare_patrol_simulation
from heartbeat.rover import FakeRover
from test_patrol import ManualPipeline


@pytest.fixture
def setup():
    profile = load_profile("clap-distress")
    profile = replace(profile, dialog=dict(profile.dialog, auto_challenge=False))
    pipeline = ManualPipeline()
    engine = DialogEngine(profile, lambda *args: None, rng_seed=1)
    commands = []

    def command(name):
        commands.append(name)
        engine.command(name, pipeline.t)
        return {"ok": True}

    session = SimpleNamespace(engine=engine, command_result=command, _active=None)
    rover = FakeRover(cfg=profile.rover, clock=lambda: pipeline.t)
    route = [{"name": "A", "moves": [["drive", 100]]}, {"name": "B", "moves": []}]
    patrol = Patrol(rover, pipeline, session, SimpleNamespace(), route, profile.rover)
    events = []
    pipeline.bus.subscribe(events.append)
    patrol.start()
    pipeline.step(0)
    yield pipeline, patrol, commands, events
    patrol.close()


def observation(pipeline, end, *, start=None, label="SCREAM", distress=True, **kwargs):
    pipeline.bus.publish("audio_observation", end, start=end - 3 if start is None else start,
                         label=label, possible_distress=distress, score=.9,
                         matched_phrases=[], transcript="", **kwargs)


def begin_verify(pipeline, patrol):
    pipeline.step(3.1)
    observation(pipeline, 3.1)
    assert patrol.state == "STOPPING"
    pipeline.step(3.2)
    assert patrol.state == "SETTLING"
    pipeline.step(5.1)
    assert patrol.state == "SETTLING"
    pipeline.step(5.2)
    assert patrol.state == "VERIFYING"


def test_stop_settle_fresh_verification_and_operator_hold(setup):
    pipeline, patrol, commands, events = setup
    begin_verify(pipeline, patrol)
    assert not pipeline.stopped and not patrol.rover.moving
    # A late old window cannot contribute, even with a new publication time.
    observation(pipeline, 5.2, start=2.2)
    assert patrol._verified_cue is None
    pipeline.step(8.3)
    observation(pipeline, 8.3)
    assert patrol._verified_cue["label"] == "SCREAM"
    assert "challenge" not in commands
    pipeline.step(13.2)
    assert patrol.state == "LISTENING" and commands[-1] == "challenge"
    # Deterministic dialog, not the classifier, supplies the final result.
    patrol._engine_command("reset")
    patrol._gap_result(patrol.engine.snapshot(), "NO_RESPONSE")
    assert patrol.state == "REVIEW" and patrol.snapshot()["paused"]
    assert patrol.results[0]["status"] == "POSSIBLE"
    assert patrol.results[0]["arrival_unverified"]
    pipeline.step(50)
    assert patrol.state == "REVIEW" and not pipeline.stopped
    assert len([e for e in events if e.type == "patrol_cue" and e.data["phase"] == "trigger"]) == 1
    assert patrol.command("resume")["ok"]
    pipeline.step(50.1)
    assert patrol.gap_name == "B"
    assert len([c for c in patrol.rover.trace if c["command"] == "DRIVE"]) == 1


def test_silence_after_candidate_holds_unconfirmed_without_challenge(setup):
    pipeline, patrol, commands, _ = setup
    begin_verify(pipeline, patrol)
    pipeline.step(13.2)
    assert patrol.state == "REVIEW"
    assert patrol.results[0]["reason"] == "UNCONFIRMED"
    assert patrol.results[0]["status"] == "POSSIBLE"
    assert "challenge" not in commands and not pipeline.stopped


@pytest.mark.parametrize("label", ["BREATHING", "SPEECH", "OTHER", "UNCERTAIN"])
def test_ordinary_labels_cannot_stop_even_if_distress_flag_is_wrong(setup, label):
    pipeline, patrol, _, _ = setup
    pipeline.step(3.1)
    observation(pipeline, 3.1, label=label)
    assert patrol.state == "MOVING"


def test_help_phrase_can_stop_without_scream(setup):
    pipeline, patrol, _, _ = setup
    pipeline.step(3.1)
    pipeline.bus.publish("audio_observation", 3.1, start=.1, label="SPEECH", score=.8,
                         possible_distress=True, matched_phrases=["help me"], transcript="Help me")
    assert patrol.state == "STOPPING" and patrol._trigger["label"] == "HELP_PHRASE"


def test_repeated_candidates_do_not_repeat_stop_and_estop_wins_late_ack(setup):
    pipeline, patrol, _, _ = setup
    pipeline.step(3.1)
    observation(pipeline, 3.1)
    for _ in range(5):
        observation(pipeline, 3.1)
    assert [c["command"] for c in patrol.rover.trace].count("STOP") == 1
    patrol.command("estop")
    pipeline.step(3.2)
    assert patrol.state == "ESTOP"
    assert not patrol.command("resume")["ok"]


def test_operator_pause_wins_candidate_qualified_before_pause(setup):
    pipeline, patrol, _, _ = setup
    pipeline.step(3.1)
    assert patrol.command("pause")["ok"]
    generation = patrol._generation
    patrol._request_audio_stop({"label": "SCREAM", "start": .1, "end": 3.1})
    assert patrol._generation == generation
    pipeline.step(3.2)
    assert patrol.state == "PAUSED" and patrol._trigger is None


def test_pause_during_review_resumes_without_duplicate_result(setup):
    pipeline, patrol, _, _ = setup
    begin_verify(pipeline, patrol)
    pipeline.step(13.2)
    patrol.command("pause")
    pipeline.step(13.3)
    assert patrol.state == "PAUSED"
    patrol.command("resume")
    pipeline.step(13.4)
    assert patrol.gap_name == "B" and len(patrol.results) == 1
    assert patrol.snapshot()["arrival_unverified"]


@pytest.mark.parametrize("case", ["old", "future", "phase_overlap", "prompt"])
def test_stale_or_own_prompt_audio_never_stops(setup, case):
    pipeline, patrol, _, _ = setup
    pipeline.step(4.5)
    if case == "old":
        observation(pipeline, 1, start=0)
    elif case == "future":
        observation(pipeline, 6)
    elif case == "phase_overlap":
        observation(pipeline, 2, start=-1)
    else:
        pipeline.bus.publish("prompt_start", 2)
        pipeline.bus.publish("prompt_end", 3)
        observation(pipeline, 4.5)
    assert patrol.state == "MOVING"


def test_moving_knock_burst_is_candidate_only_and_passthrough_is_ignored(setup):
    pipeline, patrol, commands, _ = setup
    def knock(t, classifier):
        pipeline.step(t + .9)
        pipeline.bus.publish("sound", t, is_knock=True, gated=True, score=.9, classifier=classifier)
    for t in (.2, .5, .8):
        knock(t, "none")
    assert patrol.state == "MOVING"
    for t in (1.1, 1.4, 1.7):
        knock(t, "clap")
    assert patrol.state == "STOPPING" and patrol._trigger["label"] == "KNOCK_BURST"
    assert "challenge" not in commands


def test_monitor_failure_during_verification_is_logged_as_incomplete(setup):
    pipeline, patrol, _, _ = setup
    begin_verify(pipeline, patrol)
    pipeline.bus.publish("error", 6, where="audio_monitor", message="service offline")
    pipeline.step(13.2)
    assert patrol.state == "REVIEW"
    assert patrol.results[0]["reason"] == "AUDIO_ERROR"


def test_failed_stop_cannot_be_resumed_and_capture_stays_alive(setup):
    pipeline, patrol, _, _ = setup
    patrol.rover.stop = lambda: False
    pipeline.step(3.1)
    observation(pipeline, 3.1)
    pipeline.step(3.2)
    assert patrol.state == "FAULT" and not pipeline.stopped
    assert not patrol.command("resume")["ok"]
    assert not patrol.command("pause")["ok"]
    assert not patrol.motion_allowed


def test_stop_timeout_rejects_late_success(setup):
    pipeline, patrol, _, _ = setup
    pipeline.step(3.1)
    observation(pipeline, 3.1)
    pending = pipeline.messages.pop()
    pipeline.step(9.2)
    assert patrol.state == "FAULT" and not pipeline.stopped
    pending()
    assert patrol.state == "FAULT"


def test_blocking_stop_does_not_block_audio_thread(setup):
    pipeline, patrol, _, _ = setup
    started, release = threading.Event(), threading.Event()
    original = patrol.rover.stop
    def blocking_stop():
        started.set()
        assert release.wait(3)
        return original()
    patrol.rover.stop = blocking_stop
    patrol._virtual = False  # Only STOP transport is made asynchronous here.
    try:
        pipeline.step(3.1)
        observation(pipeline, 3.1)
        assert started.wait(1)
        pipeline.step(3.5)
        assert patrol.state == "STOPPING" and not pipeline.stopped
    finally:
        release.set()
        patrol._stop_thread.join(2)
        patrol._virtual = True
    pipeline.step(4)
    assert patrol.state == "SETTLING"
    pipeline.step(5.9)
    assert patrol.state == "SETTLING"  # Settling starts at ACK, not detection.


def test_audio_stop_bypasses_blocked_drive_worker_and_discards_its_completion():
    profile = load_profile("clap-distress")
    profile = replace(profile, dialog=dict(profile.dialog, auto_challenge=False))
    pipeline = ManualPipeline()
    engine = DialogEngine(profile, lambda *args: None, rng_seed=1)

    class BlockingRover(FakeRover):
        virtual = False

        def __init__(self):
            super().__init__(cfg=profile.rover, clock=lambda: pipeline.t)
            self.started, self.release = threading.Event(), threading.Event()

        def drive(self, *args):
            ok = super().drive(*args)
            self.started.set()
            assert self.release.wait(3), "STOP must bypass the busy movement worker"
            return ok

        def stop(self):
            ok = super().stop()
            self.release.set()
            return ok

    rover = BlockingRover()
    patrol = Patrol(rover, pipeline, engine, SimpleNamespace(),
                    [{"name": "A", "moves": [["drive", 100], ["drive", 20]]}], profile.rover)
    try:
        patrol.start()
        pipeline.step(0)
        assert rover.started.wait(1)
        pipeline.step(3.1)
        observation(pipeline, 3.1)
        patrol._stop_thread.join(2)
        assert not patrol._stop_thread.is_alive() and rover.release.is_set()
        pipeline.step(3.2)
        assert patrol.state == "SETTLING" and not pipeline.stopped
        assert [c["command"] for c in rover.trace].count("DRIVE") == 1
        patrol.command("estop")
        pipeline.step(3.3)
        assert patrol.state == "ESTOP"
    finally:
        patrol.close()


def test_full_scripted_demo_holds_and_resumes_without_models():
    profile = load_profile("rover-audio-demo")
    route = [{"name": "Gap 1", "moves": [["drive", 40]]}]
    runtime, patrol = prepare_patrol_simulation(profile, route)
    reviewed, held = [], []
    def review(t):
        if patrol.state == "REVIEW" and not reviewed:
            if not held:
                held.append(t)
            assert not runtime.pipeline._stop.is_set()
            if t >= held[0] + profile.rover["settle_s"]:
                reviewed.append(patrol.results[0])
                patrol.command("resume")
    runtime.pipeline.on_block.append(review)
    try:
        runtime.run()
        assert reviewed and reviewed[0]["status"] == "CONFIRMED"
        assert patrol.state == "DONE"
        assert any(e.type == "level" and held[0] < e.t < held[0] + profile.rover["settle_s"]
                   for e in runtime.events)
        assert not [e.data for e in runtime.events if e.type == "error"]
    finally:
        runtime.close()


@pytest.mark.parametrize("change", [{"verify_s": 0}, {"max_age_s": float("nan")},
                                     {"knock_count": 1}, {"enabled": "yes"}])
def test_bad_audio_control_configuration_is_rejected(setup, change):
    pipeline, patrol, _, _ = setup
    cfg = {**patrol.cfg, "audio_stop": {**patrol.audio_stop, **change}}
    with pytest.raises(ValueError, match="audio_stop"):
        Patrol(patrol.rover, pipeline, patrol.engine, SimpleNamespace(), patrol.route, cfg)
