from dataclasses import dataclass, replace
import json
from pathlib import Path
import threading
from types import SimpleNamespace

import numpy as np
import pytest

from heartbeat.audio import AudioBlock
from heartbeat.config import load_profile
from heartbeat.events import EventBus
from heartbeat.runner import DialogSession, Pipeline, SimulationSource, run_simulation


def baseline_profile():
    profile = load_profile()
    return replace(profile, classifier=replace(profile.classifier, name="none"))


def classifier_profile(name):
    profile = load_profile()
    profile = replace(profile, classifier=replace(profile.classifier, name=name))
    if name == "head":
        pytest.importorskip("sklearn")
        from heartbeat.classifiers.head import HeadClassifier

        cfg = profile.classifier
        if not Path(cfg.head_path).is_file() or not (Path(cfg.model_dir) / "yamnet.tflite").is_file():
            pytest.skip("local head/YAMNet absent; tests never download models")
        try:
            head = HeadClassifier(cfg)
        except RuntimeError as exc:
            if "No CPU TFLite interpreter" in str(exc):
                pytest.skip(str(exc))
            raise
        assert head.name == "head"
    return profile


def test_simulated_survivor_confirms_and_answers():
    result = run_simulation(baseline_profile(), people=2, can_move="yes", hurt="no", seed=1)
    assert result.confirmed_t is not None and result.confirmed_t <= 60
    assert result.t <= 60
    assert result.snapshot["state"] == "CONFIRMED"
    assert result.snapshot["answers"] == {"q_people": 2, "q_move": "yes", "q_hurt": "no"}
    matched = [event.data for event in result.events
               if event.type == "round_result" and event.data["matched"]]
    assert len({item["expected"] for item in matched}) >= 2
    assert result.snapshot["combined_p"] <= baseline_profile().dialog["p_confirm"]
    assert not [event for event in result.events if event.type == "error"]
    for event in result.events:
        json.dumps(event.to_dict(), allow_nan=False)


@pytest.mark.parametrize("tired", [False, True], ids=["human-speed", "tired"])
@pytest.mark.parametrize("classifier_name", ["heuristic", "head"])
def test_survivor_confirms_and_answers_at_human_speed(tired, classifier_name):
    profile = classifier_profile(classifier_name)
    if tired:
        profile = replace(profile, prompter=dict(
            profile.prompter, fake_knock_interval_s=0.8, fake_jitter_s=0.15,
            fake_reply_delay_min_s=2.5, fake_reply_delay_max_s=2.5,
        ))
    else:
        assert profile.prompter["fake_knock_interval_s"] == 0.55
        assert profile.prompter["fake_jitter_s"] == 0.08
    result = run_simulation(profile, people=2, can_move="yes", hurt="no", seed=1,
                            duration_s=90 if tired else 60)
    assert result.confirmed_t is not None and result.confirmed_t <= 60
    assert result.snapshot["status"] == "CONFIRMED"
    assert result.snapshot["answers"] == {"q_people": 2, "q_move": "yes", "q_hurt": "no"}
    assert not [event for event in result.events if event.type == "error"]
    assert {event.data["classifier"] for event in result.events if event.type == "sound"} == {classifier_name}


@pytest.mark.parametrize("classifier_name", ["none", "heuristic", "head"])
def test_no_survivor_noise_never_confirms_for_ten_minutes(classifier_name):
    profile = classifier_profile(classifier_name)
    result = run_simulation(profile, no_survivor=True, noise=True, seed=1)
    assert result.t == 600.0
    assert result.confirmed_t is None
    assert not [event for event in result.events
                if event.type == "state" and event.data.get("status") == "CONFIRMED"]
    assert any(event.type == "sound" for event in result.events)
    assert not [event for event in result.events if event.type == "error"]
    assert {event.data["classifier"] for event in result.events if event.type == "sound"} == {classifier_name}
    for event in result.events:
        json.dumps(event.to_dict(), allow_nan=False)


@pytest.mark.parametrize("noisy", [False, True], ids=["quiet", "noisy"])
def test_background_evaluator_counts_pre_listen_as_started_and_dispatches_ticks(noisy):
    from heartbeat.classify import Detection
    from tools.eval_background import simulate_dialog_events

    profile = baseline_profile()
    profile = replace(profile, classifier=replace(
        profile.classifier, evaluation=dict(profile.classifier.evaluation, challenge_interval_s=1.0),
    ))
    detections = [Detection(
        t=t, peak=0.8, snr_db=20.0, gated=False, clip=np.empty(0, dtype=np.float32),
        score=1.0, is_knock=True, classifier="scripted_noise", top=[],
    ) for t in (1.0, 2.0)] if noisy else []
    events = []
    result = simulate_dialog_events(detections, 6.0, profile,
                                    emit=lambda kind, t, data: events.append((kind, t, data)))
    assert result["challenge_attempts"] == 6
    assert result["challenges_started"] == 1
    assert result["challenges_rejected"] == 5
    assert result["false_confirmations"] == 0
    prompts = [t for kind, t, data in events if kind == "prompt_start"]
    if noisy:
        assert prompts == []
        assert any(kind == "state" and data["state"] == "TOO_NOISY" for kind, t, data in events)
    else:
        assert prompts == [profile.dialog["pre_listen_s"]]
        assert result["prompt_batches"] == 1


def test_background_evaluator_challenge_during_cooldown_replaces_old_deadline():
    from tools.eval_background import simulate_dialog_events

    profile = baseline_profile()
    profile = replace(
        profile, dialog=dict(profile.dialog, max_rounds=1, max_empty_windows=1),
        classifier=replace(profile.classifier, evaluation=dict(
            profile.classifier.evaluation, challenge_interval_s=20.0,
        )),
    )
    result = simulate_dialog_events([], 30.0, profile)
    assert result["challenge_attempts"] == result["challenges_started"] == 2
    assert result["challenges_rejected"] == 0
    assert result["prompt_batches"] == 2
    assert result["false_confirmations"] == 0


def test_fast_simulation_repeats_independent_of_worker_timing():
    results = [run_simulation(baseline_profile(), seed=7, hurt="no") for _ in range(2)]
    normalized = [[(event.type, event.t, event.data) for event in result.events] for result in results]
    assert normalized[0] == normalized[1]
    assert results[0].snapshot == results[1].snapshot


class Blocks:
    def __init__(self, count=20):
        self.count = count
        self.closed = False

    def __iter__(self):
        for i in range(self.count):
            if self.closed:
                return
            yield AudioBlock(i * 256 / 16000, np.zeros(256, dtype=np.float32))

    def close(self):
        self.closed = True


class Detector:
    level_db = -50.0
    floor_db = -50.0

    def __init__(self, calls):
        self.calls = calls

    def process(self, block):
        self.calls.append(("detector", threading.get_ident()))
        return []

    def flush(self):
        return []

    def gate(self, start, end):
        self.calls.append(("gate", threading.get_ident()))


@dataclass(frozen=True)
class TestAction:
    __test__ = False
    key: str = "test"


class RecordingEngine:
    def __init__(self, profile, emit, rng_seed):
        self.calls = []
        self.completions = []
        self.ticks = []
        self._record("init")

    def _record(self, kind):
        self.calls.append((kind, threading.get_ident()))

    def command(self, name, t, **args):
        self._record(name)
        return [TestAction()] if name == "challenge" else []

    def gate_until(self, t):
        self._record("gate_until")

    def on_sound(self, det):
        self._record("sound")
        return []

    def tick(self, t):
        self._record("tick")
        self.ticks.append(t)
        return []

    def on_prompts_finished(self, t):
        self._record("finished")
        self.completions.append(t)
        return []


class VirtualPrompter:
    virtual = True

    def __init__(self):
        self.threads = []

    def run(self, action):
        self.threads.append(threading.get_ident())

    def duration(self, action):
        return 0.04

    def respond(self, actions, t):
        return []

    def close(self):
        pass


def test_queued_commands_engine_and_gates_use_processing_thread():
    calls = []
    profile = baseline_profile()
    pipeline = Pipeline(Blocks(), Detector(calls), None, EventBus(), profile)
    prompter = VirtualPrompter()
    session = DialogSession(pipeline, prompter, engine_factory=RecordingEngine)
    session.command("challenge")
    queued_threads = []
    pipeline.call_soon(lambda: queued_threads.append(threading.get_ident()))
    try:
        pipeline.run()
    finally:
        session.close()
    processing_ids = {thread for _, thread in calls + session.engine.calls}
    assert len(processing_ids) == 1
    assert threading.get_ident() not in processing_ids
    assert set(queued_threads) == processing_ids
    assert len(set(prompter.threads)) == 1
    assert not processing_ids.intersection(prompter.threads)
    assert len(session.engine.completions) == 1
    assert session.engine.ticks[-1] == pipeline.t  # EOF catches up after flush.


def test_reset_invalidates_old_prompt_completion_and_still_closes_gate():
    calls = []
    bus = EventBus()
    events = []
    bus.subscribe(events.append)
    pipeline = Pipeline(Blocks(), Detector(calls), None, bus, baseline_profile())
    session = DialogSession(pipeline, VirtualPrompter(), engine_factory=RecordingEngine)
    session.command("challenge")
    session.command("reset")
    try:
        pipeline.run()
    finally:
        session.close()
    assert session.engine.completions == []
    assert len([event for event in events if event.type == "prompt_start"]) == 1
    assert len([event for event in events if event.type == "prompt_end"]) == 1
    assert len([name for name, _ in calls if name == "gate"]) == 2


def test_clip_latency_watermark_delivers_late_answer_before_window_closes():
    class WindowEngine(RecordingEngine):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.deadline = None
            self.heard = []
            self.closed = False

        def on_prompts_finished(self, t):
            self.deadline = t + 1.0
            return super().on_prompts_finished(t)

        def on_sound(self, det):
            if not self.closed and det.t <= self.deadline:
                self.heard.append(det.t)
            return []

        def tick(self, t):
            if self.deadline is not None and t >= self.deadline:
                self.closed = True
            return super().tick(t)

    class DelayedDetector(Detector):
        emitted = False

        def process(self, block):
            if not self.emitted and block.t0 + len(block.x) / 16000 >= 1.032 + 0.875:
                self.emitted = True
                return [SimpleNamespace(
                    t=1.032, peak=0.5, snr_db=20.0, gated=False,
                    score=1.0, is_knock=True, classifier="none", top=[],
                    clip=np.zeros(15600, dtype=np.float32),
                )]
            return []

    detector = DelayedDetector([])
    classifier = SimpleNamespace(classify=lambda onset: onset)
    pipeline = Pipeline(Blocks(150), detector, classifier, EventBus(), baseline_profile())
    session = DialogSession(pipeline, VirtualPrompter(), engine_factory=WindowEngine)
    session.command("challenge")
    try:
        pipeline.run()
    finally:
        session.close()
    assert session.engine.closed
    assert session.engine.heard == [1.032]


def test_self_prompt_knock_is_logged_and_gated():
    from heartbeat.classify import make_classifier
    from heartbeat.onset import OnsetDetector

    class SlowVirtual(VirtualPrompter):
        def duration(self, action):
            return 0.5

    profile = baseline_profile()
    source = SimulationSource(profile, duration_s=2.0, no_survivor=True)
    bus = EventBus()
    events = []
    bus.subscribe(events.append)

    def inject_own_knock(event):
        if event.type == "prompt_start":
            source.schedule(event.t + 0.1, "knock", profile.prompter["fake_knock_gain_db"])

    bus.subscribe(inject_own_knock)
    pipeline = Pipeline(source, OnsetDetector(profile.onset), make_classifier(profile), bus, profile)
    session = DialogSession(pipeline, SlowVirtual(), engine_factory=RecordingEngine)
    session.command("challenge")
    try:
        pipeline.run()
    finally:
        session.close()
    sounds = [event for event in events if event.type == "sound"]
    assert sounds and all(event.data["gated"] for event in sounds)
    assert all(event._clip is not None for event in sounds)



# Hardware-free P03 prompter tests; append to tests/test_sim.py.
import io as _prompter_io
import json as _prompter_json
from types import SimpleNamespace as _PrompterNamespace

import numpy as _prompter_np
import pytest as _prompter_pytest
import soundfile as _prompter_sf

from heartbeat.dialog import PromptKnocks, PromptVoice
from heartbeat.events import EventBus
from heartbeat.prompter import (
    CompositePrompter, FakePrompter, ManualPrompter, PrompterConfig,
    SerialKnocker, SpeakerPrompter, make_prompter,
)


class _PrompterClock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class _PrompterManual:
    def __init__(self):
        self.actions = []

    def run(self, action):
        self.actions.append(action)

    def close(self):
        pass


class _PrompterPort:
    def __init__(self, clock, *, reply=True, ready=True, reject=False):
        self.clock = clock
        self.pending = bytearray(b'READY knocker v1 rest=60 strike=95 hold=70\n' if ready else b'')
        self.reply = reply
        self.reject = reject
        self.is_open = True
        self.writes = []

    def read(self, size):
        self.clock.now += 0.001 if self.pending else 0.02
        result = bytes(self.pending[:size])
        del self.pending[:size]
        return result

    def write(self, payload):
        self.writes.append(payload)
        if self.reply:
            if self.reject:
                self.pending.extend(b'ERR jammed\n')
            elif payload == b'PING\n':
                self.pending.extend(b'PONG knocker v1\n')
            else:
                self.pending.extend(b'OK K ' + payload.split()[1] + b'\n')
        return len(payload)

    def close(self):
        self.is_open = False


def test_prompter_config_rejects_unknown_and_invalid_fields():
    with _prompter_pytest.raises(ValueError, match='Unknown prompter'):
        PrompterConfig.from_dict({'typo': 1})
    with _prompter_pytest.raises(ValueError, match='positive'):
        PrompterConfig.from_dict({'serial_read_poll_s': 0})
    with _prompter_pytest.raises(ValueError, match='knock order'):
        PrompterConfig.from_dict({'fake_jitter_s': 1})
    with _prompter_pytest.raises(ValueError, match='finite'):
        PrompterConfig.from_dict({'gate_tail_s': float('nan')})
    with _prompter_pytest.raises(ValueError, match='audio sample'):
        PrompterConfig.from_dict({'sim_chunk_s': 0.000001})
    with _prompter_pytest.raises(ValueError, match='gate_tail_s'):
        PrompterConfig.from_dict({'fake_reply_delay_min_s': 0.1})


def test_prompter_manual_waits_and_emits_only_streamtime():
    bus, events, sleeps = EventBus(), [], []
    bus.subscribe(events.append)
    output = _prompter_io.StringIO()
    manual = ManualPrompter(bus, clock=lambda: 42.25, sleep=sleeps.append, stream=output)
    manual.run(PromptKnocks(3, 0.5))
    assert output.getvalue().strip() == 'OPERATOR: knock 3 times now'
    assert sleeps == [2.0]
    assert events[0].type == 'operator'
    assert events[0].t == 42.25
    _prompter_json.dumps(events[0].to_dict(), allow_nan=False)


def test_prompter_serial_ack_and_reconnect_after_error():
    clock, fallback = _PrompterClock(), _PrompterManual()
    ports = [_PrompterPort(clock, reject=True), _PrompterPort(clock)]
    opened, settings, events = [], [], []
    bus = EventBus()
    bus.subscribe(events.append)

    def factory(port, **kwargs):
        opened.append(port)
        settings.append(kwargs)
        return ports[len(opened) - 1]

    knocker = SerialKnocker('/dev/fake', bus=bus, clock=lambda: 7.5,
                            serial_factory=factory, monotonic=clock.monotonic, manual=fallback)
    action = PromptKnocks(3, 0.5)
    knocker.run(action)
    assert fallback.actions == [action]
    assert not ports[0].is_open
    knocker.run(action)
    assert opened == ['/dev/fake', '/dev/fake']
    assert ports[1].writes == [b'K 3 500\n']
    assert settings[0]['baudrate'] == 115200
    assert settings[0]['timeout'] == PrompterConfig.from_dict().serial_read_poll_s
    assert events[0].type == 'error'
    assert events[0].t == 7.5
    assert events[0].data['code'] == 'knocker_prompt_failed'
    knocker.close()
    assert not ports[1].is_open


def test_prompter_serial_timeout_is_bounded_and_falls_back():
    clock, fallback = _PrompterClock(), _PrompterManual()
    port = _PrompterPort(clock, ready=False, reply=False)
    knocker = SerialKnocker('/dev/fake', serial_factory=lambda *a, **kw: port,
                            monotonic=clock.monotonic, manual=fallback)
    action = PromptKnocks(2, 0.5)
    knocker.run(action)
    assert fallback.actions == [action]
    assert 5 <= clock.now < 5.1
    assert not port.is_open


def test_prompter_speaker_blocks_and_spaces_knock_starts(tmp_path):
    clock, plays = _PrompterClock(), []
    _prompter_sf.write(tmp_path / 'knock.wav', _prompter_np.ones(1600) * 0.2, 16000, subtype='PCM_16')

    def play(samples, **kwargs):
        plays.append((clock.now, kwargs))
        clock.now += len(samples) / kwargs['samplerate']

    backend = _PrompterNamespace(play=play, stop=lambda: None)
    speaker = SpeakerPrompter(4, cfg={'voice_dir': str(tmp_path)}, audio=backend,
                               monotonic=clock.monotonic, sleep=clock.sleep)
    speaker.run(PromptKnocks(3, 0.5))
    assert [t for t, _ in plays] == _prompter_pytest.approx([0, 0.5, 1])
    assert all(kwargs['blocking'] is True and kwargs['device'] == 4 for _, kwargs in plays)
    assert clock.now == _prompter_pytest.approx(1.1)


def test_prompter_speaker_failure_emits_error_and_manual_fallback(tmp_path):
    _prompter_sf.write(tmp_path / 'q_people.wav', _prompter_np.ones(1600) * 0.2, 16000)
    fallback, events, bus = _PrompterManual(), [], EventBus()
    bus.subscribe(events.append)

    def unavailable(*args, **kwargs):
        raise OSError('no output device')

    speaker = SpeakerPrompter(bus=bus, cfg={'voice_dir': str(tmp_path)}, clock=lambda: 9,
                               audio=_PrompterNamespace(play=unavailable), manual=fallback)
    action = PromptVoice('q_people')
    speaker.run(action)
    assert fallback.actions == [action]
    assert events[0].data['code'] == 'speaker_prompt_failed'
    assert events[0].t == 9


def test_prompter_auto_discovers_using_ping_without_striking():
    clock = _PrompterClock()
    port = _PrompterPort(clock)
    prompter = make_prompter('auto', port_lister=lambda: [_PrompterNamespace(device='/dev/ttyUSB-fake')],
                             serial_factory=lambda *args, **kwargs: port, monotonic=clock.monotonic,
                             audio=_PrompterNamespace())
    assert isinstance(prompter, CompositePrompter)
    assert port.writes == [b'PING\n']
    prompter.close()
    assert not port.is_open


def test_prompter_auto_without_serial_or_speaker_uses_manual():
    def unavailable(**kwargs):
        raise OSError('no speaker')

    result = make_prompter('auto', port_lister=lambda: [],
                          audio=_PrompterNamespace(check_output_settings=unavailable))
    assert isinstance(result, ManualPrompter)


def test_prompter_fake_is_seeded_and_never_sleeps(monkeypatch):
    import heartbeat.prompter as module

    def forbidden(seconds):
        raise AssertionError('fake prompter attempted to sleep')

    monkeypatch.setattr(module.time, 'sleep', forbidden)
    first = FakePrompter(seed=8, people=2, can_move='no', hurt='unclear')
    second = FakePrompter(seed=8, people=2, can_move='no', hurt='unclear')
    actions = [PromptVoice('copy_knocks'), PromptKnocks(3, 0.5)]
    end, responses = first.schedule_batch(actions, 10)
    assert (end, responses) == second.schedule_batch(actions, 10)
    assert first.virtual
    assert first.actions == actions
    assert len(responses) == 3
    assert end + 0.9 < responses[0][0] < end + 1.6
    assert [event[0] for event in responses] == sorted(event[0] for event in responses)
    assert len(first.respond([PromptVoice('q_people')], end)) == 2
    assert len(first.respond([PromptVoice('q_move')], end)) == 1
    assert len(first.respond([PromptVoice('q_hurt')], end)) == 3
    assert FakePrompter(no_survivor=True).respond(actions, end) == []
    _prompter_json.dumps(responses, allow_nan=False)


def test_prompter_voice_generator_rejects_empty_tts_without_replacing_asset(tmp_path, monkeypatch):
    import importlib.util
    from pathlib import Path
    import heartbeat.prompter as module

    spec = importlib.util.spec_from_file_location('heartbeat_make_voice_test', Path(module.__file__).parents[1] / 'tools' / 'make_voice.py')
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    original = tmp_path / 'copy_knocks.wav'
    _prompter_sf.write(original, _prompter_np.ones(1600) * 0.2, 16000, subtype='PCM_16')
    saved = original.read_bytes()
    monkeypatch.setattr(tool.platform, 'system', lambda: 'Darwin')
    monkeypatch.setattr(tool.shutil, 'which', lambda name: '/fake/' + name)

    def empty_output(command, **kwargs):
        target = command[command.index('-o') + 1] if '-o' in command else command[-1]
        _prompter_sf.write(target, _prompter_np.zeros(0), 16000, subtype='PCM_16')
        return _PrompterNamespace(returncode=0)

    monkeypatch.setattr(tool.subprocess, 'run', empty_output)
    with _prompter_pytest.raises(ValueError, match='nonempty'):
        tool.generate(tmp_path)
    assert original.read_bytes() == saved
    assert sorted(path.name for path in tmp_path.iterdir()) == ['copy_knocks.wav']


@pytest.mark.parametrize("no_survivor", [False, True])
def test_sim_cli_rejects_nonwarning_error_even_with_expected_outcome(monkeypatch, no_survivor):
    from heartbeat import cli
    from heartbeat.events import Event

    result = SimpleNamespace(
        t=600.0 if no_survivor else 54.0,
        confirmed_t=None if no_survivor else 20.0,
        snapshot={"state": "NO_RESPONSE" if no_survivor else "CONFIRMED",
                  "status": "NO_SIGNS" if no_survivor else "CONFIRMED", "odds": "1 in 1",
                  "answers": {"q_people": 2, "q_move": "yes", "q_hurt": "yes"}},
        events=[Event(type="error", t=5.0, data={"message": "processing failed"})],
    )
    monkeypatch.setattr(cli, "run_simulation", lambda *args, **kwargs: result)
    args = ["sim", "--no-survivor"] if no_survivor else ["sim"]
    assert cli.main(args) == 1


def test_sim_cli_rejects_incomplete_negative_control(monkeypatch):
    from heartbeat import cli

    duration = load_profile().prompter["sim_no_survivor_duration_s"]
    result = SimpleNamespace(
        t=duration - 0.016, confirmed_t=None, events=[],
        snapshot={"state": "LISTENING", "status": "NO_SIGNS", "odds": "1 in 1", "answers": {}},
    )
    monkeypatch.setattr(cli, "run_simulation", lambda *args, **kwargs: result)
    assert cli.main(["sim", "--no-survivor", "--noise"]) == 1


def test_sim_cli_allows_warning_after_complete_negative_control(monkeypatch):
    from heartbeat import cli
    from heartbeat.events import Event

    result = SimpleNamespace(
        t=load_profile().prompter["sim_no_survivor_duration_s"], confirmed_t=None,
        events=[Event(type="error", t=0.0, data={"severity": "warning", "message": "fallback active"})],
        snapshot={"state": "LISTENING", "status": "NO_SIGNS", "odds": "1 in 1", "answers": {}},
    )
    monkeypatch.setattr(cli, "run_simulation", lambda *args, **kwargs: result)
    assert cli.main(["sim", "--no-survivor", "--noise"]) == 0
