"""Small deterministic DSP feature vector shared by the baseline and head."""

from __future__ import annotations

from collections.abc import Mapping
from functools import lru_cache

import numpy as np
from scipy.signal import stft

from ..onset import SAMPLE_RATE


@lru_cache(maxsize=None)
def _defaults(section):
    from ..config import load_profile

    return getattr(load_profile().classifier, section) or {}


def settings(cfg=None, section="features") -> dict:
    """Fill omitted section values from the profile, without Python tunables."""
    supplied = cfg if isinstance(cfg, Mapping) else getattr(cfg, section, None)
    merged = {**_defaults(section), **(supplied or {})}
    for name, value in (supplied or {}).items():
        if isinstance(value, Mapping) and isinstance(_defaults(section).get(name), Mapping):
            merged[name] = {**_defaults(section)[name], **value}
    return merged


def feature_names(cfg=None) -> list[str]:
    opts = settings(cfg)
    return [
        "attack_s", "decay_s", "centroid_hz", "low_ratio", "mid_ratio",
        "high_ratio", "peak_rms",
        *(f"log_mel_{index:02d}" for index in range(int(opts["mel_bands"]))),
    ]


@lru_cache(maxsize=16)
def _mel_filters(fft_size: int, bands: int, low_hz: float, high_hz: float):
    # The standard Hz/mel conversion constants define the scale, not tuning.
    hz_to_mel = lambda hz: 2595 * np.log10(1 + hz / 700)
    mel_to_hz = lambda mel: 700 * (10 ** (mel / 2595) - 1)
    edges = mel_to_hz(np.linspace(hz_to_mel(low_hz), hz_to_mel(high_hz), bands + 2))
    frequencies = np.fft.rfftfreq(fft_size, 1 / SAMPLE_RATE)
    filters = np.zeros((bands, len(frequencies)))
    for index in range(bands):
        left, middle, right = edges[index : index + 3]
        filters[index] = np.maximum(0, np.minimum(
            (frequencies - left) / (middle - left),
            (right - frequencies) / (right - middle),
        ))
        total = filters[index].sum()
        if total:
            filters[index] /= total
    return filters


def extract_features(clip, cfg=None) -> np.ndarray:
    """Return attack, decay, spectrum, crest factor and log-mel means.

    Attack measures the envelope's first configured fractional crossing before
    its peak; decay ends at the last crossing above the configured relative dB
    level. Using the last crossing avoids treating an oscillation's zero as
    the end of a resonant impact. Input can be any finite mono length, so the
    same extractor serves both zero-padded onset clips and data tools.
    """
    opts = settings(cfg)
    x = np.asarray(clip, dtype=np.float64)
    if x.ndim != 1 or not np.all(np.isfinite(x)):
        raise ValueError("DSP features require finite mono audio")
    if not len(x):
        x = np.zeros(1)
    floor = float(opts["log_floor"])
    frame_size = int(opts["fft_size"])
    hop = int(opts["fft_hop"])
    bands = int(opts["mel_bands"])
    if not (floor > 0 and frame_size > 1 and 0 < hop <= frame_size and bands > 0):
        raise ValueError("Invalid DSP floor, FFT, hop or mel settings")
    if not (float(opts["envelope_s"]) > 0 and 0 < float(opts["attack_fraction"]) < 1 and float(opts["decay_db"]) < 0):
        raise ValueError("Envelope duration, attack fraction or decay threshold is invalid")
    if not 0 <= float(opts["mel_low_hz"]) < float(opts["mel_high_hz"]) <= SAMPLE_RATE / 2:
        raise ValueError("Mel bounds must lie within the audio Nyquist range")

    # Linear-time moving mean of squared amplitude avoids a wide convolution.
    window = max(1, round(float(opts["envelope_s"]) * SAMPLE_RATE))
    padded = np.pad(x * x, (window // 2, window - window // 2))
    cumulative = np.concatenate(([0.0], np.cumsum(padded)))
    envelope = np.sqrt(np.maximum((cumulative[window:] - cumulative[:-window])[:len(x)] / window, 0))
    peak_index = int(np.argmax(envelope))
    envelope_peak = float(envelope[peak_index])
    if envelope_peak > 0:
        # Measure the contiguous rise into the peak: start after the last
        # sample below the fraction, so steady background noise above that
        # fraction in the pre-roll is not counted as a slow attack.
        baseline = float(np.median(envelope[:peak_index])) if peak_index else 0.0
        level = baseline + (envelope_peak - baseline) * float(opts["attack_fraction"])
        below = np.flatnonzero(envelope[:peak_index + 1] < level)
        start = int(below[-1]) + 1 if len(below) else 0
        attack = (peak_index - start) / SAMPLE_RATE
        tail = np.flatnonzero(envelope[peak_index:] >= envelope_peak * 10 ** (float(opts["decay_db"]) / 20))
        decay = int(tail[-1]) / SAMPLE_RATE if len(tail) else 0.0
    else:
        attack = decay = 0.0

    padded_audio = np.pad(x, (0, max(0, frame_size - len(x))))
    frequencies, _, spectrum = stft(
        padded_audio, fs=SAMPLE_RATE, nperseg=frame_size,
        noverlap=frame_size-hop, boundary=None, padded=True,
    )
    power = np.square(np.abs(spectrum))
    mean_power = power.mean(axis=1)
    total = float(mean_power.sum())
    denominator = max(total, floor)
    centroid = float(np.dot(frequencies, mean_power) / denominator)
    low_edge, high_edge = float(opts["low_mid_hz"]), float(opts["mid_high_hz"])
    if not 0 < low_edge < high_edge < SAMPLE_RATE / 2:
        raise ValueError("Energy band boundaries must increase within Nyquist")
    low = float(mean_power[frequencies < low_edge].sum() / denominator)
    middle = float(mean_power[(frequencies >= low_edge) & (frequencies < high_edge)].sum() / denominator)
    high = float(mean_power[frequencies >= high_edge].sum() / denominator)
    rms = float(np.sqrt(np.mean(x * x)))
    crest = float(np.max(np.abs(x)) / max(rms, floor))
    mel_power = _mel_filters(frame_size, bands, float(opts["mel_low_hz"]), float(opts["mel_high_hz"])) @ power
    log_mel = np.log(np.maximum(mel_power, floor)).mean(axis=1)
    result = np.concatenate(([attack, decay, centroid, low, middle, high, crest], log_mel))
    if not np.all(np.isfinite(result)):
        raise ValueError("DSP feature computation produced non-finite values")
    return result
