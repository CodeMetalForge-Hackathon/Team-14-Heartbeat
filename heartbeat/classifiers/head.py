"""Persisted, calibrated knock head with an explicit, reproducible feature contract.

The optional ML packages are imported only when a head is trained or loaded.
A DSP-only artifact is deliberately identified in its feature spec; missing YAMNet
never silently substitutes zero scores for the real model's output.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import tempfile
from time import perf_counter

import numpy as np

from ..classify import Detection
from .features import FEATURE_SPEC_VERSION, extract_features, feature_names, settings


ARTIFACT_VERSION = 1
SAMPLE_RATE = 16000  # Audio contract, not a tuning parameter.
CLIP_SAMPLES = 15600  # YAMNet input contract.
YAMNET_CLASSES = 521  # AudioSet/YAMNet output contract.


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


class FeatureExtractor:
    """Shared training/runtime feature extraction; fake YAMNet is injectable."""

    def __init__(self, cfg, *, mode="yamnet_dsp", yamnet=None):
        if mode not in {"yamnet_dsp", "dsp"}:
            raise ValueError("feature mode must be 'yamnet_dsp' or explicit 'dsp'")
        self.cfg = cfg
        self.mode = mode
        self.yamnet = yamnet
        if mode == "yamnet_dsp" and self.yamnet is None:
            from .yamnet import YamnetClassifier
            self.yamnet = YamnetClassifier(cfg)
        self.spec = {
            "version": FEATURE_SPEC_VERSION,
            "mode": mode,
            "sample_rate": SAMPLE_RATE,
            "clip_samples": CLIP_SAMPLES,
            "dsp_names": list(feature_names(cfg)),
            "dsp_config": settings(cfg, "features"),
            "yamnet": None,
        }
        if mode == "yamnet_dsp":
            dimensions = dict(self.yamnet.feature_spec)
            if int(dimensions["class_scores"]) != YAMNET_CLASSES:
                raise ValueError("YAMNet must expose all 521 class scores")
            self.spec["yamnet"] = {
                "class_scores": YAMNET_CLASSES,
                "embedding": int(dimensions.get("embedding", 0)),
                "model_sha256": getattr(self.yamnet, "model_sha256", None),
            }
        self.dimension = len(self.spec["dsp_names"])
        if self.spec["yamnet"] is not None:
            self.dimension += YAMNET_CLASSES + self.spec["yamnet"]["embedding"]

    def transform(self, clip):
        x = np.asarray(clip, dtype=np.float32)
        if x.ndim != 1 or len(x) != CLIP_SAMPLES or not np.all(np.isfinite(x)):
            raise ValueError("head requires a finite mono 15600-sample clip")
        dsp = np.asarray(extract_features(x, self.cfg), dtype=np.float64).reshape(-1)
        parts = []
        if self.mode == "yamnet_dsp":
            scores, embedding = self.yamnet.extract(x)
            scores = np.asarray(scores, dtype=np.float64).reshape(-1)
            embedded = np.asarray([] if embedding is None else embedding,
                                  dtype=np.float64).reshape(-1)
            if (len(scores) != YAMNET_CLASSES or
                    len(embedded) != self.spec["yamnet"]["embedding"]):
                raise ValueError("YAMNet output changed from the saved feature spec")
            parts.extend((scores, embedded))
        parts.append(dsp)
        features = np.concatenate(parts)
        if features.size != self.dimension or not np.all(np.isfinite(features)):
            raise ValueError("feature vector has unexpected dimensions or nonfinite values")
        return features

    __call__ = transform


def feature_spec(cfg, *, mode="yamnet_dsp", yamnet=None):
    """Describe the feature ordering/settings used by this model and profile."""
    return FeatureExtractor(cfg, mode=mode, yamnet=yamnet).spec


def save_head(path, pipeline, feature_spec, training_data_hash, threshold, *, metadata=None):
    """Atomically save a trusted local sklearn pipeline and its provenance."""
    threshold = float(threshold)
    if not math.isfinite(threshold) or not 0 <= threshold <= 1:
        raise ValueError("head threshold must be finite and in [0, 1]")
    if (not isinstance(training_data_hash, str) or len(training_data_hash) != 64 or
            any(c not in "0123456789abcdef" for c in training_data_hash)):
        raise ValueError("training_data_hash must be a SHA-256 hex digest")
    _canonical(feature_spec)  # Reject unserializable/NaN feature specifications.
    artifact = {
        "version": ARTIFACT_VERSION,
        "pipeline": pipeline,
        "feature_spec": feature_spec,
        "training_data_hash": training_data_hash,
        "threshold": threshold,
        "metadata": {} if metadata is None else metadata,
    }
    import joblib
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix=target.name + ".",
                                         suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
        joblib.dump(artifact, temporary)
        os.replace(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return artifact


def load_head(path):
    """Load and validate an artifact written by :func:`save_head`."""
    import joblib
    artifact = joblib.load(Path(path))
    if not isinstance(artifact, dict) or artifact.get("version") != ARTIFACT_VERSION:
        raise ValueError("unsupported knock head artifact version")
    for key in ("pipeline", "feature_spec", "training_data_hash", "threshold"):
        if key not in artifact:
            raise ValueError(f"knock head artifact is missing {key}")
    threshold = float(artifact["threshold"])
    if not math.isfinite(threshold) or not 0 <= threshold <= 1:
        raise ValueError("invalid saved head threshold")
    if not hasattr(artifact["pipeline"], "predict_proba"):
        raise ValueError("saved head has no probability predictor")
    classes = np.asarray(getattr(artifact["pipeline"], "classes_", []))
    if not np.array_equal(classes, [0, 1]):
        raise ValueError("saved head must have binary classes [0, 1]")
    if not isinstance(artifact["feature_spec"], dict):
        raise ValueError("invalid saved feature spec")
    data_hash = artifact["training_data_hash"]
    if (not isinstance(data_hash, str) or len(data_hash) != 64 or
            any(c not in "0123456789abcdef" for c in data_hash)):
        raise ValueError("invalid saved training-data SHA-256")
    _canonical(artifact["feature_spec"])
    return artifact


class HeadClassifier:
    """Apply the profile threshold while checking the saved feature contract."""

    def __init__(self, cfg, *, yamnet=None):
        self.cfg = cfg
        self.artifact = load_head(cfg.head_path)
        saved_spec = self.artifact["feature_spec"]
        if saved_spec.get("version") != FEATURE_SPEC_VERSION:
            raise ValueError(f"head feature spec version {saved_spec.get('version')!r} "
                             f"differs from current version {FEATURE_SPEC_VERSION}; retrain")
        self.extractor = FeatureExtractor(cfg, mode=saved_spec.get("mode"), yamnet=yamnet)
        if _canonical(self.extractor.spec) != _canonical(saved_spec):
            raise ValueError("head feature spec differs from current DSP/YAMNet configuration; retrain")
        self.pipeline = self.artifact["pipeline"]
        if int(getattr(self.pipeline, "n_features_in_", -1)) != self.extractor.dimension:
            raise ValueError("head predictor width differs from saved feature spec")
        self.calibrated_threshold = float(self.artifact["threshold"])
        self.threshold = float(cfg.threshold)
        if not math.isfinite(self.threshold) or not 0 <= self.threshold <= 1:
            raise ValueError("profile head threshold must be finite and in [0, 1]")
        self.name = "head" if self.extractor.mode == "yamnet_dsp" else "head_dsp"
        self.inference_ms = 0.0

    def classify(self, onset):
        started = perf_counter()
        features = self.extractor.transform(onset.clip)
        score = float(self.pipeline.predict_proba(features.reshape(1, -1))[0, 1])
        self.inference_ms = (perf_counter() - started) * 1000
        if not math.isfinite(score) or not 0 <= score <= 1:
            raise ValueError("head produced an invalid probability")
        return Detection(
            **asdict_without_clip(onset), clip=onset.clip, score=score,
            is_knock=bool(score >= self.threshold), classifier=self.name,
            top=[["knock", score], ["other", float(1 - score)]],
        )


def asdict_without_clip(onset):
    # Avoid dataclasses.asdict: its deep copy would copy the audio for each onset.
    return {"t": float(onset.t), "peak": float(onset.peak),
            "snr_db": float(onset.snr_db), "gated": bool(onset.gated)}
