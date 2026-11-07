"""Continuous vocal observations, deliberately separate from knock detections."""

from collections import deque
from hashlib import sha256
import re

import numpy as np

from .clap_settings import SAMPLE_RATE


def monitor_identity(cfg):
    return {"window_samples": round(cfg["window_s"] * SAMPLE_RATE),
            "prompts": [{"label": label, "text": prompt} for label, prompts in cfg["prompts"].items() for prompt in prompts],
            "threshold": cfg["threshold"], "min_dbfs": cfg["min_dbfs"], "asr": cfg["asr"]}


def match_phrases(text, phrases):
    normalize = lambda value: " ".join(re.findall(r"\w+", value.casefold()))
    words = " " + normalize(text) + " "
    return [p for p in phrases if " " + normalize(p) + " " in words]


def decode_observation(cfg, response, body):
    if response.get("monitor") != monitor_identity(cfg) or response.get("clip_sha256") != sha256(body).hexdigest():
        raise ValueError("Monitor response does not match audio/settings")
    labels = [label for label, prompts in cfg["prompts"].items() for _ in prompts]
    scores = np.asarray(response.get("scores"), dtype=float)
    if (scores.shape != (len(labels),) or not np.isfinite(scores).all() or np.any(scores < 0)
            or np.any(scores > 1) or not np.isclose(scores.sum(), 1, atol=1e-5)):
        raise ValueError("Invalid monitor scores")
    groups = {label: float(sum(score for key, score in zip(labels, scores) if key == label))
              for label in cfg["prompts"]}
    label = max(groups, key=groups.get)
    score = groups[label]
    if score < cfg["threshold"]:
        label = "UNCERTAIN"
    transcript = response.get("transcript", "")
    if not isinstance(transcript, str) or len(transcript) > 4096:
        raise ValueError("Invalid transcript")
    if not cfg["asr"]["enabled"] and transcript:
        raise ValueError("Unexpected transcript with ASR disabled")
    matches = match_phrases(transcript, cfg["asr"]["phrases"]) if cfg["asr"]["enabled"] else []
    return {"label": label, "score": score, "scores": groups, "transcript": transcript,
            "matched_phrases": matches, "possible_distress": label in {"SCREAM", "SHOUT", "CRYING"} or bool(matches)}


class AudioMonitor:
    """Bounded rolling buffer and one outstanding request. Called by Pipeline."""

    def __init__(self, classifier, bus, cfg, tail_s):
        self.classifier, self.bus, self.cfg, self.tail_s = classifier, bus, cfg, tail_s
        self.size = round(cfg["window_s"] * SAMPLE_RATE)
        self.hop = round(cfg["hop_s"] * SAMPLE_RATE)
        self.buffer = deque(maxlen=self.size)
        self.next_end = self.size
        self.end = 0
        self.ready = self.pending = None
        self.generation = 0
        self.blocked_until = 0.0
        self.last_alert = -float("inf")
        self.skipped = 0
        self.unsubscribe = bus.subscribe(self._event)

    def _event(self, event):
        if event.type not in {"prompt_start", "prompt_end"}:
            return
        self.generation += 1
        self.buffer.clear()
        self.ready = None
        self.blocked_until = float("inf") if event.type == "prompt_start" else event.t + self.tail_s

    def feed(self, block):
        start = round(block.t0 * SAMPLE_RATE)
        if start != self.end:
            self.buffer.clear()
            self.ready = None
            self.generation += 1
        self.end = start + len(block.x)
        if block.t0 < self.blocked_until:
            self.buffer.clear()
            return
        self.buffer.extend(block.x)
        if len(self.buffer) < self.size:
            self.next_end = self.end + self.size - len(self.buffer)
            return
        if self.end < self.next_end:
            return
        self.next_end += self.hop
        clip = np.asarray(self.buffer, dtype=np.float32)
        rms = float(np.sqrt(np.mean(clip.astype(float) ** 2)))
        if 20 * np.log10(max(rms, 1e-12)) < self.cfg["min_dbfs"]:
            self.ready = None
            return
        if self.ready is not None:
            self.skipped += 1
            self.bus.publish("audio_monitor_status", self.end / SAMPLE_RATE,
                             message="Monitor busy; replaced an unprocessed window", skipped=self.skipped)
        self.ready = (self.end / SAMPLE_RATE, clip, self.generation)

    def poll(self, executor=None, *, can_submit=True, wait=False):
        if self.pending:
            end, clip, generation, future = self.pending
            if not wait and not future.done():
                return
            self.pending = None
            try:
                result = future.result(timeout=self.cfg["timeout_s"])
                self._publish(end, clip, generation, result)
            except Exception as exc:
                self.bus.publish("error", end, where="audio_monitor", message=str(exc))
        if self.ready is None or not can_submit:
            return
        end, clip, generation = self.ready
        self.ready = None
        if executor:
            self.pending = (end, clip, generation, executor.submit(self.classifier.observe, clip))
        else:
            try:
                self._publish(end, clip, generation, self.classifier.observe(clip))
            except Exception as exc:
                self.bus.publish("error", end, where="audio_monitor", message=str(exc))

    def _publish(self, end, clip, generation, result):
        if generation != self.generation:
            return
        alert = result["possible_distress"] and end - self.last_alert >= self.cfg["cooldown_s"]
        if alert:
            self.last_alert = end
        self.bus.publish("audio_observation", end, **result, alert=alert,
                         start=end - len(clip) / SAMPLE_RATE)

    def close(self):
        if self.pending:
            self.pending[-1].cancel()
        self.unsubscribe()
