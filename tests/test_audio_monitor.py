from concurrent.futures import Future
from hashlib import sha256
from types import SimpleNamespace
import importlib.util
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from heartbeat.audio import AudioBlock
from heartbeat.audio_monitor import AudioMonitor, decode_observation, match_phrases, monitor_identity
from heartbeat.clap_settings import validate_monitor
from heartbeat.classifiers.clap import ClapClassifier
from heartbeat.config import load_profile
from heartbeat.events import EventBus
from heartbeat.runner import Pipeline


@pytest.fixture
def profile():
    return load_profile("clap-distress")


def result(label="SCREAM", distress=True):
    return dict(label=label, score=.9, scores={label: .9}, transcript="", matched_phrases=[],
                possible_distress=distress)


def make_monitor(profile, observe=None):
    cfg = profile.classifier.clap["monitor"]
    bus, events = EventBus(), []
    bus.subscribe(events.append)
    classifier = SimpleNamespace(observe=observe or (lambda clip: result()))
    return AudioMonitor(classifier, bus, cfg, .2), bus, events


def feed(monitor, start, end, amplitude=.2, poll=True):
    for i in range(start, end):
        monitor.feed(AudioBlock(i * .016, np.full(256, amplitude, np.float32)))
        if poll:
            monitor.poll()


def test_continuous_windows_do_not_require_impacts_and_are_bounded(profile):
    clips = []
    def observe(clip):
        clips.append(clip)
        return result()
    monitor, bus, events = make_monitor(profile, observe)
    feed(monitor, 0, 400)
    observations = [e for e in events if e.type == "audio_observation"]
    assert len(observations) == 4
    assert all(len(clip) == 48000 for clip in clips)
    assert len(monitor.buffer) == monitor.size
    assert observations[0].t == pytest.approx(3.008)
    assert observations[0].data["start"] == pytest.approx(.008)
    assert [e.data["alert"] for e in observations] == [True, False, False, False]
    assert not any(e.type == "sound" for e in events)
    monitor.close()


def test_quiet_audio_never_runs_clap_or_asr(profile):
    monitor, _, events = make_monitor(profile, lambda clip: pytest.fail("quiet window submitted"))
    feed(monitor, 0, 300, amplitude=0)
    assert not events
    monitor.close()


def test_prompt_overlap_and_late_results_are_suppressed(profile):
    monitor, bus, events = make_monitor(profile)
    feed(monitor, 0, 190, poll=False)
    future = Future()
    monitor.poll(SimpleNamespace(submit=lambda *args: future))
    bus.publish("prompt_start", 3.04)
    future.set_result(result())
    monitor.poll()
    feed(monitor, 190, 250)
    bus.publish("prompt_end", 4.0)
    feed(monitor, 250, 440)
    assert not any(e.type == "audio_observation" for e in events)
    feed(monitor, 440, 470)
    assert any(e.type == "audio_observation" for e in events)
    monitor.close()


def test_busy_monitor_keeps_capture_moving_and_reports_lost_coverage(profile):
    monitor, _, events = make_monitor(profile)
    feed(monitor, 0, 190, poll=False)
    future = Future()
    monitor.poll(SimpleNamespace(submit=lambda *args: future))
    feed(monitor, 190, 400, poll=False)
    assert monitor.end == 400 * 256
    assert len(monitor.buffer) == 48000
    assert any(e.type == "audio_monitor_status" for e in events)
    future.set_exception(ConnectionError("offline"))
    monitor.poll(can_submit=False)
    assert any(e.type == "error" for e in events)
    assert not any(e.type == "sound" for e in events)
    monitor.close()


def test_audio_discontinuity_drops_stale_window(profile):
    monitor, _, events = make_monitor(profile)
    feed(monitor, 0, 190, poll=False)
    monitor.feed(AudioBlock(10.0, np.ones(256, np.float32)))
    monitor.poll()
    assert len(monitor.buffer) == 256 and monitor.ready is None
    assert not events
    monitor.close()


def test_distress_never_calls_knock_dialog_hooks(profile):
    blocks = [AudioBlock(i * .016, np.full(256, .2, np.float32)) for i in range(260)]
    class Source:
        def __iter__(self):
            return iter(blocks)
        def close(self):
            pass
    detector = SimpleNamespace(process=lambda block: [], flush=lambda: [], level_db=-20, floor_db=-50)
    classifier = SimpleNamespace(settings=profile.classifier.clap, observe=lambda clip: result())
    bus, events = EventBus(), []
    bus.subscribe(events.append)
    pipeline = Pipeline(Source(), detector, classifier, bus, profile)
    pipeline.on_sound.append(lambda det: pytest.fail("Vocal observation reached knock dialog"))
    pipeline.run()
    assert any(e.type == "audio_observation" and e.data["alert"] for e in events)
    assert not any(e.type in {"sound", "answer", "state"} for e in events)


def test_phrase_matching_requires_whole_words():
    assert match_phrases("HELP me! I'm trapped.", ["help", "help me", "I'm trapped"]) == ["help", "help me", "I'm trapped"]
    assert match_phrases("A helpful helper", ["help"]) == []


def test_decoding_is_not_a_knock_and_phrase_requires_enabled_asr(profile):
    cfg = profile.classifier.clap["monitor"]
    body = np.zeros(48000, dtype="<f4").tobytes()
    response = {"monitor": monitor_identity(cfg), "clip_sha256": sha256(body).hexdigest(),
                "scores": [.9] + [.1 / 8] * 8}
    observation = decode_observation(cfg, response, body)
    assert observation["label"] == "SCREAM" and observation["possible_distress"]
    assert "is_knock" not in observation
    response["transcript"] = "Help me"
    with pytest.raises(ValueError, match="ASR disabled"):
        decode_observation(cfg, response, body)
    cfg["asr"]["enabled"] = True
    response["monitor"] = monitor_identity(cfg)
    assert "help me" in decode_observation(cfg, response, body)["matched_phrases"]
    response["scores"][0] = float("nan")
    with pytest.raises(ValueError, match="scores"):
        decode_observation(cfg, response, body)


@pytest.mark.parametrize("change", [{"window_s": 0}, {"hop_s": 4}, {"threshold": float("nan")},
                                     {"min_dbfs": 2}, {"prompts": {"KNOCK": ["knock"]}}])
def test_bad_monitor_config_rejected(profile, change):
    with pytest.raises(ValueError):
        validate_monitor({**profile.classifier.clap["monitor"], **change})


def test_old_service_is_rejected_before_listening(profile):
    cfg = profile.classifier
    identity = {"protocol": "heartbeat-clap-v1", "model": cfg.clap["model"], "revision": cfg.clap["revision"],
                "labels": cfg.clap["positive_prompts"] + cfg.clap["negative_prompts"],
                "sample_rate": 16000, "clip_samples": 15600}
    with pytest.raises(ValueError, match="Restart"):
        ClapClassifier(cfg, request=lambda *args: identity)


def test_observe_http_contract_and_missing_capability(profile):
    path = Path(__file__).resolve().parents[1] / "tools/clap_service.py"
    spec = importlib.util.spec_from_file_location("monitor_service", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    identity = {"monitor": monitor_identity(profile.classifier.clap["monitor"])}
    engine = SimpleNamespace(identity=lambda: identity, predict=lambda body: {}, observe=lambda body: {"ok": True})
    client = TestClient(module.create_app(engine))
    body = np.zeros(48000, dtype="<f4").tobytes()
    headers = {"Content-Type": "application/octet-stream"}
    assert client.post("/observe", content=body, headers=headers).json() == {"ok": True}
    assert client.post("/observe", content=body[:-4], headers=headers).status_code == 422
    assert client.post("/observe", content=body+b"extra", headers=headers).status_code == 413
    identity.clear()
    assert client.post("/observe", content=body, headers=headers).status_code == 409
    recognizer = module.ClapEngine.__new__(module.ClapEngine)
    recognizer.monitor_cfg = profile.classifier.clap["monitor"]
    requests = []
    def transcribe(audio, **kwargs):
        requests.append(audio)
        assert kwargs == {"generate_kwargs": {"do_sample": False}}
        return {"text": "  Help me!  "}
    recognizer.transcriber = transcribe
    assert recognizer._transcribe(np.zeros(48000, np.float32), "SPEECH") == ""
    assert recognizer._transcribe(np.full(48000, .2, np.float32), "OTHER") == ""
    assert recognizer._transcribe(np.full(48000, .2, np.float32), "SPEECH") == "Help me!"
    assert len(requests) == 1 and requests[0]["sampling_rate"] == 16000
