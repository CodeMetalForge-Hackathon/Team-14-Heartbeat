"""Streaming energy onsets with sample-clock timestamps and delayed raw clips."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import math
from typing import TYPE_CHECKING

import numpy as np
from scipy.signal import butter, sosfilt

if TYPE_CHECKING:
    from .audio import AudioBlock

# Audio and classifier input contracts, rather than detector tuning parameters.
SAMPLE_RATE = 16000
HOP_SAMPLES = 256
CLIP_SAMPLES = 15600
PRE_SAMPLES = 1600
PEAK_SAMPLES = 800


@dataclass
class Onset:
    t: float
    peak: float
    snr_db: float
    gated: bool
    clip: np.ndarray


@dataclass
class _Pending:
    sample: int
    snr_db: float


class OnsetDetector:
    """Detect hop-level energy rises, preserving unfiltered audio for the clip.

    The configured initial floor is used until eligible history is available.
    Each excursion above the SNR and absolute thresholds produces at most one
    onset, subject to the minimum refractory interval. Re-arming requires a hop
    at or below ``floor + rearm_db`` or below the absolute minimum. This
    hysteresis prevents beating resonant modes in a double hit's decaying tail
    from creating a fresh onset when the refractory time expires.
    ``flush`` is terminal and pads clips with zeros when a file ends before
    their post-onset audio is complete.
    """

    def __init__(self, cfg=None):
        if cfg is None:
            from .config import load_profile

            cfg = load_profile("rubble")
        cfg = getattr(cfg, "onset", cfg)

        def value(name):
            return cfg[name] if isinstance(cfg, dict) else getattr(cfg, name)

        self.threshold_db = float(value("threshold_db"))
        self.rearm_db = float(value("rearm_db"))
        self.min_dbfs = float(value("min_dbfs"))
        self.refractory_samples = round(float(value("refractory_s")) * SAMPLE_RATE)
        self.floor_window_samples = round(float(value("floor_window_s")) * SAMPLE_RATE)
        self.floor_percentile = float(value("floor_percentile"))
        self.floor_exclude_samples = round(float(value("floor_exclude_s")) * SAMPLE_RATE)
        self.floor_db = float(value("initial_floor_dbfs"))
        self.level_db = self.floor_db
        self._sos = butter(2, float(value("highpass_hz")), "highpass", fs=SAMPLE_RATE, output="sos")
        self._zi = np.zeros((len(self._sos), 2), dtype=np.float64)
        self._levels: deque[tuple[int, float]] = deque()
        self._raw = np.empty(0, dtype=np.float32)
        self._raw_start = 0
        self._filtered = np.empty(0, dtype=np.float64)
        self._received = 0
        self._processed = 0
        self._origin = 0
        self._started = False
        self._last_onset: int | None = None
        self._in_excursion = False
        self._pending: list[_Pending] = []
        self._gates: list[tuple[float, float]] = []
        self._flushed = False

    def gate(self, t_start: float, t_end: float) -> None:
        """Add a gate, or close an open gate by repeating its start time."""
        t_start, t_end = float(t_start), float(t_end)
        if not math.isfinite(t_start) or math.isnan(t_end) or t_end < t_start:
            raise ValueError("gate requires a finite start and an end at or after it")
        self._gates = [(a, b) for a, b in self._gates if a != t_start]
        self._gates.append((t_start, t_end))
        self._gates.sort()

    def process(self, block: AudioBlock) -> list[Onset]:
        if self._flushed:
            raise ValueError("cannot process audio after flush")
        x = np.asarray(block.x, dtype=np.float32)
        if x.ndim != 1 or not np.all(np.isfinite(x)):
            raise ValueError("audio blocks must contain finite mono samples")
        if not len(x):
            return []
        if not self._started:
            # Round once to the sample lattice; all later times use integers.
            self._origin = round(float(block.t0) * SAMPLE_RATE)
            self._started = True
        self._raw = np.concatenate((self._raw, x))
        filtered, self._zi = sosfilt(self._sos, x, zi=self._zi)
        self._filtered = np.concatenate((self._filtered, filtered))
        self._received += len(x)
        complete = len(self._filtered) // HOP_SAMPLES
        for index in range(complete):
            offset = index * HOP_SAMPLES
            self._process_hop(self._filtered[offset : offset + HOP_SAMPLES])
        self._filtered = self._filtered[complete * HOP_SAMPLES :]
        result = self._emit_ready(final=False)
        self._trim()
        return result

    def flush(self) -> list[Onset]:
        """Finish a file, including a partial hop and zero-padded pending clips."""
        if self._flushed:
            return []
        self._flushed = True
        if len(self._filtered):
            self._process_hop(np.pad(self._filtered, (0, HOP_SAMPLES - len(self._filtered))))
            self._filtered = np.empty(0, dtype=np.float64)
        result = self._emit_ready(final=True)
        self._trim()
        return result

    def _process_hop(self, filtered: np.ndarray) -> None:
        sample = self._processed
        self._processed += HOP_SAMPLES
        rms = float(np.sqrt(np.mean(np.square(filtered))))
        self.level_db = float(20 * np.log10(max(rms, np.finfo(np.float64).tiny)))
        while self._levels and self._levels[0][0] < sample - self.floor_window_samples:
            self._levels.popleft()
        if self._levels:
            self.floor_db = float(np.percentile([level for _, level in self._levels], self.floor_percentile))

        snr = self.level_db - self.floor_db
        outside_refractory = self._last_onset is None or sample - self._last_onset >= self.refractory_samples
        above_threshold = snr >= self.threshold_db and self.level_db >= self.min_dbfs
        if above_threshold and not self._in_excursion:
            if outside_refractory:
                self._pending.append(_Pending(sample, snr))
                self._last_onset = sample
            self._in_excursion = True
        elif snr <= self.rearm_db or self.level_db < self.min_dbfs:
            self._in_excursion = False
        if self._last_onset is None or sample - self._last_onset >= self.floor_exclude_samples:
            self._levels.append((sample, self.level_db))

    def _emit_ready(self, *, final: bool) -> list[Onset]:
        result = []
        remaining = []
        for pending in self._pending:
            start = pending.sample - PRE_SAMPLES
            end = start + CLIP_SAMPLES
            if not final and end > self._received:
                remaining.append(pending)
                continue
            clip = np.zeros(CLIP_SAMPLES, dtype=np.float32)
            available_start = max(start, self._raw_start, 0)
            available_end = min(end, self._received)
            if available_end > available_start:
                clip[available_start - start : available_end - start] = self._raw[
                    available_start - self._raw_start : available_end - self._raw_start
                ]
            t = (self._origin + pending.sample) / SAMPLE_RATE
            result.append(
                Onset(
                    t=t,
                    peak=float(np.max(np.abs(clip[PRE_SAMPLES : PRE_SAMPLES + PEAK_SAMPLES]))),
                    snr_db=float(pending.snr_db),
                    gated=any(a <= t <= b for a, b in self._gates),
                    clip=clip,
                )
            )
        self._pending = remaining
        return result

    def _trim(self) -> None:
        # Keep pre-roll for the next hop and complete clips for waiting onsets.
        keep = max(0, self._processed - PRE_SAMPLES)
        if self._pending:
            keep = min(keep, max(0, self._pending[0].sample - PRE_SAMPLES))
        keep = min(keep, self._received)
        discard = max(0, keep - self._raw_start)
        self._raw = self._raw[discard:]
        self._raw_start += discard
        oldest_sample = self._pending[0].sample if self._pending else self._processed
        oldest = (self._origin + oldest_sample) / SAMPLE_RATE
        self._gates = [(a, b) for a, b in self._gates if b >= oldest]
