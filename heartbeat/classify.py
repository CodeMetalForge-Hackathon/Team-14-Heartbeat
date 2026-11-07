"""Classifier contract and registry; P01 deliberately accepts all transients."""

from dataclasses import dataclass
from typing import Callable, Protocol
import warnings

from .config import ClassifierConfig, Profile, load_profile
from .onset import Onset


@dataclass
class Detection(Onset):
    score: float
    is_knock: bool
    classifier: str
    top: list[list[str | float]]


class Classifier(Protocol):
    def classify(self, onset: Onset) -> Detection: ...


class PassThroughClassifier:
    """Baseline for collecting data; a positive result is not human confirmation."""

    def __init__(self, cfg: ClassifierConfig | None = None):
        self.cfg = cfg

    def classify(self, onset: Onset) -> Detection:
        return Detection(
            t=float(onset.t), peak=float(onset.peak), snr_db=float(onset.snr_db),
            gated=bool(onset.gated), clip=onset.clip,
            score=1.0, is_knock=True, classifier="none", top=[["knock", 1.0]],
        )


_REGISTRY: dict[str, Callable[[ClassifierConfig], Classifier]] = {"none": PassThroughClassifier}


def register(name: str, factory: Callable[[ClassifierConfig], Classifier]) -> None:
    """Register a factory accepting the classifier profile section."""
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Classifier name must be a nonempty string")
    if not callable(factory):
        raise TypeError("Classifier factory must be callable")
    _REGISTRY[name] = factory


def make_classifier(cfg: ClassifierConfig | Profile | None = None, *, bus=None) -> Classifier:
    """Build a registered classifier, warning and falling back if unavailable."""
    if cfg is None:
        cfg = load_profile().classifier
    if isinstance(cfg, Profile):
        cfg = cfg.classifier
    factory = _REGISTRY.get(cfg.name)
    message = None
    if factory is None:
        message = f"Classifier {cfg.name!r} is not registered; using passthrough ('none')"
    else:
        try:
            return factory(cfg)
        except Exception as exc:
            message = f"Classifier {cfg.name!r} failed to initialize: {exc}; using passthrough ('none')"
    warnings.warn(message, RuntimeWarning, stacklevel=2)
    if bus is not None:
        bus.publish("error", 0.0, component="classifier", severity="warning", message=message)
    return PassThroughClassifier(cfg)
