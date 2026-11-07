"""Patrol safety and the complete three-gap synthetic survivor demo."""

import asyncio
from dataclasses import replace
import json
import threading
from types import SimpleNamespace

import pytest

from heartbeat.config import load_profile
from heartbeat.events import EventBus
from heartbeat.patrol import Patrol, load_route, prepare_patrol_simulation
from heartbeat.rover import FakeRover


@pytest.fixture
def profile():
    profile = load_profile()
    # No optional model files, accelerators, serial ports or network are needed.
    return replace(profile, classifier=replace(profile.classifier, name="none"))


@pytest.fixture
def route():
    return [{"name": "Gap 1", "moves": [["drive", 40]]},
            {"name": "Gap 2", "moves": [["turn", 90], ["drive", 30]]},
            {"name": "Gap 3", "moves": [["turn", -90], ["drive", 30]]}]


def run_patrol(profile, route, setup=None):
    runtime, patrol = prepare_patrol_simulation(profile, route)
    if setup is not None:
        setup(runtime, patrol)
    try:
        runtime.run()
        return runtime, patrol
    finally:
        runtime.close()


def test_three_gap_survivor_interview_and_motion_event_log(profile, route):
    checks, backgrounds, first_reset_counts, engine_threads = [], [], [], []

    def setup(runtime, patrol):
        runtime.pipeline.source.schedule(0.25, "knock", -3)

        def seed_history():
            background = runtime.session.engine.background
            background.observe(0.0, 0.1)
            assert background.add(0.05)

        runtime.pipeline.call_soon(seed_history)

        def inspect(event):
            if event.type == "state":
                engine_threads.append(threading.get_ident())
            if event.type == "operator" and event.data.get("command") == "reset":
                backgrounds.append(runtime.session.engine.background)
                first_reset_counts.append(backgrounds[-1].count)
            if event.type == "rover" and event.data.get("kind") == "command":
                if event.data["command"] in {"DRIVE", "TURN"}:
                    checks.append((patrol.motion_allowed, runtime.session.engine.state,
                                   patrol._gate_started is not None,
                                   runtime.session._active is None))

        runtime.bus.subscribe(inspect)

    runtime, patrol = run_patrol(profile, route, setup)
    assert [result["status"] for result in patrol.results] == ["NO_SIGNS", "CONFIRMED", "NO_SIGNS"]
    assert patrol.results[1]["answers"] == {"q_people": 2, "q_move": "yes", "q_hurt": "yes"}
    assert patrol.results[1]["combined_p"] <= profile.dialog["p_confirm"]
    assert checks and all(allowed and closed and idle for allowed, _, closed, idle in checks)
    assert all(state not in {"PROMPTING", "PRE_LISTEN", "RESPONSE_WINDOW", "INTERVIEW"}
               for _, state, _, _ in checks)
    assert len(backgrounds) == 3 and all(item is backgrounds[0] for item in backgrounds)
    assert set(engine_threads) == {runtime.pipeline._thread.ident}
    assert first_reset_counts[0] == 1
    sounds = [event for event in runtime.events if event.type == "sound" and event.t < 2]
    assert sounds and all(event.data["gated"] for event in sounds)
    assert patrol.state == "DONE" and patrol.done
    led_commands = [item["args"] for item in patrol.rover.trace if item["command"] == "LED"]
    assert led_commands == [profile.rover["led_no_signs"], profile.rover["led_confirmed"],
                            profile.rover["led_no_signs"]]
    done = [event for event in runtime.events if event.type == "patrol_done"]
    assert len(done) == 1 and done[0].data["gaps"] == patrol.results
    json.dumps(done[0].to_dict(), allow_nan=False)
    assert not [event.data for event in runtime.events if event.type == "error"]


def test_dry_patrol_is_deterministic_in_sample_time(profile, route):
    _, first = run_patrol(profile, route)
    _, second = run_patrol(profile, route)
    assert first.results == second.results
    assert first.rover.trace == second.rover.trace


def test_estop_mid_move_is_stop_first_and_latched(profile, route):
    stopped = []

    def setup(runtime, patrol):
        def control(t):
            if not stopped and t >= 0.4:
                assert patrol.state == "MOVING" and patrol.rover.moving
                assert patrol.command("estop")["ok"]
                assert patrol.rover.trace[-1]["command"] == "STOP"
                assert not patrol.rover.moving
                stopped.append(t)
        runtime.pipeline.on_block.append(control)

    runtime, patrol = run_patrol(profile, route, setup)
    assert stopped and patrol.state == "ESTOP" and patrol.done
    assert patrol.snapshot()["estopped"]
    commands = [event.data.get("command") for event in runtime.events
                if event.type == "rover" and event.data.get("kind") == "command"]
    assert commands[:2] == ["DRIVE", "STOP"]
    assert not any(command in {"DRIVE", "TURN"} for command in commands[2:])
    assert not patrol.command("resume")["ok"]
    assert not patrol.rover.drive(1)
    assert patrol.results == []


def test_abort_during_dialog_ends_cleanly(profile, route):
    aborted = []

    def setup(runtime, patrol):
        def control(t):
            if not aborted and patrol.state == "LISTENING":
                assert patrol.command("abort")["ok"]
                aborted.append(t)
        runtime.pipeline.on_block.append(control)

    runtime, patrol = run_patrol(profile, route, setup)
    assert aborted and patrol.done and patrol.state == "ABORTED"
    assert not patrol.rover.moving
    assert patrol.results == []
    done = [event for event in runtime.events if event.type == "patrol_done"]
    assert len(done) == 1 and done[0].data["aborted"]


def test_pause_mid_move_resumes_at_actual_stop_and_marks_arrival(profile, route):
    controls = []

    def setup(runtime, patrol):
        def control(t):
            if not controls and t >= 0.4:
                patrol.command("pause")
                controls.append("pause")
            elif controls == ["pause"] and patrol.state == "PAUSED" and t >= 0.9:
                assert not patrol.rover.moving
                patrol.command("resume")
                controls.append("resume")
        runtime.pipeline.on_block.append(control)

    _, patrol = run_patrol(profile, route[:1], setup)
    assert controls == ["pause", "resume"]
    assert patrol.state == "DONE"
    assert patrol.results[0]["arrival_unverified"]
    assert patrol.results[0]["interrupted_move"]
    assert len([item for item in patrol.rover.trace if item["command"] == "DRIVE"]) == 1


def test_gap_timeout_drains_prompt_before_next_move(profile, route):
    profile = replace(profile, rover=dict(profile.rover, gap_timeout_s=4.5))
    checks = []

    def setup(runtime, patrol):
        def inspect(event):
            if (event.type == "rover" and event.data.get("kind") == "command"
                    and event.data.get("command") in {"DRIVE", "TURN"}):
                checks.append(runtime.session._active is None)
        runtime.bus.subscribe(inspect)

    _, patrol = run_patrol(profile, route, setup)
    assert patrol.state == "DONE"
    assert [item["reason"] for item in patrol.results] == ["TIMEOUT"] * 3
    assert checks and all(checks)


def test_pause_while_reporting_never_duplicates_a_gap(profile, route):
    controls = []

    def setup(runtime, patrol):
        def inspect(event):
            if event.type == "gap_result" and not controls:
                patrol.command("pause")
                controls.append("pause")

        def resume(t):
            if controls == ["pause"] and patrol.state == "PAUSED":
                patrol.command("resume")
                controls.append("resume")

        runtime.bus.subscribe(inspect)
        runtime.pipeline.on_block.append(resume)

    _, patrol = run_patrol(profile, route, setup)
    assert controls == ["pause", "resume"]
    assert [result["gap"] for result in patrol.results] == ["Gap 1", "Gap 2", "Gap 3"]
    assert patrol.state == "DONE"


def test_noisy_checks_exhaust_before_gap_advances(profile, route):
    profile = replace(profile, dialog=dict(profile.dialog, prior_events=10, prior_s=1,
                                           max_noisy_tries=2, noisy_retry_s=0.5))
    _, patrol = run_patrol(profile, route[:1])
    assert patrol.state == "DONE"
    assert patrol.results[0]["status"] == "NO_SIGNS"
    assert patrol.results[0]["reason"] == "TOO_NOISY"
    assert patrol.results[0]["noisy"]


def test_only_confirmed_gap_is_approved_and_historical_engine_is_untouched(profile, route):
    profile = replace(profile, rover=dict(profile.rover, stop_on_done=False))
    approvals, rejected = [], []

    def setup(runtime, patrol):
        assert runtime.pipeline.source._limit >= round(
            profile.server["dashboard_sim_duration_s"] * profile.audio.sample_rate)

        def approve(t):
            if patrol.done and not approvals:
                rejected.extend([patrol.approve("Gap 1", "AB"),
                                 patrol.approve("Missing", "AB"),
                                 patrol.approve("Gap 2", "  "),
                                 patrol.approve("Gap 2", 12)])
                approvals.append(patrol.approve("Gap 2", " AB "))
                assert not runtime.session.engine.approved
                runtime.pipeline.stop()

        runtime.pipeline.on_block.append(approve)

    runtime, patrol = run_patrol(profile, route, setup)
    assert approvals[0]["ok"] and all(not item["ok"] for item in rejected)
    assert patrol.results[1]["approved"] and patrol.results[1]["approved_by"] == "AB"
    assert approvals[0]["patrol"]["results"] == patrol.results
    events = [event for event in runtime.events if event.type == "approval"]
    assert len(events) == 1
    assert events[0].data == {"gap": "Gap 2", "operator": "AB", "approved": True}
    json.dumps(events[0].to_dict(), allow_nan=False)
    assert not patrol.approve("Gap 2", "AB")["ok"]  # Main-thread mutation is refused.


class ManualPipeline:
    """A manually advanced processing thread, for blocking transport tests."""

    def __init__(self):
        self.t = 0.0
        self.bus = EventBus()
        self.on_block, self.on_end, self.messages = [], [], []
        self.detector = SimpleNamespace(gate=lambda start, end: None)
        self.stopped = False

    def call_soon(self, callback):
        self.messages.append(callback)

    def step(self, t):
        self.t = t
        while self.messages:
            self.messages.pop(0)()
        for hook in self.on_block:
            hook(t)

    def stop(self):
        self.stopped = True


def test_blocking_move_never_blocks_processing_and_late_completion_cannot_resume(profile, route):
    from heartbeat.dialog import DialogEngine

    class BlockingRover(FakeRover):
        virtual = False

        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.started, self.release = threading.Event(), threading.Event()

        def drive(self, *args):
            ok = super().drive(*args)
            self.started.set()
            assert self.release.wait(5), "control never interrupted the fake move"
            return ok

        def stop(self):
            ok = super().stop()
            self.release.set()
            return ok

    pipeline = ManualPipeline()
    engine = DialogEngine(profile, lambda *args: None, rng_seed=1)
    rover = BlockingRover(cfg=profile.rover, bus=pipeline.bus, clock=lambda: pipeline.t)
    patrol = Patrol(rover, pipeline, engine, SimpleNamespace(), route, profile.rover)
    try:
        patrol.start()
        pipeline.step(0.0)
        assert rover.started.wait(2)
        pipeline.step(0.5)  # Returns while drive() is still waiting for STOP.
        assert not rover.release.is_set()
        assert patrol.command("estop")["ok"]
        pipeline.step(0.6)
        assert patrol.done and pipeline.stopped and patrol.state == "ESTOP"
        patrol.close()  # Join the old move completion; then deliver its stale callback.
        pipeline.step(0.7)
        assert patrol.state == "ESTOP"
        assert [item["command"] for item in rover.trace].count("DRIVE") == 1
    finally:
        patrol.close()


@pytest.mark.parametrize("trailing_pause", [False, True])
def test_queued_resume_cannot_undo_later_estop(profile, route, trailing_pause):
    from heartbeat.dialog import DialogEngine

    pipeline = ManualPipeline()
    engine = DialogEngine(profile, lambda *args: None, rng_seed=1)
    rover = FakeRover(cfg=profile.rover, clock=lambda: pipeline.t)
    patrol = Patrol(rover, pipeline, engine, SimpleNamespace(), route, profile.rover)
    try:
        patrol.start()
        pipeline.step(0.0)
        patrol.command("pause")
        pipeline.step(0.5)
        assert patrol.state == "PAUSED"
        assert patrol.command("resume")["ok"]
        assert patrol.command("estop")["ok"]
        if trailing_pause:
            patrol.command("pause")
        pipeline.step(0.6)
        assert patrol.state == "ESTOP" and patrol.done and rover.estopped
        assert patrol._halted and not rover.moving
        assert [item["command"] for item in rover.trace].count("DRIVE") == 1
    finally:
        patrol.close()


def test_resume_waits_for_physical_prompt_and_gate_tail(profile, route):
    from heartbeat.dialog import DialogEngine

    pipeline = ManualPipeline()
    engine = DialogEngine(profile, lambda *args: None, rng_seed=1)
    commands = []

    def command_result(name):
        commands.append(name)
        engine.command(name, pipeline.t)
        return {"ok": True}

    session = SimpleNamespace(engine=engine, command_result=command_result, _active=object())
    rover = FakeRover(cfg=profile.rover, clock=lambda: pipeline.t)
    patrol = Patrol(rover, pipeline, session, SimpleNamespace(), route, profile.rover)
    try:
        patrol._started, patrol._halted = True, True
        patrol.gap_index, patrol.state = 0, "PAUSED"
        patrol._close_gate()
        patrol.command("resume")
        pipeline.step(0.0)
        pipeline.step(2.5)
        assert patrol.state == "SETTLING" and commands == []
        session._active = None
        pipeline.bus.publish("prompt_end", 3.0)
        pipeline.step(3.1)
        assert patrol.state == "SETTLING" and commands == []
        pipeline.step(3.0 + profile.prompter["gate_tail_s"])
        assert patrol.state == "LISTENING" and commands == ["reset", "challenge"]
        assert not rover.trace
    finally:
        patrol.close()


@pytest.mark.parametrize("state", ["PROMPTING", "PRE_LISTEN", "RESPONSE_WINDOW", "INTERVIEW"])
def test_motion_gate_refuses_every_active_dialog_state(profile, route, state):
    from heartbeat.dialog import DialogEngine

    pipeline = ManualPipeline()
    engine = DialogEngine(profile, lambda *args: None, rng_seed=1)
    rover = FakeRover(cfg=profile.rover)
    patrol = Patrol(rover, pipeline, engine, SimpleNamespace(), route, profile.rover)
    try:
        patrol.state = "MOVING"
        patrol._close_gate()
        engine.state = state
        assert not patrol.motion_allowed
        assert not rover.drive(1)
        assert not rover.trace
    finally:
        patrol.close()


@pytest.mark.parametrize("route", [[], {}, [{"name": "A", "moves": [["drive", 101]]}],
                                    [{"name": "A", "moves": [["turn", 181]]}],
                                    [{"name": "A", "moves": [["drive", True]]}],
                                    [{"name": "A", "moves": [["fly", 1]]}],
                                    [{"name": "A", "moves": []}, {"name": "A", "moves": []}]])
def test_route_rejected_before_motor_commands(route):
    with pytest.raises(ValueError):
        load_route(route)


def test_route_file_loads_without_hardware(tmp_path):
    route = tmp_path / "route.yaml"
    route.write_text('- name: Gap 1\n  moves: [[drive, 40]]\n', encoding="utf-8")
    assert load_route(route) == [{"name": "Gap 1", "moves": [("drive", 40.0)]}]


def test_cli_ctrl_c_stops_rover_before_closing_audio(monkeypatch, tmp_path):
    from heartbeat import cli, patrol as patrol_module

    calls = []
    controller = SimpleNamespace(done=False)

    def command(name):
        calls.append(name)
        controller.done = True
        return {"ok": True}

    def interrupted():
        raise KeyboardInterrupt

    controller.command = command
    runtime = SimpleNamespace(pipeline=SimpleNamespace(join=interrupted),
                              start=lambda: calls.append("start"),
                              close=lambda: calls.append("close"))
    monkeypatch.setattr(patrol_module, "prepare_patrol_simulation", lambda *a, **kw: (runtime, controller))
    monkeypatch.setattr(cli, "_patrol_input", lambda _: None)
    route = tmp_path / "route.yaml"
    route.write_text('- name: Gap 1\n  moves: []\n')
    assert cli.main(["patrol", "--dry-run", "--route", str(route),
                     "--run-dir", str(tmp_path / "run")]) == 130
    assert calls == ["start", "estop", "close"]


def test_dashboard_shutdown_stops_rover_before_closing_audio(monkeypatch, tmp_path, profile):
    from heartbeat import cli, server

    calls, hooks = [], {}
    controller = SimpleNamespace(done=False)

    def command(name):
        calls.append(name)
        controller.done = True
        return {"ok": True}

    controller.command = command
    runtime = SimpleNamespace(patrol=controller, pipeline=object(), session=object(),
                              bus=EventBus(), run_dir=tmp_path, blackbox=None,
                              start=lambda: None, close=lambda: calls.append("close"))

    def app(*args, **kwargs):
        hooks.update(kwargs)
        return object()

    monkeypatch.setattr(server, "create_app", app)
    monkeypatch.setattr(server, "run_server", lambda *a, **kw: asyncio.run(hooks["on_stop"]()))
    cli._serve(runtime, SimpleNamespace(host=None, port=None), profile)
    assert hooks["patrol"] is controller
    assert calls == ["estop", "close", "close"]
