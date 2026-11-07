"""Shared, dependency-free settings for the optional CLAP service."""

import math
from urllib.parse import urlsplit


PROTOCOL = "heartbeat-clap-v1"
SAMPLE_RATE = 16000
CLIP_SAMPLES = 15600
BODY_BYTES = CLIP_SAMPLES * 4  # Protocol: mono little-endian float32, no WAV header.
RESPONSE_BYTES = 65536  # Bound JSON responses from a configured inference endpoint.


def validate_clap(settings):
    required = {"endpoint", "timeout_s", "max_pending", "max_lag_s", "model",
                "revision", "device", "cpu_threads", "positive_prompts", "negative_prompts"}
    if not isinstance(settings, dict) or set(settings) != required:
        raise ValueError(f"classifier.clap requires exactly: {', '.join(sorted(required))}")
    endpoint = urlsplit(settings["endpoint"])
    if (endpoint.scheme not in {"http", "https"} or not endpoint.hostname or endpoint.username
            or endpoint.password or endpoint.query or endpoint.fragment or endpoint.path not in ("", "/")):
        raise ValueError("classifier.clap.endpoint must be an HTTP(S) origin without credentials or a path")
    for name in ("timeout_s", "max_pending", "max_lag_s", "cpu_threads"):
        value = settings[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError(f"classifier.clap.{name} must be positive and finite")
        if name in {"max_pending", "cpu_threads"} and not isinstance(value, int):
            raise ValueError(f"classifier.clap.{name} must be an integer")
    if settings["max_lag_s"] <= (CLIP_SAMPLES - 1600) / SAMPLE_RATE:
        raise ValueError("classifier.clap.max_lag_s must exceed the detector's 0.875s clip look-ahead")
    for name in ("model", "revision", "device"):
        if not isinstance(settings[name], str) or not settings[name].strip():
            raise ValueError(f"classifier.clap.{name} must be a nonempty string")
    prompts = []
    for name in ("positive_prompts", "negative_prompts"):
        values = settings[name]
        if not isinstance(values, list) or not values or not all(isinstance(x, str) and x.strip() for x in values):
            raise ValueError(f"classifier.clap.{name} must be a nonempty list of descriptions")
        prompts.extend(values)
    if len(prompts) != len(set(prompts)):
        raise ValueError("CLAP descriptions must be unique across both groups")
    return settings
