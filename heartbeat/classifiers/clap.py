"""In-memory CLAP client; PyTorch lives only in the separate inference service."""

from hashlib import sha256
from dataclasses import replace
import json
from time import perf_counter
from urllib.request import Request, urlopen

import numpy as np

from ..clap_settings import CLIP_SAMPLES, PROTOCOL, RESPONSE_BYTES, SAMPLE_RATE, validate_clap
from ..classify import Detection
from ..audio_monitor import monitor_identity, decode_observation


class ClapIdentityError(ValueError):
    """A reachable service has the wrong model, descriptions or contract."""


class ClapClassifier:
    name = "clap"
    async_inference = True

    def __init__(self, cfg, *, request=None):
        self.cfg = cfg
        self.settings = validate_clap(cfg.clap)
        self.labels = self.settings["positive_prompts"] + self.settings["negative_prompts"]
        self.positive_count = len(self.settings["positive_prompts"])
        self.max_pending = self.settings["max_pending"]
        self.max_lag_s = self.settings["max_lag_s"]
        self.inference_ms = 0.0
        self._request = request or self._http
        from ..classify import _REGISTRY, PassThroughClassifier
        factory = _REGISTRY.get(self.settings["fallback"])
        if factory is None:
            raise ValueError("CLAP fallback classifier is not registered")
        self.fallback_classifier = factory(replace(cfg, name=self.settings["fallback"]))
        if isinstance(self.fallback_classifier, (PassThroughClassifier, ClapClassifier)):
            raise ValueError("CLAP fallback must be an independent impact classifier")
        self.startup_error = None
        try:
            self.check_health()
        except ClapIdentityError:
            raise
        except Exception as exc:
            if self.settings["on_failure"] == "stop":
                raise
            self.startup_error = exc

    def check_health(self):
        self._validate_identity(self._request("/health", None))

    def _http(self, path, data):
        request = Request(self.settings["endpoint"].rstrip("/") + path, data=data,
                          headers={"Content-Type": "application/octet-stream"})
        timeout = self.settings["monitor"]["timeout_s"] if path == "/observe" else self.settings["timeout_s"]
        with urlopen(request, timeout=timeout) as response:
            content = response.read(RESPONSE_BYTES + 1)
        if len(content) > RESPONSE_BYTES:
            raise ValueError("CLAP response exceeded the protocol size limit")
        return json.loads(content)

    def _validate_identity(self, response):
        expected = {"protocol": PROTOCOL, "model": self.settings["model"],
                    "revision": self.settings["revision"], "labels": self.labels,
                    "sample_rate": SAMPLE_RATE, "clip_samples": CLIP_SAMPLES}
        if not isinstance(response, dict) or any(response.get(key) != value for key, value in expected.items()):
            raise ClapIdentityError("CLAP service model, descriptions, or audio contract does not match this profile")
        monitor = self.settings.get("monitor")
        if monitor and monitor["enabled"] and response.get("monitor") != monitor_identity(monitor):
            raise ClapIdentityError("Restart CLAP service with the matching distress profile")

    def observe(self, clip):
        cfg = self.settings["monitor"]
        audio = np.asarray(clip, dtype="<f4")
        if (audio.shape != (round(cfg["window_s"] * SAMPLE_RATE),)
                or not np.isfinite(audio).all() or np.max(np.abs(audio)) > 1):
            raise ValueError("Invalid monitor PCM")
        body = audio.tobytes()
        response = self._request("/observe", body)
        self._validate_identity(response)
        return decode_observation(cfg, response, body)

    def classify(self, onset):
        if onset.gated:
            return self.fallback_classifier.classify(onset)
        clip = np.asarray(onset.clip, dtype=np.float32)
        if clip.shape != (CLIP_SAMPLES,) or not np.isfinite(clip).all() or np.max(np.abs(clip)) > 1:
            raise ValueError("CLAP requires 15600 finite mono samples in [-1, 1]")
        body = clip.astype("<f4", copy=False).tobytes()
        started = perf_counter()
        response = self._request("/classify", body)
        self.inference_ms = (perf_counter() - started) * 1000
        self._validate_identity(response)
        if response.get("clip_sha256") != sha256(body).hexdigest():
            raise ValueError("CLAP returned scores for a different audio clip")
        scores = np.asarray(response.get("scores"), dtype=np.float64)
        if (scores.shape != (len(self.labels),) or not np.isfinite(scores).all()
                or np.any(scores < 0) or np.any(scores > 1) or not np.isclose(scores.sum(), 1, atol=1e-5)):
            raise ValueError("CLAP returned invalid relative class scores")
        # Both human-knock descriptions form one positive class. This is a
        # relative softmax mass for a fixed prompt set, not calibrated confidence.
        score = float(np.clip(scores[:self.positive_count].sum(), 0, 1))
        top = [[self.labels[int(i)], float(scores[i])] for i in np.argsort(-scores, kind="stable")[:5]]
        return Detection(t=float(onset.t), peak=float(onset.peak), snr_db=float(onset.snr_db),
                         gated=bool(onset.gated), clip=onset.clip, score=score,
                         is_knock=score >= self.cfg.threshold, classifier=self.name, top=top)
