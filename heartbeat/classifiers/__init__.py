"""Register resilient classifiers without importing optional ML libraries."""

from __future__ import annotations

import logging
from pathlib import Path
import sys

from ..classify import register
from .heuristic import HeuristicClassifier
from .clap import ClapClassifier

_LOG = logging.getLogger(__name__)


class AutoClassifier:
    """Select the strongest usable classifier, retiring failed candidates."""

    def __init__(self, cfg, *, preferred="auto"):
        self.cfg = cfg
        if preferred == "auto":
            self._remaining = ["head", "yamnet", "heuristic"]
        elif preferred == "head":
            self._remaining = ["head", "yamnet", "heuristic"]
        elif preferred == "yamnet":
            self._remaining = ["yamnet", "heuristic"]
        else:
            self._remaining = ["heuristic"]
        self.active = self._select()
        print(f"Heartbeat classifier active: {self.name}", file=sys.stderr)

    @property
    def name(self):
        return self.active.name

    @property
    def inference_ms(self):
        return getattr(self.active, "inference_ms", 0.0)

    def _select(self):
        while self._remaining:
            candidate = self._remaining.pop(0)
            try:
                if candidate == "head":
                    if not Path(self.cfg.head_path).is_file():
                        continue
                    from .head import HeadClassifier

                    return HeadClassifier(self.cfg)
                if candidate == "yamnet":
                    if not (Path(self.cfg.model_dir) / "yamnet.tflite").is_file():
                        continue
                    from .yamnet import YamnetClassifier

                    return YamnetClassifier(self.cfg)
                return HeuristicClassifier(self.cfg)
            except Exception as exc:
                _LOG.warning("Classifier %s unavailable: %s", candidate, exc)
        # The heuristic has no optional dependencies; malformed configuration
        # should surface here rather than silently accepting every transient.
        raise RuntimeError("No usable knock classifier remains")

    def classify(self, onset):
        while True:
            try:
                return self.active.classify(onset)
            except Exception as exc:
                if not self._remaining:
                    raise
                _LOG.warning("Classifier %s inference failed: %s; falling back", self.name, exc)
                self.active = self._select()
                print(f"Heartbeat classifier active after fallback: {self.name}", file=sys.stderr)


register("heuristic", HeuristicClassifier)
register("yamnet", lambda cfg: AutoClassifier(cfg, preferred="yamnet"))
register("head", lambda cfg: AutoClassifier(cfg, preferred="head"))
register("auto", AutoClassifier)
register("clap", ClapClassifier)
