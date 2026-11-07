"""Seeded contact-audio fixtures; synthesis settings live in the rubble profile."""

from __future__ import annotations

from functools import lru_cache
import math
from collections.abc import Mapping, Sequence

import numpy as np

SAMPLE_RATE = 16000  # Shared audio contract.


@lru_cache(maxsize=1)
def _defaults():
    from .config import load_profile

    return load_profile("rubble").audio.synth


def _settings(cfg):
    return _defaults() if cfg is None else cfg


def _random(seed, rng, cfg):
    return rng if rng is not None else np.random.default_rng(cfg["scene"]["seed"] if seed is None else seed)


def _length(duration_s):
    duration_s = float(duration_s)
    if not math.isfinite(duration_s) or duration_s < 0:
        raise ValueError("duration_s must be finite and nonnegative")
    return round(duration_s * SAMPLE_RATE)


def _normalize(x, cfg):
    if not len(x):
        return np.asarray(x, dtype=np.float32)
    peak = float(np.max(np.abs(x)))
    return np.asarray(x * (float(cfg["peak"]) / peak) if peak else x, dtype=np.float32)


def knock(duration_s=None, *, seed=None, rng=None, cfg=None):
    """Two or three damped debris modes plus a short noisy contact click."""
    cfg = _settings(cfg)
    settings = cfg["knock"]
    rng = _random(seed, rng, cfg)
    count = _length(settings["duration_s"] if duration_s is None else duration_s)
    t = np.arange(count) / SAMPLE_RATE
    x = np.zeros(count)
    modes = int(rng.integers(settings["modes_min"], settings["modes_max"] + 1))
    for _ in range(modes):
        frequency = rng.uniform(settings["low_hz"], settings["high_hz"])
        decay = rng.uniform(settings["decay_min_s"], settings["decay_max_s"])
        x += np.sin(2 * np.pi * frequency * t) * np.exp(-t / decay)
    click = min(count, _length(settings["click_s"]))
    x[:click] += float(settings["click_gain"]) * rng.standard_normal(click)
    return _normalize(x, cfg)


def footstep(duration_s=None, *, seed=None, rng=None, cfg=None):
    """A slow low-frequency impact, deliberately unlike a contact click."""
    cfg = _settings(cfg)
    settings = cfg["footstep"]
    rng = _random(seed, rng, cfg)
    count = _length(settings["duration_s"] if duration_s is None else duration_s)
    t = np.arange(count) / SAMPLE_RATE
    frequency = rng.uniform(settings["low_hz"], settings["high_hz"])
    return _normalize(np.sin(2 * np.pi * frequency * t) * np.exp(-t / settings["decay_s"]), cfg)


def clap(duration_s=None, *, seed=None, rng=None, cfg=None):
    """A rapidly decaying broadband burst."""
    cfg = _settings(cfg)
    settings = cfg["clap"]
    rng = _random(seed, rng, cfg)
    count = _length(settings["duration_s"] if duration_s is None else duration_s)
    t = np.arange(count) / SAMPLE_RATE
    return _normalize(rng.standard_normal(count) * np.exp(-t / settings["decay_s"]), cfg)


def speech_like(duration_s=None, *, seed=None, rng=None, cfg=None):
    """Band-limited noise with a syllabic envelope, not synthesized speech."""
    cfg = _settings(cfg)
    settings = cfg["speech_like"]
    rng = _random(seed, rng, cfg)
    count = _length(settings["duration_s"] if duration_s is None else duration_s)
    if not count:
        return np.empty(0, dtype=np.float32)
    frequencies = np.fft.rfftfreq(count, 1 / SAMPLE_RATE)
    spectrum = np.fft.rfft(rng.standard_normal(count))
    spectrum[(frequencies < settings["low_hz"]) | (frequencies > settings["high_hz"])] = 0
    t = np.arange(count) / SAMPLE_RATE
    depth = float(settings["modulation_depth"])
    envelope = 1 - depth + depth * (1 - np.cos(2 * np.pi * settings["modulation_hz"] * t)) / 2
    return _normalize(np.fft.irfft(spectrum, n=count) * envelope, cfg)


def drop(duration_s=None, *, seed=None, rng=None, cfg=None):
    """An object bouncing repeatedly, with shrinking gaps and amplitudes."""
    cfg = _settings(cfg)
    settings = cfg["drop"]
    rng = _random(seed, rng, cfg)
    count = _length(settings["duration_s"] if duration_s is None else duration_s)
    x = np.zeros(count)
    bounces = int(rng.integers(settings["bounces_min"], settings["bounces_max"] + 1))
    t, gap, gain = 0.0, float(settings["first_gap_s"]), 1.0
    for _ in range(bounces):
        start = round(t * SAMPLE_RATE)
        sound = knock(rng=rng, cfg=cfg)
        end = min(count, start + len(sound))
        if start < count:
            x[start:end] += gain * sound[: end - start]
        t += gap
        gap *= settings["gap_ratio"]
        gain *= settings["gain_ratio"]
    return _normalize(x, cfg)


def background(level_dbfs=-50.0, duration_s=None, *, seed=None, rng=None, cfg=None):
    """Pinkish stationary noise normalized to the requested RMS dBFS."""
    cfg = _settings(cfg)
    settings = cfg["background"]
    rng = _random(seed, rng, cfg)
    count = _length(settings["duration_s"] if duration_s is None else duration_s)
    if not count:
        return np.empty(0, dtype=np.float32)
    frequencies = np.fft.rfftfreq(count, 1 / SAMPLE_RATE)
    spectrum = np.fft.rfft(rng.standard_normal(count))
    spectrum /= np.maximum(frequencies, settings["low_hz"]) ** settings["pink_exponent"]
    spectrum[0] = 0
    x = np.fft.irfft(spectrum, n=count)
    rms = float(np.sqrt(np.mean(x * x)))
    if rms:
        x *= 10 ** (float(level_dbfs) / 20) / rms
    return np.clip(x, -1, 1).astype(np.float32)


_SOUNDS = {"knock": knock, "footstep": footstep, "clap": clap, "speech_like": speech_like, "drop": drop}


def scene(duration_s, events: Sequence[tuple[float, str, float]] | None = None,
          bg_dbfs=-50.0, seed=None, *, cfg: Mapping | None = None):
    """Return ``(audio, [{t, kind}, ...])`` on the shared 16 kHz sample clock.

    Explicit events are ``(seconds, sound_name, gain_db)``. With ``events=None``
    the profile's repeated schedule supplies knocks mixed with distractors.
    Events beyond the scene are omitted; a final sound may be truncated.
    """
    cfg = _settings(cfg)
    count = _length(duration_s)
    rng = _random(seed, None, cfg)
    x = background(bg_dbfs, duration_s, rng=rng, cfg=cfg).astype(np.float64)
    if events is None:
        settings = cfg["scene"]
        interval = float(settings["interval_s"])
        kinds = settings["kinds"]
        if interval <= 0 or not kinds:
            raise ValueError("scene requires a positive interval_s and at least one kind")
        events = []
        t = float(settings["first_event_s"])
        while round(t * SAMPLE_RATE) < count:
            events.append((t, kinds[len(events) % len(kinds)], settings["gain_db"]))
            t += interval
    truth = []
    for t, kind, gain_db in sorted(events, key=lambda event: event[0]):
        if kind not in _SOUNDS:
            raise ValueError(f"unknown synthetic sound: {kind}")
        if not math.isfinite(float(t)) or float(t) < 0 or not math.isfinite(float(gain_db)):
            raise ValueError("event times must be nonnegative and event gains finite")
        start = round(float(t) * SAMPLE_RATE)
        if start >= count:
            continue
        sound = _SOUNDS[kind](rng=rng, cfg=cfg)
        end = min(count, start + len(sound))
        x[start:end] += sound[: end - start] * 10 ** (float(gain_db) / 20)
        truth.append({"t": start / SAMPLE_RATE, "kind": kind})
    return np.clip(x, -1, 1).astype(np.float32), truth
