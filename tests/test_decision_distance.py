"""Exercise the real Decisions controller with in-memory camera/API/motors."""
from dataclasses import replace
from types import SimpleNamespace
import time
import threading

import pytest

from heartbeat.config import load_profile
from heartbeat.decision_patrol import DecisionsPatrol, validate_decision_settings
from heartbeat.dialog import DialogEngine
from heartbeat.rover import FakeRover
from test_patrol import ManualPipeline


class BenchRover(FakeRover):
    virtual = False

    def __init__(self, *args, front=120, fail_step=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.front, self.fail_step = front, fail_step
        self.steps = []
        self.cruises = []
        self.cruise_finished = threading.Event()
        self.cruise_canceled = False

    def capabilities(self):
        return {"guarded": True, "cruise": True}

    def cruise(self, cm, speed, deadline):
        self.cruise_finished.clear()
        self.cruise_canceled = False
        self.moving = True
        self.cruises.append(cm)
        self.cruise_finished.wait(2)
        self.moving = False
        return not self.cruise_canceled

    def cruise_observation(self):
        from heartbeat.decision_patrol import monotonic
        return {'at': monotonic(), 'cm': self.front, 'valid': self.front >= 2, 'moving': self.moving}

    def stop(self):
        self.cruise_canceled = True
        self.cruise_finished.set()
        return super().stop()

    def range(self, port):
        return {"valid": True, "cm": self.front}

    def status(self):
        return {"moving": False, "batt": 8}

    def step(self, cm, speed, deadline):
        assert not self.engine_is_listening()
        self.steps.append(cm)
        return not self.fail_step


@pytest.mark.parametrize("budget", [0, -1, True, float('nan'), float('inf')])
def test_bad_distance_budget_rejected(budget):
    cfg = {**load_profile('clap-knocks').rover['decisions'], 'forward_budget_cm': budget}
    with pytest.raises(ValueError, match='forward_budget_cm'):
        validate_decision_settings(cfg)


@pytest.fixture
def bench(monkeypatch):
    import heartbeat.decision_patrol as module
    clock = [100.0]
    monkeypatch.setattr(module, 'monotonic', lambda: clock[0])
    controllers = []

    def make(*, front=120, fail_step=False, budget=50, choice=None, confidence=1.0, continuous=False):
        profile = load_profile('pi-full-check')
        cfg = profile.rover
        cfg['settle_s'] = .01
        cfg['decisions'].update(forward_budget_cm=budget, session_s=60, continuous_forward=continuous,
                                min_confidence=.7,
                                decision_interval_s=.01, action_settle_s=0, hold_s=.01)
        profile = replace(profile, dialog={**profile.dialog, 'auto_challenge': False})
        pipeline = ManualPipeline()
        pipeline.classifier = SimpleNamespace(name='clap')
        engine = DialogEngine(profile, lambda *args: None, rng_seed=1)
        def command(name):
            engine.command(name, pipeline.t)
            return {'ok': True}
        session = SimpleNamespace(engine=engine, _active=None, command_result=command)
        rover = BenchRover(cfg=cfg, clock=lambda: pipeline.t, front=front, fail_step=fail_step)
        sequence = [0]

        def capture(**kwargs):
            sequence[0] += 1
            return SimpleNamespace(jpeg=b'fake-jpeg', captured_monotonic=clock[0], sequence=sequence[0])

        def choose(frame, telemetry, allowed):
            action = choice or ('FORWARD_SHORT' if 'FORWARD_SHORT' in allowed else 'HOLD')
            if callable(action):
                action = action(telemetry)
            return SimpleNamespace(action=action, probabilities={
                a: confidence if a == action else (1 - confidence) / (len(allowed) - 1)
                for a in allowed}, latency_s=0)

        controller = DecisionsPatrol(rover, pipeline, session, SimpleNamespace(), cfg,
            client=SimpleNamespace(choose=choose, close=lambda: None),
            camera=SimpleNamespace(capture=capture, close=lambda: None), arm_motion=True)
        controllers.append(controller)
        controller.start()

        def tick():
            clock[0] += .01
            pipeline.step(clock[0] - 100)
            time.sleep(.001)

        return controller, rover, tick

    yield make
    for controller in controllers:
        controller.close()


def test_fifty_cm_stops_after_sixteen_three_cm_steps_and_final_two(bench):
    controller, rover, tick = bench()
    for _ in range(1000):
        tick()
        if controller.done:
            break
    assert controller.done and controller.state == 'DONE'
    assert rover.steps == [3.0] * 16 + [2.0]
    assert controller.snapshot()['decisions']['forward_commanded_cm'] == 50
    assert controller.snapshot()['decisions']['reason'] == 'forward_budget_exhausted'
    assert not controller.command('resume')['ok']
    assert 'FORWARD_SHORT' not in controller._allowed_actions(120)


def test_distance_target_does_not_override_obstacle(bench):
    controller, rover, tick = bench(front=27)
    for _ in range(100):
        tick()
    assert not rover.steps
    assert controller.snapshot()['decisions']['forward_commanded_cm'] == 0


def test_continuous_forward_reobserves_without_restarting_motors(bench):
    controller, rover, tick = bench(continuous=True)
    for _ in range(100):
        tick()
    assert controller.state == 'MOVING'
    assert rover.cruises == [50]
    assert not rover.steps
    assert any(item['outcome'] == 'continued' for item in controller._history)
    rover.cruise_finished.set()
    for _ in range(100):
        tick()
        if controller.done:
            break
    assert controller.done
    assert controller.snapshot()['decisions']['forward_commanded_cm'] == 50


@pytest.mark.parametrize('action', ['HOLD', 'STOP_AND_LISTEN'])
def test_continuous_decision_can_interrupt_active_motion(bench, action):
    controller, rover, tick = bench(continuous=True,
        choice=lambda telemetry: action if telemetry['moving'] else 'FORWARD_SHORT')
    for _ in range(100):
        tick()
        if rover.cruises and controller.state not in {'MOVING', 'STOPPING'}:
            break
    assert rover.cruises == [50]
    assert rover.cruise_canceled and not rover.moving


def test_continuous_stale_camera_stops_active_motion(bench):
    controller, rover, tick = bench(continuous=True)
    for _ in range(100):
        tick()
        if rover.cruises:
            break
    controller._next_observation_at = float('inf')
    for _ in range(300):
        tick()
        if rover.cruise_canceled:
            break
    assert controller.state in {'STOPPING', 'REVIEW', 'DONE'}
    assert rover.cruise_canceled and not rover.moving


def test_continuous_audio_interrupts_motion(bench):
    controller, rover, tick = bench(continuous=True)
    for _ in range(100):
        tick()
        if rover.cruises:
            break
    controller._request_audio_stop({'kind': 'KNOCK_BURST', 'detected_t': controller.pipeline.t})
    for _ in range(30):
        tick()
    assert rover.cruise_canceled and not rover.moving
    assert controller.state in {'SETTLING', 'VERIFYING'}
    assert controller.snapshot()['decisions']['forward_commanded_cm'] == 50


def test_failed_step_keeps_its_reserved_distance_and_stops(bench):
    controller, rover, tick = bench(fail_step=True)
    for _ in range(200):
        tick()
        if controller.state == 'REVIEW':
            break
    assert controller.state == 'REVIEW'
    assert rover.steps == [3.0]
    assert controller._forward_remaining() == 47
    for _ in range(50):
        tick()
    assert rover.steps == [3.0]


def test_unconfigured_budget_preserves_existing_navigation(bench):
    controller, rover, tick = bench(budget=None)
    for _ in range(600):
        tick()
        if sum(rover.steps) > 50:
            break
    assert sum(rover.steps) > 50
    assert not controller.done
    assert controller.command('estop')['ok']
    previous = list(rover.steps)
    for _ in range(25):
        tick()
    assert rover.steps == previous


@pytest.mark.parametrize('choice, reason', [('HOLD', 'model_hold'), ('FORWARD_SHORT', 'low_confidence')])
def test_hold_reason_distinguishes_api_choice_from_blocked_movement(bench, choice, reason):
    controller, rover, tick = bench(choice=choice, confidence=.6)
    for _ in range(100):
        tick()
        if controller.snapshot()['decisions']['reason'] == reason:
            break
    assert controller.snapshot()['decisions']['reason'] == reason
    assert not rover.steps
