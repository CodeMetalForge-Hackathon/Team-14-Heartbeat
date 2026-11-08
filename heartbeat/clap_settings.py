"""Shared, dependency-free settings for the optional CLAP service."""

import math
from urllib.parse import urlsplit


PROTOCOL = "heartbeat-clap-v1"
SAMPLE_RATE = 16000
BUSY_STATUS_INTERVAL_S = 10  # Fixed CLAP-05 status cadence, independent of health retries.
CLIP_SAMPLES = 15600
BODY_BYTES = CLIP_SAMPLES * 4  # Protocol: mono little-endian float32, no WAV header.
RESPONSE_BYTES = 65536  # Bound JSON responses from a configured inference endpoint.


def validate_clap(settings):
    required = {"endpoint", "timeout_s", "max_pending", "max_lag_s", "model",
                "revision", "device", "cpu_threads", "positive_prompts", "negative_prompts"}
    optional = {"monitor", "on_failure", "fallback", "retry_s"}
    if not isinstance(settings, dict) or set(settings) - optional != required:
        raise ValueError(f"classifier.clap requires exactly: {', '.join(sorted(required))}")
    settings = {"on_failure": "fallback", "fallback": "heuristic", "retry_s": 10, **settings}
    if not isinstance(settings["on_failure"], str) or settings["on_failure"] not in {"fallback", "stop"}:
        raise ValueError("classifier.clap.on_failure must be fallback or stop")
    if (not isinstance(settings["fallback"], str) or not settings["fallback"].strip()
            or settings["fallback"] in {"clap", "none"}):
        raise ValueError("classifier.clap.fallback must classify impacts; clap and passthrough none are forbidden")
    endpoint = urlsplit(settings["endpoint"])
    if (endpoint.scheme not in {"http", "https"} or not endpoint.hostname or endpoint.username
            or endpoint.password or endpoint.query or endpoint.fragment or endpoint.path not in ("", "/")):
        raise ValueError("classifier.clap.endpoint must be an HTTP(S) origin without credentials or a path")
    for name in ("timeout_s", "max_pending", "max_lag_s", "cpu_threads", "retry_s"):
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
    if settings.get("monitor") is not None:
        validate_monitor(settings["monitor"])
    return settings


def validate_monitor(cfg):
    required = {"enabled", "window_s", "hop_s", "threshold", "min_dbfs", "cooldown_s",
                "timeout_s", "prompts", "asr"}
    if not isinstance(cfg, dict) or set(cfg) != required or type(cfg["enabled"]) is not bool:
        raise ValueError("Invalid CLAP monitor configuration")
    for key in ("window_s", "hop_s", "threshold", "min_dbfs", "cooldown_s", "timeout_s"):
        value = cfg[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"monitor.{key} must be finite")
    if not (1 <= cfg["window_s"] <= 10 and 0 < cfg["hop_s"] <= cfg["window_s"]
            and 0 < cfg["threshold"] <= 1 and -120 <= cfg["min_dbfs"] <= 0
            and cfg["cooldown_s"] >= 0 and cfg["timeout_s"] > 0
            and round(cfg["hop_s"] * SAMPLE_RATE) >= 1):
        raise ValueError("CLAP monitor values out of range")
    prompts = cfg["prompts"]
    required_labels = {"SCREAM", "SHOUT", "CRYING", "SPEECH", "OTHER"}
    if not isinstance(prompts, dict) or set(prompts) - {"BREATHING"} != required_labels:
        raise ValueError("Monitor requires SCREAM, SHOUT, CRYING, SPEECH, OTHER; BREATHING is optional")
    values = [p for group in prompts.values() if isinstance(group, list) for p in group]
    if (any(not isinstance(group, list) or not group for group in prompts.values())
            or any(not isinstance(p, str) or not p.strip() for p in values)
            or len(values) != len(set(values))):
        raise ValueError("Monitor descriptions must be nonempty and unique")
    asr = cfg["asr"]
    if (not isinstance(asr, dict) or set(asr) != {"enabled", "model", "revision", "phrases"}
            or type(asr["enabled"]) is not bool
            or any(not isinstance(asr[k], str) or not asr[k].strip() for k in ("model", "revision"))
            or not isinstance(asr["phrases"], list) or not asr["phrases"]
            or any(not isinstance(p, str) or not p.strip() for p in asr["phrases"])):
        raise ValueError("Invalid optional speech recognizer settings")
    return cfg
