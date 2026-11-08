"""CLAP-05: controlled futures and fake requests, never hardware or a network."""
from concurrent.futures import Future
from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pytest

from heartbeat.audio import AudioBlock
from heartbeat.classifiers.clap import ClapClassifier, ClapIdentityError
from heartbeat.clap_settings import validate_clap
from heartbeat.config import load_profile
from heartbeat.events import EventBus
from heartbeat.runner import Pipeline, run_simulation
from test_clap import identity, onset, response


class Executor:
    def __init__(self):
        self.jobs = []

    def submit(self, fn, *args):
        future = Future()
        self.jobs.append((future, fn, args))
        return future

    def complete(self, index=0):
        future, fn, args = self.jobs[index]
        try:
            value = fn(*args)
        except Exception as exc:
            future.set_exception(exc)
        else:
            future.set_result(value)


def setup(**changes):
    profile = load_profile('clap')
    profile = replace(profile, classifier=replace(profile.classifier,
                      clap={**profile.classifier.clap, **changes}))
    calls = []
    def request(path, body):
        calls.append(path)
        return identity(profile.classifier.clap) if body is None else response(profile.classifier.clap, body)
    classifier = ClapClassifier(profile.classifier, request=request)
    bus, events = EventBus(), []
    bus.subscribe(events.append)
    pipeline = Pipeline(SimpleNamespace(close=lambda: None), SimpleNamespace(), classifier, bus, profile)
    pipeline._inference = Executor()
    pipeline._health_executor = Executor()
    return pipeline, events, calls


def sounds(events):
    return [e for e in events if e.type == 'sound']


def codes(events, code):
    return [e for e in events if e.data.get('code') == code]


def test_timeout_classifies_every_pending_and_new_onset_once():
    pipeline, events, _ = setup()
    for t in [.1, .2, .3]:
        pipeline._sound(onset(t))
    pipeline._inference.jobs[0][0].set_exception(TimeoutError('remote timed out'))
    pipeline.t = 1.2
    pipeline._poll_inference()
    pipeline._sound(onset(1.3))
    pipeline._fail_inference(ConnectionError('still offline'))
    assert [e.t for e in sounds(events)] == [.1, .2, .3, 1.3]
    assert {e.data['classifier'] for e in sounds(events)} == {'heuristic'}
    assert not any(e.data['is_knock'] for e in sounds(events))  # Silent clips were actually classified.
    assert len(codes(events, 'clap_degraded')) == 1
    assert codes(events, 'clap_degraded')[0].data['severity'] == 'warning'
    assert not pipeline.inference_failed and not pipeline._stop.is_set()


def test_queue_overflow_uses_individual_fallback_and_preserves_order():
    pipeline, events, _ = setup(max_pending=1, max_lag_s=100)
    pipeline._sound(onset(.1))
    for t in [.2, .3, .4, 10.3]:
        pipeline.t = t
        pipeline._sound(onset(t))
    assert len(pipeline._inference.jobs) == 1
    assert len(codes(events, 'clap_busy')) == 2
    assert codes(events, 'clap_busy')[-1].data['count'] == 4
    assert not codes(events, 'clap_degraded') and not pipeline.inference_failed
    pipeline._inference.complete()
    pipeline._poll_inference()
    assert [e.t for e in sounds(events)] == [.1, .2, .3, .4, 10.3]
    assert [e.data['classifier'] for e in sounds(events)] == ['clap'] + ['heuristic'] * 4


def test_lag_degrades_and_late_inflight_result_is_ignored():
    pipeline, events, _ = setup()
    pipeline._sound(onset(.1))
    old = pipeline._inference.jobs[0][0]
    old.set_running_or_notify_cancel()
    pipeline.t = 3
    pipeline._poll_inference()
    assert pipeline._clap_degraded
    pipeline._inference.complete()
    pipeline._poll_inference()
    assert len(sounds(events)) == 1 and sounds(events)[0].data['classifier'] == 'heuristic'


def test_recovery_is_nonblocking_and_checks_identity_before_using_clap():
    pipeline, events, calls = setup(retry_s=10)
    pipeline.t = 1
    pipeline._fail_inference(ConnectionError('offline'))
    pipeline.t = 10
    pipeline._poll_health()
    assert not pipeline._health_executor.jobs
    pipeline.t = 11
    pipeline._poll_health()
    pipeline._sound(onset(11))  # Pending health request never blocks fallback.
    assert sounds(events)[0].data['classifier'] == 'heuristic'
    pipeline._health_executor.jobs[0][0].set_exception(ClapIdentityError('wrong descriptions'))
    pipeline._poll_health()
    assert pipeline._clap_degraded
    pipeline.t = 21
    pipeline._poll_health()
    pipeline._health_executor.complete(1)
    pipeline._poll_health()
    assert not pipeline._clap_degraded and len(codes(events, 'clap_restored')) == 1
    pipeline._sound(onset(21))
    pipeline._inference.complete()
    pipeline._poll_inference()
    assert [e.data['classifier'] for e in sounds(events)] == ['heuristic', 'clap']
    assert calls == ['/health', '/health', '/classify']


@pytest.mark.parametrize('policy', ['fallback', 'stop'])
def test_twelve_gated_onsets_during_four_second_prompt_never_call_clap(policy):
    pipeline, events, calls = setup(on_failure=policy)
    for i in range(12):
        pipeline.t = i / 3
        pipeline._sound(onset(pipeline.t, gated=True))
    assert calls == ['/health'] and not pipeline._inference.jobs
    assert len(sounds(events)) == 12
    assert all(e.data['gated'] and e.data['classifier'] == 'heuristic' for e in sounds(events))
    assert not codes(events, 'clap_degraded') and not pipeline._stop.is_set()


def test_stop_policy_keeps_queue_failure_fatal():
    pipeline, events, _ = setup(on_failure='stop', max_pending=1)
    pipeline._sound(onset(.1))
    pipeline._sound(onset(.2))
    assert pipeline.inference_failed and pipeline._stop.is_set()
    assert not sounds(events) and not codes(events, 'clap_degraded')


@pytest.mark.parametrize('change', [{'on_failure': 'ignore'}, {'fallback': 'clap'}, {'fallback': 'none'},
                                    {'fallback': ''}, {'retry_s': 0}, {'retry_s': True}, {'retry_s': float('nan')}])
def test_bad_recovery_settings_rejected(change):
    with pytest.raises(ValueError):
        validate_clap({**load_profile('clap').classifier.clap, **change})


def test_old_profiles_get_fallback_defaults_and_unknown_registry_name_is_rejected():
    cfg = load_profile('clap-knocks').classifier
    settings = validate_clap(cfg.clap)
    assert (settings['on_failure'], settings['fallback'], settings['retry_s']) == ('fallback', 'heuristic', 10)
    with pytest.raises(ValueError, match='registered'):
        ClapClassifier(replace(cfg, clap={**cfg.clap, 'fallback': 'missing'}), request=lambda *args: None)


def test_service_down_at_start_warns_once_and_processes_audio(monkeypatch):
    profile = load_profile('clap')
    def offline(*args):
        raise ConnectionRefusedError('offline')
    classifier = ClapClassifier(profile.classifier, request=offline)
    bus, events = EventBus(), []
    bus.subscribe(events.append)
    from test_clap import Detector
    class Source:
        def __iter__(self):
            yield AudioBlock(0, np.zeros(256, np.float32))
        def close(self):
            pass
    pipeline = Pipeline(Source(), Detector(), classifier, bus, profile)
    pipeline.run()
    assert pipeline.finished and not pipeline.inference_failed
    assert len(codes(events, 'clap_degraded')) == 1
    assert sounds(events)[0].data['classifier'] == 'heuristic'


def test_wrong_identity_is_a_hard_startup_error():
    profile = load_profile('clap')
    with pytest.raises(ClapIdentityError):
        ClapClassifier(profile.classifier, request=lambda *args: {**identity(profile.classifier.clap), 'model': 'wrong'})


def test_full_simulated_dialog_survives_clap_failing_halfway(monkeypatch):
    profile = load_profile('clap')
    calls = [0]
    def request(self, path, body):
        if body is None:
            if calls[0] >= 4:
                raise ConnectionRefusedError('service offline')
            return identity(profile.classifier.clap)
        calls[0] += 1
        if calls[0] >= 4:
            raise TimeoutError('Wi-Fi interrupted')
        return response(profile.classifier.clap, body)
    monkeypatch.setattr(ClapClassifier, '_http', request)
    result = run_simulation(profile, people=2, can_move='yes', hurt='no', seed=1)
    assert result.snapshot['status'] == 'CONFIRMED'
    assert result.snapshot['answers'] == {'q_people': 2, 'q_move': 'yes', 'q_hurt': 'no'}
    assert len(codes(result.events, 'clap_degraded')) == 1
    assert {e.data['classifier'] for e in sounds(result.events)} == {'clap', 'heuristic'}
