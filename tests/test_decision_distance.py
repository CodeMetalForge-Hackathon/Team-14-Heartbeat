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
        self.pivots = []
        self.approaches = []
        self.observations = []
        self.battery = 8
        self.cruise_finished = threading.Event()
        self.cruise_canceled = False

    def capabilities(self):
        return {"guarded": True, "cruise": True, "noecho": True, "approach": True}

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
        return {"valid": self.front >= 2, "cm": self.front}

    def status(self):
        return {"moving": False, "batt": self.battery}

    def step(self, cm, speed, deadline):
        assert not self.engine_is_listening()
        self.steps.append(cm)
        return not self.fail_step

    def pivot(self, deg, speed, deadline):
        assert not self.moving
        assert not self.engine_is_listening()
        self.pivots.append(deg)
        return True

    def approach(self, cm, speed, deadline, stop_cm):
        assert not self.moving
        assert not self.engine_is_listening()
        self.approaches.append((cm, speed, deadline, stop_cm))
        self.front = max(stop_cm, self.front - cm)
        return True


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

    def make(*, front=120, fail_step=False, budget=50, choice=None, confidence=1.0,
             continuous=False, exploration=False, turns=False, chunk=None,
             battery=8, min_battery=None, trigger='audio_and_vision', stop_images=8,
             jpeg=None, no_echo_motion=False, close_debris=False):
        profile = load_profile('pi-full-check')
        cfg = profile.rover
        cfg['settle_s'] = .01
        cfg['decisions']['debris_approach']['enabled'] = close_debris
        cfg['decisions'].update(forward_budget_cm=budget, session_s=60, continuous_forward=continuous,
                                autonomous_exploration=exploration, cruise_chunk_cm=chunk,
                                min_battery_v=min_battery,
                                no_echo_camera_motion=no_echo_motion,
                                inspection_trigger=trigger,
                                stop_image_history_size=stop_images,
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
        rover = BenchRover(cfg=cfg, bus=pipeline.bus, clock=lambda: pipeline.t,
                           front=front, fail_step=fail_step)
        rover.battery = battery
        sequence = [0]

        def capture(**kwargs):
            sequence[0] += 1
            image = jpeg(sequence[0]) if jpeg is not None else b'fake-jpeg'
            return SimpleNamespace(jpeg=image, captured_monotonic=clock[0], sequence=sequence[0])

        def choose(frame, telemetry, allowed):
            rover.observations.append((dict(telemetry), list(allowed)))
            action = choice or ('FORWARD_SHORT' if 'FORWARD_SHORT' in allowed else 'HOLD')
            if callable(action):
                action = action(telemetry)
            return SimpleNamespace(action=action, probabilities={
                a: confidence if a == action else (1 - confidence) / (len(allowed) - 1)
                for a in allowed}, latency_s=0)

        controller = DecisionsPatrol(rover, pipeline, session, SimpleNamespace(), cfg,
            client=SimpleNamespace(choose=choose, close=lambda: None),
            camera=SimpleNamespace(capture=capture, close=lambda: None), arm_motion=True,
            allow_turns=turns)
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


def test_exploration_rechecks_unknown_range_and_drives_only_after_recovery(bench):
    controller, rover, tick = bench(front=-1, exploration=True, continuous=True, chunk=20)
    for _ in range(150):
        tick()
    assert controller.state in {'HOLDING', 'OBSERVING', 'DECIDING'}
    assert len(rover.observations) > 1
    assert all(allowed == ['HOLD', 'STOP_AND_LISTEN', 'INSPECT_DEBRIS'] for _, allowed in rover.observations)
    assert all(t['front_cm'] is None and t['range_valid'] is False for t, _ in rover.observations)
    assert not rover.cruises and not rover.pivots
    rover.front = 120
    for _ in range(100):
        tick()
        if rover.cruises:
            break
    assert rover.cruises == [20]
    assert controller.snapshot()['decisions']['forward_commanded_cm'] == 20


def test_missing_range_still_requires_review_outside_exploration(bench):
    controller, rover, tick = bench(front=-1)
    for _ in range(100):
        tick()
        if controller.state == 'REVIEW':
            break
    assert controller.state == 'REVIEW'
    assert not rover.observations and not rover.steps


@pytest.mark.parametrize('front', [-1, 20, 120])
def test_visual_inspection_can_stop_at_any_range_without_motion(bench, front):
    controller, rover, tick = bench(front=front, exploration=True, choice='INSPECT_DEBRIS',
                                   trigger='debris_only')
    for _ in range(100):
        tick()
        if controller.state in {'SETTLING', 'VERIFYING'}:
            break
    assert controller.state in {'SETTLING', 'VERIFYING'}
    assert not rover.steps and not rover.cruises and not rover.pivots
    assert controller.snapshot()['decisions']['last_action'] == 'INSPECT_DEBRIS'
    assert controller.snapshot()['decisions']['stop_reason'] == 'debris_detected'
    assert 'Camera flagged possible debris' in controller.snapshot()['decisions']['status_message']
    assert controller.engine.status != 'CONFIRMED'


def test_exploration_turn_stops_cruise_then_requires_a_new_observation(bench):
    controller, rover, tick = bench(continuous=True, exploration=True, turns=True, chunk=20,
        choice=lambda t: 'TURN_LEFT_SHORT' if t['moving'] or t['front_cm'] == 25 else 'FORWARD_SHORT')
    for _ in range(100):
        tick()
        if rover.cruises:
            rover.front = 25
            break
    for _ in range(150):
        tick()
        if rover.pivots:
            break
    assert rover.cruises == [20]
    assert rover.cruise_canceled and not rover.moving
    assert rover.pivots == [-10]
    assert any(not t['moving'] and t['front_cm'] == 25 for t, _ in rover.observations)
    assert controller._forward_remaining() == 30


@pytest.mark.parametrize('fault,recover', [('obstacle_too_close', True), ('range_unknown', True),
                                        ('navigation_not_configured', True),
                                        ('stall', False)])
def test_exploration_recovers_only_expected_obstacle_or_echo_faults(bench, fault, recover):
    controller, rover, tick = bench(exploration=True)
    def fail(cm, speed, deadline):
        rover.steps.append(cm)
        rover._error(fault, command='STEP')
        rover.front = -1
        return False
    rover.step = fail
    for _ in range(200):
        tick()
        if rover.steps and (controller.state == 'REVIEW' or len(rover.observations) > 2):
            break
    assert rover.steps == [3]
    assert (controller.state != 'REVIEW') is recover
    assert controller._forward_remaining() == 47
    assert not rover.moving
    configurations = [item for item in rover.trace if item['command'] == 'NAVCFG']
    assert len(configurations) == (2 if recover else 1)


def test_exploration_estop_wins_unknown_range_recovery(bench):
    controller, rover, tick = bench(front=-1, exploration=True)
    for _ in range(50):
        tick()
    assert controller.command('estop')['ok']
    rover.front = 120
    for _ in range(100):
        tick()
    assert not rover.steps and not rover.cruises and not rover.pivots
    assert controller.state == 'ESTOP'


def test_exploration_profile_has_finite_budgets_and_continuous_chunks():
    profile = load_profile('pi-explore')
    cfg = validate_decision_settings(profile.rover['decisions'])
    assert profile.rover['navigation']['sensor_port'] == 10
    assert load_profile('pi-full-check').rover['navigation']['sensor_port'] == 10
    assert cfg['autonomous_exploration'] and cfg['continuous_forward']
    assert cfg['inspection_trigger'] == 'debris_only'
    assert cfg['cruise_chunk_cm'] == 20
    assert cfg['forward_budget_cm'] == 100 and cfg['session_s'] == 120


@pytest.mark.parametrize('change', [{'autonomous_exploration': 'yes'},
                                  {'cruise_chunk_cm': 0}, {'cruise_chunk_cm': True},
                                  {'cruise_chunk_cm': float('nan')}, {'cruise_chunk_cm': 101}])
def test_exploration_rejects_invalid_mode_and_segment_settings(change):
    cfg = {**load_profile('pi-explore').rover['decisions'], **change}
    with pytest.raises(ValueError):
        validate_decision_settings(cfg)


def test_exploration_continuous_segments_keep_the_total_distance_bound(bench):
    controller, rover, tick = bench(continuous=True, exploration=True, chunk=20)
    completed_cm = 0
    for _ in range(500):
        tick()
        requested_cm = controller.snapshot()['decisions']['forward_commanded_cm']
        if requested_cm > completed_cm:
            # Let the real worker dispatch before advancing the fake clock.
            # Otherwise the test can observe a queued cruise as not yet moving.
            deadline = time.monotonic() + 1
            while sum(rover.cruises) < requested_cm and time.monotonic() < deadline:
                time.sleep(.001)
            assert sum(rover.cruises) == requested_cm
            completed_cm = requested_cm
            rover.cruise_finished.set()
        if controller.done:
            break
    assert controller.done
    assert rover.cruises == [20, 20, 10]
    assert controller.snapshot()['decisions']['forward_commanded_cm'] == 50


def test_exploration_does_not_treat_a_transport_failure_as_an_ordinary_no_echo(bench):
    controller, rover, tick = bench(exploration=True)
    rover.range = lambda port: None
    for _ in range(100):
        tick()
        if controller.state == 'REVIEW':
            break
    assert controller.state == 'REVIEW'
    assert not rover.observations and not rover.steps


def test_exploration_waits_for_battery_recovery_before_forward_travel(bench):
    controller, rover, tick = bench(exploration=True, battery=6.9, min_battery=7)
    for _ in range(100):
        tick()
        if controller.snapshot()['decisions']['reason']=='low_battery':
            break
    assert not rover.steps and not rover.cruises and not rover.pivots
    assert all(allowed == ['HOLD', 'STOP_AND_LISTEN', 'INSPECT_DEBRIS'] for _, allowed in rover.observations)
    assert controller.snapshot()['decisions']['reason'] == 'low_battery'
    rover.battery = 8
    for _ in range(100):
        tick()
        if rover.steps:
            break
    assert rover.steps


def test_generic_camera_hold_does_not_claim_debris_detection(bench):
    controller, rover, tick = bench(exploration=True, choice='HOLD')
    for _ in range(100):
        tick()
        if controller.snapshot()['decisions']['reason'] == 'model_hold':
            break
    message=controller.snapshot()['decisions']['status_message']
    assert 'Camera requested HOLD' in message
    assert 'debris' not in message.lower()
    assert not rover.steps


def test_stop_reason_is_printed_by_the_console_and_kept_in_snapshot(bench,capsys):
    from heartbeat.cli import _DialogConsole
    controller, rover, tick = bench(exploration=True, choice='INSPECT_DEBRIS')
    controller.pipeline.bus.subscribe(_DialogConsole())
    for _ in range(100):
        tick()
        if controller.state in {'SETTLING','VERIFYING'}:
            break
    assert 'Camera flagged possible debris' in capsys.readouterr().out
    assert 'Camera flagged possible debris' in controller.snapshot()['decisions']['stop_message']


@pytest.mark.parametrize('phase', ['MOVING', 'OBSERVING', 'DECIDING', 'HOLDING'])
def test_debris_only_ignores_knocks_and_distress_in_every_navigation_phase(bench,phase):
    controller, rover, tick = bench(exploration=True, continuous=True, chunk=20,
                                   trigger='debris_only')
    for _ in range(350):
        tick()
    assert rover.cruises and not rover.cruise_canceled
    controller.state=phase
    pipeline=controller.pipeline
    pipeline.bus.publish('sound',pipeline.t-.9,is_knock=True,gated=True,score=.99,classifier='clap')
    pipeline.bus.publish('audio_observation',pipeline.t,start=pipeline.t-3,label='SCREAM',
                         score=.99,possible_distress=True,matched_phrases=[],transcript='',alert=True)
    pipeline.bus.publish('error',pipeline.t,where='audio_monitor',message='Observation request timed out')
    controller._request_audio_stop({'label':'KNOCK_BURST','end':pipeline.t,'start':pipeline.t-1})
    assert controller.state==phase
    assert controller._trigger is None
    assert not rover.cruise_canceled
    assert 'STOP_AND_LISTEN' not in controller._allowed_actions(120)
    assert 'INSPECT_DEBRIS' in controller._allowed_actions(120)


def test_debris_only_still_collects_fresh_knocks_after_a_visual_debris_stop(bench):
    controller, rover, tick = bench(exploration=True, trigger='debris_only',choice='INSPECT_DEBRIS')
    for _ in range(100):
        tick()
        if controller.state=='VERIFYING':
            break
    assert controller.state=='VERIFYING'
    for _ in range(200):
        tick()
    pipeline=controller.pipeline
    pipeline.bus.publish('sound',pipeline.t-.9,is_knock=True,gated=True,score=.99,classifier='clap')
    assert controller._verified_cue['label']=='KNOCK_BURST'
    assert not rover.steps and not rover.cruises


def test_debris_only_resume_checks_camera_without_starting_an_audio_investigation(bench):
    controller, rover, tick = bench(exploration=True, trigger='debris_only',front=-1)
    for _ in range(50):
        tick()
    assert controller.command('pause')['ok']
    for _ in range(50):
        tick()
    assert controller.state=='PAUSED'
    phases=[]
    controller.pipeline.bus.subscribe(lambda e:phases.append(e.data['state']) if e.type=='patrol' else None)
    assert controller.command('resume')['ok']
    for _ in range(100):
        tick()
    assert controller.state in {'OBSERVING','DECIDING','HOLDING'}
    assert not {'SETTLING','VERIFYING','LISTENING'} & set(phases)
    assert not rover.steps


@pytest.mark.parametrize('trigger', ['invalid',None,[]])
def test_invalid_inspection_policy_is_rejected(trigger):
    cfg={**load_profile('pi-explore').rover['decisions'],'inspection_trigger':trigger}
    with pytest.raises(ValueError,match='inspection_trigger'):
        validate_decision_settings(cfg)


def test_debris_stop_freezes_the_decision_frame_once_without_bytes_in_events(bench):
    import json
    controller, rover, tick = bench(exploration=True, trigger='debris_only',
                                   choice='INSPECT_DEBRIS', jpeg=lambda n: f'frame-{n}'.encode())
    events = []
    controller.pipeline.bus.subscribe(lambda event: events.append(event.to_dict()))
    for _ in range(150):
        tick()
        if controller.state == 'VERIFYING':
            break
    assert controller.state == 'VERIFYING'
    stops = controller.snapshot()['decisions']['stop_images']
    assert len(stops) == 1
    stop = stops[0]
    assert stop['reason'] == 'debris_detected' and stop['kind'] == 'possible_debris'
    assert stop['image_url'] == f"/api/stop-images/{stop['id']}"
    assert 0 <= stop['frame_age_s'] < 1
    assert controller.stop_image(stop['id']) == f"frame-{stop['frame_sequence']}".encode()
    assert 'frame-' not in json.dumps(events)
    assert controller.stop_image('unknown') is None
    assert not rover.steps


def test_recent_stops_keep_their_own_frames_and_evict_old_images(bench):
    controller, rover, tick = bench(exploration=True, stop_images=2,
                                   jpeg=lambda n: f'frame-{n}'.encode())
    for _ in range(100):
        tick()
        stops = controller.snapshot()['decisions']['stop_images']
        if stops:
            break
    first = stops[0]
    first_jpeg = controller.stop_image(first['id'])
    assert first['kind'] == 'scene'
    assert 'debris' not in first['message'].lower()
    for _ in range(100):
        tick()
        stops = controller.snapshot()['decisions']['stop_images']
        if len(stops) == 2:
            break
    assert controller.stop_image(first['id']) == first_jpeg
    assert controller.stop_image(stops[0]['id']) != first_jpeg
    for _ in range(100):
        tick()
        stops = controller.snapshot()['decisions']['stop_images']
        if first['id'] not in {stop['id'] for stop in stops}:
            break
    assert len(stops) == 2
    assert controller.stop_image(first['id']) is None
    assert stops[0]['stop_t'] >= stops[1]['stop_t']
    for stop in stops:
        assert controller.stop_image(stop['id']) == f"frame-{stop['frame_sequence']}".encode()


def test_stationary_range_retries_do_not_fill_the_stop_gallery(bench):
    controller, rover, tick = bench(exploration=True, front=-1)
    for _ in range(150):
        tick()
    assert len(rover.observations) > 3
    stops = controller.snapshot()['decisions']['stop_images']
    assert len(stops) == 1
    assert stops[0]['reason'] == 'waiting_for_range'
    assert not rover.steps and not rover.cruises


def test_stop_before_first_camera_frame_reports_photo_unavailable(bench):
    controller, rover, tick = bench(exploration=True)
    assert controller.command('pause')['ok']
    for _ in range(50):
        tick()
        if controller.state == 'PAUSED':
            break
    assert controller.state == 'PAUSED'
    stop = controller.snapshot()['decisions']['stop_images'][0]
    assert stop['reason'] == 'operator_pause'
    assert controller.snapshot()['decisions']['status_message'] == 'Stopped: Exploration paused by operator'
    assert stop['image_url'] is None and stop['frame_age_s'] is None
    assert controller.stop_image(stop['id']) is None


@pytest.mark.parametrize('limit', [0, -1, True, 1.5, None])
def test_stop_image_history_limit_must_be_a_positive_integer(limit):
    cfg = {**load_profile('pi-explore').rover['decisions'], 'stop_image_history_size': limit}
    with pytest.raises(ValueError, match='stop_image_history_size'):
        validate_decision_settings(cfg)


def test_no_echo_camera_mode_can_start_and_continue_cruise_without_inventing_distance(bench):
    controller, rover, tick = bench(front=-1, exploration=True, continuous=True,
                                   chunk=20, no_echo_motion=True, trigger='debris_only')
    for _ in range(150):
        tick()
    assert rover.cruises == [20] and not rover.cruise_canceled
    assert all(t['front_cm'] is None and t['range_valid'] is False and t['no_echo'] is True
               for t, _ in rover.observations)
    assert all('FORWARD_SHORT' in allowed for _, allowed in rover.observations)
    assert controller.snapshot()['decisions']['camera_only_motion'] is True
    assert controller.motion_allowed
    assert any(e['command'] == 'NOECHO' and e['args'] == [1] for e in rover.trace)
    assert controller.command('estop')['ok']
    for _ in range(30):
        tick()
    assert controller.state == 'ESTOP' and not rover.moving


def test_no_echo_turn_still_requires_turn_opt_in(bench):
    controller, rover, tick = bench(front=-1, exploration=True, turns=True,
        no_echo_motion=True, choice='TURN_LEFT_SHORT')
    for _ in range(100):
        tick()
        if rover.pivots:
            break
    assert rover.pivots == [-10]
    controller.allow_turns = False
    assert 'TURN_LEFT_SHORT' not in controller._allowed_actions(None)
    assert 'TURN_RIGHT_SHORT' not in controller._allowed_actions(None)


@pytest.mark.parametrize('front', [10, 30, 40])
def test_no_echo_camera_mode_never_overrides_a_valid_close_distance(bench, front):
    controller, rover, tick = bench(front=front, exploration=True, no_echo_motion=True,
                                   trigger='debris_only')
    for _ in range(100):
        tick()
    assert not rover.steps and not rover.cruises
    assert all('FORWARD_SHORT' not in allowed for _, allowed in rover.observations)
    assert controller.snapshot()['decisions']['camera_only_motion'] is False


@pytest.mark.parametrize('fault', ['silence', 'malformed', 'moving_mismatch'])
def test_no_echo_camera_mode_requires_a_real_fresh_sensor_reply(bench, fault):
    controller, rover, tick = bench(front=-1, exploration=True, no_echo_motion=True)
    if fault == 'silence':
        rover.range = lambda port: None
    elif fault == 'malformed':
        rover.range = lambda port: {'valid': False, 'cm': 999}
    else:
        rover.status = lambda: {'moving': True, 'batt': 8}
    for _ in range(100):
        tick()
        if controller.state == 'REVIEW':
            break
    assert controller.state == 'REVIEW'
    assert not rover.steps and not rover.cruises and not rover.observations


def test_no_echo_camera_mode_stops_on_stale_streamed_telemetry(bench):
    controller, rover, tick = bench(front=-1, exploration=True, continuous=True,
                                   chunk=20, no_echo_motion=True)
    for _ in range(100):
        tick()
        if rover.cruises:
            break
    assert rover.cruises == [20]
    rover.cruise_observation = lambda: {'valid': False, 'cm': -1, 'moving': True, 'at': 0}
    for _ in range(100):
        tick()
        if rover.cruise_canceled:
            break
    assert rover.cruise_canceled and not rover.moving


def test_no_echo_camera_mode_cannot_use_old_firmware(bench):
    controller, rover, tick = bench(front=-1, exploration=True, no_echo_motion=True)
    rover.capabilities = lambda: {'guarded': True, 'cruise': True}
    for _ in range(100):
        tick()
        if controller.state == 'REVIEW':
            break
    assert controller.state == 'REVIEW'
    assert not rover.steps and not rover.cruises
    assert 'NOECHO firmware' in controller.snapshot()['decisions']['reason']


@pytest.mark.parametrize('failure', ['camera', 'policy_ack'])
def test_no_echo_camera_mode_requires_working_camera_and_acknowledged_policy(bench, failure):
    controller, rover, tick = bench(front=-1, exploration=True, no_echo_motion=True)
    if failure == 'camera':
        def broken_capture(**kwargs):
            raise RuntimeError('camera failed')
        controller.camera.capture = broken_capture
    else:
        rover.set_no_echo_motion = lambda enabled: False
    for _ in range(100):
        tick()
        if controller.state == 'REVIEW':
            break
    assert controller.state == 'REVIEW'
    assert not rover.steps and not rover.cruises and not rover.pivots


def test_camera_stop_reapplies_ram_navigation_policy_before_the_next_move(bench):
    def choice(telemetry):
        return 'HOLD' if telemetry['moving'] and len(rover.cruises) == 1 else 'FORWARD_SHORT'
    controller, rover, tick = bench(front=-1, exploration=True, continuous=True,
        chunk=20, no_echo_motion=True, trigger='debris_only', choice=choice)
    ram = {'navigation': False, 'noecho': False}
    configure = rover.configure_navigation
    policy = rover.set_no_echo_motion
    cruise = rover.cruise
    stop = rover.stop

    def configure_ram(settings):
        ram['navigation'] = True
        return configure(settings)

    def policy_ram(enabled):
        assert ram['navigation']
        ram['noecho'] = enabled
        return policy(enabled)

    def guarded_cruise(*args):
        assert ram['navigation'] and ram['noecho']
        return cruise(*args)

    def reset_after_interrupt():
        was_moving = rover.moving
        result = stop()
        if was_moving:
            ram.update(navigation=False, noecho=False)
        return result

    rover.configure_navigation = configure_ram
    rover.set_no_echo_motion = policy_ram
    rover.cruise = guarded_cruise
    rover.stop = reset_after_interrupt
    for _ in range(300):
        tick()
        if len(rover.cruises) == 2:
            break
    assert rover.cruises == [20, 20]
    assert sum(e['command'] == 'NOECHO' for e in rover.trace) == 2
    assert ram['navigation'] and ram['noecho']


@pytest.mark.parametrize('changes', [{'no_echo_camera_motion': 'yes'},
                                     {'no_echo_camera_motion': None},
                                     {'no_echo_camera_motion': True, 'autonomous_exploration': False}])
def test_no_echo_camera_policy_is_explicit_and_requires_exploration(changes):
    cfg = {**load_profile('pi-explore').rover['decisions'], **changes}
    with pytest.raises(ValueError, match='no_echo_camera_motion'):
        validate_decision_settings(cfg)


def test_debris_is_approached_before_listening_and_each_step_gets_a_fresh_view(bench):
    controller, rover, tick = bench(front=9, exploration=True, close_debris=True,
        trigger='debris_only', choice=lambda t: 'INSPECT_DEBRIS' if t['front_cm'] <= 3.5 else 'APPROACH_DEBRIS')
    for _ in range(200):
        tick()
        if controller.state == 'VERIFYING':
            break
    assert controller.state == 'VERIFYING'
    assert rover.approaches == [(2, 35, 1000, 3)] * 3
    assert [t['front_cm'] for t, _ in rover.observations] == [9, 7, 5, 3]
    assert controller.snapshot()['decisions']['forward_commanded_cm'] == 6
    assert controller.snapshot()['decisions']['stop_reason'] == 'debris_arrived'
    assert not rover.steps and not rover.cruises


@pytest.mark.parametrize('front', [120, 10, 4, -1])
def test_close_debris_policy_never_offers_listening_for_far_or_unknown_distance(bench, front):
    controller, rover, tick = bench(front=front, exploration=True, close_debris=True,
                                   trigger='debris_only', no_echo_motion=True, choice='HOLD')
    for _ in range(100):
        tick()
    assert rover.observations
    assert all('INSPECT_DEBRIS' not in allowed and 'STOP_AND_LISTEN' not in allowed
               for _, allowed in rover.observations)
    assert controller.state in {'HOLDING', 'OBSERVING', 'DECIDING'}
    assert not rover.approaches
    if front == -1:
        assert all('APPROACH_DEBRIS' not in allowed for _, allowed in rover.observations)


@pytest.mark.parametrize('front', [2, 3, 3.4])
def test_only_close_visible_debris_starts_listening_with_no_extra_creep(bench, front):
    controller, rover, tick = bench(front=front, exploration=True, close_debris=True,
                                   trigger='debris_only', choice='INSPECT_DEBRIS')
    for _ in range(100):
        tick()
        if controller.state == 'VERIFYING':
            break
    assert controller.state == 'VERIFYING'
    assert not rover.approaches and not rover.steps and not rover.cruises
    assert controller.snapshot()['decisions']['stop_images'][0]['kind'] == 'possible_debris'


def test_approach_does_not_listen_if_the_camera_loses_the_debris(bench):
    def choose(t):
        return 'APPROACH_DEBRIS' if t['front_cm'] == 5 else 'HOLD'
    controller, rover, tick = bench(front=5, exploration=True, close_debris=True,
                                   trigger='debris_only', choice=choose)
    for _ in range(100):
        tick()
    assert len(rover.approaches) == 1 and rover.front == 3
    assert controller.state in {'HOLDING', 'OBSERVING', 'DECIDING'}
    assert not controller._debris_inspection_active


def test_distant_model_listen_choice_is_rejected_without_approaching_or_listening(bench):
    controller, rover, tick = bench(front=80, exploration=True, close_debris=True,
                                   trigger='debris_only', choice='INSPECT_DEBRIS')
    for _ in range(100):
        tick()
        if controller.state == 'REVIEW':
            break
    assert controller.state == 'REVIEW'
    assert not rover.approaches and not controller._debris_inspection_active


def test_close_approach_respects_remaining_forward_budget(bench):
    controller, rover, tick = bench(front=15, exploration=True, close_debris=True,
                                   trigger='debris_only', choice='APPROACH_DEBRIS', budget=3)
    for _ in range(150):
        tick()
        if controller.done:
            break
    assert controller.done and controller.state == 'DONE'
    assert [a[0] for a in rover.approaches] == [2, 1]
    assert not controller._debris_inspection_active


def test_close_approach_requires_new_firmware(bench):
    controller, rover, tick = bench(front=10, exploration=True, close_debris=True, trigger='debris_only')
    rover.capabilities = lambda: {'guarded': True, 'cruise': True, 'noecho': True}
    for _ in range(100):
        tick()
        if controller.state == 'REVIEW':
            break
    assert controller.state == 'REVIEW' and not rover.approaches
    assert 'APPROACH firmware' in controller.snapshot()['decisions']['reason']


def test_close_approach_stops_cruise_before_dispatching_a_creep(bench):
    def choose(t):
        return 'FORWARD_SHORT' if not rover.cruises else 'APPROACH_DEBRIS'
    controller, rover, tick = bench(front=80, continuous=True, exploration=True,
        close_debris=True, trigger='debris_only', choice=choose, chunk=20)
    for _ in range(150):
        tick()
        if rover.approaches:
            break
    assert rover.cruises == [20]
    assert rover.cruise_canceled and rover.approaches
    assert 'close approach' in controller.snapshot()['decisions']['stop_message']


def test_missing_echo_during_close_approach_cannot_start_an_ordinary_cruise(bench):
    def choose(t):
        return 'HOLD' if t['front_cm'] is None else 'APPROACH_DEBRIS'
    controller, rover, tick = bench(front=10, exploration=True, continuous=True,
        close_debris=True, trigger='debris_only', no_echo_motion=True, choice=choose)
    for _ in range(100):
        tick()
        if rover.approaches:
            break
    assert len(rover.approaches) == 1
    rover.front = -1
    for _ in range(100):
        tick()
    unknown = [allowed for t, allowed in rover.observations if t['front_cm'] is None]
    assert unknown and all(allowed == ['HOLD'] for allowed in unknown)
    assert len(rover.approaches) == 1 and not rover.cruises
    assert controller.state in {'HOLDING', 'OBSERVING', 'DECIDING'}
    assert not controller._debris_inspection_active


@pytest.mark.parametrize('change', [{'enabled': 'yes'}, {'stop_cm': 0}, {'stop_cm': 2},
    {'stop_cm': 16}, {'step_cm': 0}, {'step_cm': 6}, {'speed_pct': 36}, {'max_ms': 2001},
    {'max_ms': True}, {'arrival_tolerance_cm': 2}])
def test_bad_close_approach_settings_are_rejected(change):
    cfg = load_profile('pi-explore').rover['decisions']
    cfg['debris_approach'].update(change)
    with pytest.raises(ValueError, match='debris_approach'):
        validate_decision_settings(cfg)
