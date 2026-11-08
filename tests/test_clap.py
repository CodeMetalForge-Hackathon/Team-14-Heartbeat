from dataclasses import replace
from hashlib import sha256
import importlib.util
from pathlib import Path
import threading
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi.testclient import TestClient

from heartbeat.audio import AudioBlock, WavSource
from heartbeat.classify import Detection, make_classifier
from heartbeat.classifiers.clap import ClapClassifier
from heartbeat.clap_settings import BODY_BYTES, CLIP_SAMPLES, PROTOCOL, validate_clap
from heartbeat.config import load_profile
from heartbeat.events import EventBus
from heartbeat.onset import Onset
from heartbeat.runner import DialogSession, Pipeline


@pytest.fixture
def profile():
    return load_profile("clap")


def identity(settings):
    return {"protocol": PROTOCOL, "model": settings["model"], "revision": settings["revision"],
            "labels": settings["positive_prompts"] + settings["negative_prompts"],
            "sample_rate": 16000, "clip_samples": CLIP_SAMPLES}


def onset(t=0.1, gated=False):
    return Onset(t=t, peak=0.5, snr_db=20.0, gated=gated, clip=np.zeros(CLIP_SAMPLES, np.float32))


def response(settings, body):
    return {**identity(settings), "clip_sha256": sha256(body).hexdigest(),
            "scores": [0.4, 0.4, 0.2] + [0.0] * 7}


def test_clap_preserves_event_time_gate_and_clip_and_combines_positive_descriptions(profile):
    requests = []
    def request(path, body):
        requests.append((path, body))
        return identity(profile.classifier.clap) if body is None else response(profile.classifier.clap, body)
    classifier = ClapClassifier(profile.classifier, request=request)
    source = onset(t=2.34, gated=True)
    det = classifier.classify(source)
    assert det.t == source.t and det.clip is source.clip and det.gated
    assert det.score == pytest.approx(0.8) and det.is_knock and det.classifier == "clap"
    assert len(requests[1][1]) == BODY_BYTES
    assert [name for name, _ in requests] == ["/health", "/classify"]


@pytest.mark.parametrize("change", [
    {"scores": [float("nan")] * 10}, {"scores": [0.2] * 10},
    {"scores": [1.0]}, {"scores": [-0.1, 1.1] + [0.0] * 8},
    {"clip_sha256": "wrong"}, {"protocol": "wrong"}, {"labels": ["different"]},
])
def test_malformed_or_mismatched_responses_never_become_knocks(profile, change):
    def request(path, body):
        return identity(profile.classifier.clap) if body is None else {**response(profile.classifier.clap, body), **change}
    classifier = ClapClassifier(profile.classifier, request=request)
    with pytest.raises(ValueError):
        classifier.classify(onset())


def test_unavailable_clap_never_falls_back_to_passthrough(profile, monkeypatch):
    def fail(*args):
        raise ConnectionError("service is not running")
    monkeypatch.setattr(ClapClassifier, "_http", fail)
    with pytest.raises(RuntimeError, match="CLAP service unavailable"):
        make_classifier(profile)


@pytest.mark.parametrize("change", [{"unknown": 1}, {"max_pending": 1.2}, {"timeout_s": 0},
                                    {"max_lag_s": 0.5}, {"endpoint": "file:///tmp/model"},
                                    {"positive_prompts": []}, {"cpu_threads": True}])
def test_invalid_settings_rejected(profile, change):
    with pytest.raises(ValueError):
        validate_clap({**profile.classifier.clap, **change})


def test_default_classifier_and_dependencies_are_unchanged():
    assert load_profile().classifier.name == "auto"
    assert load_profile().classifier.clap is None


@pytest.fixture
def service():
    path = Path(__file__).resolve().parents[1] / "tools/clap_service.py"
    spec = importlib.util.spec_from_file_location("test_clap_service_module", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_service_contract_without_model_or_network(service, profile):
    fake = SimpleNamespace(identity=lambda: identity(profile.classifier.clap),
                           predict=lambda body: response(profile.classifier.clap, body))
    client = TestClient(service.create_app(fake))
    assert client.get("/health").json()["protocol"] == PROTOCOL
    body = np.zeros(CLIP_SAMPLES, dtype="<f4").tobytes()
    assert client.post("/classify", content=body).status_code == 415
    headers = {"Content-Type": "application/octet-stream"}
    assert client.post("/classify", content=b"short", headers=headers).status_code == 422
    assert client.post("/classify", content=body + b"extra", headers=headers).status_code == 413
    assert client.post("/classify", content=body, headers=headers).json()["clip_sha256"] == sha256(body).hexdigest()
    def busy(body):
        raise service.BusyError("busy")
    fake.predict = busy
    assert client.post("/classify", content=body, headers=headers).status_code == 429


@pytest.mark.parametrize("values", [np.full(CLIP_SAMPLES, np.nan), np.full(CLIP_SAMPLES, 2.0)])
def test_engine_rejects_invalid_pcm_before_torch(service, values):
    engine = service.ClapEngine.__new__(service.ClapEngine)
    with pytest.raises(ValueError, match="finite"):
        engine.predict(values.astype("<f4").tobytes())


class LiveSource:
    def __init__(self, entered, count=80):
        self.entered, self.count = entered, count
        self.closed = False
        self.all_read = threading.Event()

    def __iter__(self):
        for i in range(self.count):
            if i == 1:
                assert self.entered.wait(3)
            if self.closed:
                return
            yield AudioBlock(i * .016, np.zeros(256, np.float32))
        self.all_read.set()

    def close(self):
        self.closed = True


class Detector:
    level_db, floor_db = -40.0, -50.0
    def __init__(self, every=False):
        self.calls, self.every = 0, every
    def process(self, block):
        self.calls += 1
        return [onset(t=block.t0)] if self.calls == 1 or self.every else []
    def flush(self):
        return []


def slow_pipeline(profile, *, max_pending=4, max_lag_s=2.5, every=False, failure=False):
    entered, release = threading.Event(), threading.Event()
    def classify(source):
        entered.set()
        assert release.wait(5)
        if failure:
            raise ConnectionError("remote failed")
        return Detection(**vars(source), score=0.9, is_knock=True, classifier="clap", top=[])
    classifier = SimpleNamespace(name="clap", async_inference=True, classify=classify,
                                 max_pending=max_pending, max_lag_s=max_lag_s)
    source = LiveSource(entered)
    bus, events = EventBus(), []
    bus.subscribe(events.append)
    pipeline = Pipeline(source, Detector(every), classifier, bus, profile)
    return pipeline, source, entered, release, events


def test_live_capture_and_level_meter_continue_during_inference(profile):
    pipeline, source, entered, release, events = slow_pipeline(profile)
    pipeline.start()
    try:
        assert entered.wait(3) and source.all_read.wait(3)
        assert not any(e.type == "sound" for e in events)
        assert sum(e.type == "level" for e in events) > 5
        assert pipeline.t == pytest.approx(1.28)
        assert pipeline.detection_watermark == 0
    finally:
        release.set()
        pipeline.join(5)
    sounds = [e for e in events if e.type == "sound"]
    assert len(sounds) == 1 and sounds[0].t == 0
    assert not pipeline.inference_failed and pipeline.finished


@pytest.mark.parametrize("settings", [{"max_pending": 1, "every": True}, {"max_lag_s": 0.2}])
def test_overload_stops_session_and_discards_late_results(profile, settings):
    pipeline, source, entered, release, events = slow_pipeline(profile, **settings)
    pipeline.start()
    try:
        pipeline.join(3)
        assert pipeline.finished and pipeline.inference_failed and source.closed
        assert any(e.type == "error" and e.data["where"] == "classifier" for e in events)
    finally:
        release.set()
        pipeline.join(5)
    assert not any(e.type == "sound" for e in events)


def test_service_failure_does_not_emit_a_positive_detection(profile):
    pipeline, source, entered, release, events = slow_pipeline(profile, failure=True)
    pipeline.start()
    try:
        assert source.all_read.wait(3)
    finally:
        release.set()
        pipeline.join(5)
    assert pipeline.inference_failed
    assert not any(e.type == "sound" for e in events)


def test_offline_replay_is_synchronous_to_avoid_artificial_queue_overflow(profile, tmp_path):
    source = WavSource(tmp_path / "unused.wav", realtime=False)
    classifier = SimpleNamespace(async_inference=True)
    pipeline = Pipeline(source, Detector(), classifier, EventBus(), profile)
    assert not pipeline._async_inference


def test_dialog_watermark_waits_strictly_before_pending_impact(profile):
    pipeline, _, _, release, _ = slow_pipeline(profile)
    pipeline.t = 3.0
    pipeline._pending_inference.append((onset(t=1.0), None))
    ticks = []
    session = DialogSession.__new__(DialogSession)
    session.pipeline = pipeline
    session._active = None
    session._stop_when_finished = False
    session._actions = lambda actions: None
    session.engine = SimpleNamespace(tick=lambda t: ticks.append(t))
    session._block(3.0)
    assert ticks == [1 - 1 / 16000]


def test_failed_inference_cannot_confirm_partial_answer_at_eof(profile):
    pipeline, _, _, _, _ = slow_pipeline(profile)
    pipeline.inference_failed = True
    calls = []
    session = DialogSession.__new__(DialogSession)
    session.pipeline, session._active = pipeline, None
    session.engine = SimpleNamespace(command=lambda *args: calls.append(args),
                                    tick=lambda t: pytest.fail("must not close a partial answer"))
    session._end(3.0)
    assert calls == [("reset", 3.0)]
