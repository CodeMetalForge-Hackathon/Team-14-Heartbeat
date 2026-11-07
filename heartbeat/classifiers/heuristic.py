"""CPU-only knock baseline: interpretable rules, no optional ML packages."""

from __future__ import annotations

from time import perf_counter

import numpy as np

from ..classify import Detection
from .features import extract_features, feature_names, settings


class HeuristicClassifier:
    name = "heuristic"

    def __init__(self, cfg):
        self.cfg = cfg
        self.options = settings(cfg, "heuristic")
        self.names = feature_names(cfg)
        self.inference_ms = 0.0
        self.last_evidence: dict[str, float] = {}

    def score_features(self, features) -> float:
        """Combine soft rules with a weighted geometric mean.

        A maximum rule is fully satisfied up to its threshold, then fades to
        zero at twice the threshold. A minimum rule rises linearly to one.
        These are ranking scores, not calibrated human probabilities.
        """
        values = np.asarray(features, dtype=np.float64)
        if values.ndim != 1 or len(values) != len(self.names) or not np.all(np.isfinite(values)):
            raise ValueError("Invalid heuristic feature vector")
        features = dict(zip(self.names, values))
        opts = self.options
        maximum_rules = {
            "attack": ("attack_s", "attack_max_s"),
            "decay": ("decay_s", "decay_max_s"),
            "centroid": ("centroid_hz", "centroid_max_hz"),
            "high_ratio": ("high_ratio", "high_ratio_max"),
        }
        evidence = {}
        for name, (feature, limit) in maximum_rules.items():
            threshold = float(opts[limit])
            if threshold <= 0:
                raise ValueError(f"Heuristic {limit} must be positive")
            evidence[name] = float(np.clip(2 - features[feature] / threshold, 0, 1))
        threshold = float(opts["centroid_min_hz"])
        if threshold <= 0:
            raise ValueError("Heuristic centroid_min_hz must be positive")
        evidence["centroid_low"] = float(np.clip(features["centroid_hz"] / threshold - 1, 0, 1))
        threshold = float(opts["peak_rms_min"])
        if threshold <= 0:
            raise ValueError("Heuristic peak_rms_min must be positive")
        evidence["peak_rms"] = float(np.clip(features["peak_rms"] / threshold, 0, 1))
        self.last_evidence = dict(evidence)
        weights = opts["weights"]
        total = sum(float(weights[name]) for name in evidence)
        if total <= 0 or any(float(weights[name]) < 0 for name in evidence):
            raise ValueError("Heuristic weights must be nonnegative with positive total")
        score = 1.0
        for name, value in evidence.items():
            weight = float(weights[name])
            if weight:
                score *= value ** (weight / total)
        return float(np.clip(score, 0, 1))

    def classify(self, onset) -> Detection:
        started = perf_counter()
        score = self.score_features(extract_features(onset.clip, self.cfg))
        self.inference_ms = (perf_counter() - started) * 1000
        det = Detection(
            t=float(onset.t), peak=float(onset.peak), snr_db=float(onset.snr_db),
            gated=bool(onset.gated), clip=onset.clip, score=score,
            is_knock=bool(score >= self.cfg.threshold), classifier=self.name,
            top=[["Knock", score], ["Other", float(1-score)]],
        )
        det.evidence = dict(self.last_evidence)
        return det
