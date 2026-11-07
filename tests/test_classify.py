import numpy as np
import pytest

from heartbeat.classify import PassThroughClassifier, make_classifier, register
from heartbeat.config import ClassifierConfig, load_profile
from heartbeat.onset import Onset


def test_passthrough_preserves_onset_and_gate():
    onset = Onset(t=1.0, peak=0.4, snr_db=23.0, gated=True,
                  clip=np.zeros(15600, dtype=np.float32))
    detection = make_classifier(load_profile()).classify(onset)
    assert detection.t == onset.t
    assert detection.clip is onset.clip
    assert detection.gated is True
    assert detection.score == 1.0
    assert detection.is_knock is True
    assert detection.classifier == "none"
    assert len(detection.top) <= 5


def test_registered_factory_receives_classifier_settings():
    observed = []
    classifier = PassThroughClassifier()

    def factory(cfg):
        observed.append(cfg)
        return classifier

    register("test_custom", factory)
    cfg = ClassifierConfig(name="test_custom", threshold=0.7)
    assert make_classifier(cfg) is classifier
    assert observed == [cfg]


def test_missing_classifier_warns_and_publishes_fallback():
    from heartbeat.events import EventBus

    events = []
    bus = EventBus()
    bus.subscribe(events.append)
    with pytest.warns(RuntimeWarning, match="not registered"):
        result = make_classifier(ClassifierConfig("not-installed", 0.5), bus=bus)
    assert isinstance(result, PassThroughClassifier)
    assert events[0].type == "error"
    assert events[0].data["severity"] == "warning"


def test_failed_classifier_initialization_falls_back():
    def unavailable(cfg):
        raise OSError("model missing")

    register("test_unavailable", unavailable)
    with pytest.warns(RuntimeWarning, match="model missing"):
        assert isinstance(make_classifier(ClassifierConfig("test_unavailable", 0.5)), PassThroughClassifier)
