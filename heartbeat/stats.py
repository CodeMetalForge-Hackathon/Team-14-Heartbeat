"""Poisson background-model likelihoods, not a probability a person is present.

The model assumes independent background events at a stationary rate estimated
only from passive listening. Correlated impacts, periodic machinery, changing
noise, adaptive response windows and repeated trials violate that simplification.
Multiplying round probabilities is useful evidence under the model; it is not a
calibrated posterior probability of humanity or a session-wide false alarm rate.
Likelihood alone cannot reject high-rate or correlated noise. DialogEngine also
requires quiet pre-listening, bounded expected background counts, a prompt start
and silence after an answer. The rate object survives operator dialog resets.
"""

from __future__ import annotations

from bisect import bisect_left, insort
from collections import deque
import math

from scipy.stats import poisson


class BackgroundRate:
    """A sample-time sliding rate with explicitly supplied passive exposure.

    ``observe(start, end)`` records passive intervals, ``add(t)`` accepts only
    detections inside them, and ``advance(t)`` ages both out of the same real
    stream-time window. Inactive gaps never become quiet observation time.
    Retained intervals allow delayed detections from earlier passive listening.
    Callers include every ungated interval outside response windows, including
    cooldowns and interview gaps; a dialog reset does not reset this estimator.
    """

    def __init__(self, rate_window_s: float, prior_events: float, prior_s: float, lambda_floor: float):
        values = (rate_window_s, prior_events, prior_s, lambda_floor)
        if not all(math.isfinite(float(value)) for value in values):
            raise ValueError("background-rate parameters must be finite")
        if rate_window_s <= 0 or prior_s <= 0 or prior_events < 0 or lambda_floor < 0:
            raise ValueError("background-rate window/prior time must be positive; counts/floor nonnegative")
        self.rate_window_s = float(rate_window_s)
        self.prior_events = float(prior_events)
        self.prior_s = float(prior_s)
        self.lambda_floor = float(lambda_floor)
        self._now = 0.0
        self._intervals: deque[tuple[float, float]] = deque()
        self._events: list[float] = []

    def advance(self, t: float) -> None:
        t = float(t)
        if not math.isfinite(t) or t < 0:
            raise ValueError("stream time must be finite and nonnegative")
        self._now = max(self._now, t)
        cutoff = self._now - self.rate_window_s
        while self._intervals and self._intervals[0][1] < cutoff:
            self._intervals.popleft()
        if self._intervals and self._intervals[0][0] < cutoff:
            _, end = self._intervals.popleft()
            self._intervals.appendleft((cutoff, end))
        del self._events[:bisect_left(self._events, cutoff)]

    def observe(self, start: float, end: float) -> None:
        start, end = float(start), float(end)
        if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end < start:
            raise ValueError("passive interval must have finite, nonnegative, ordered endpoints")
        if self._intervals and start < self._intervals[-1][0]:
            raise ValueError("passive intervals must be observed in stream order")
        if self._intervals and start <= self._intervals[-1][1]:
            previous_start, previous_end = self._intervals.pop()
            start, end = previous_start, max(previous_end, end)
        self._intervals.append((start, end))
        self.advance(end)

    def add(self, t: float) -> bool:
        """Count a delayed knock only when its timestamp was passive exposure."""
        t = float(t)
        if not math.isfinite(t):
            return False
        if any(start <= t <= end for start, end in self._intervals):
            insort(self._events, t)
            return True
        return False

    @property
    def observed_s(self) -> float:
        return float(sum(end - start for start, end in self._intervals))

    @property
    def count(self) -> int:
        return len(self._events)

    @property
    def lambda_per_s(self) -> float:
        return float(max(self.lambda_floor, (self.count + self.prior_events) / (self.observed_s + self.prior_s)))


def round_p(k: int, n: int, window_s: float, lam: float) -> float | None:
    """Likelihood of the matching count interval under a Poisson background.

    Exact answers use a single count's mass. An allowed one-count deviation
    around targets above two includes the complete symmetric interval. Callers
    supply the nominal response duration rather than selecting a shorter period
    retrospectively from the last knock.
    """
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in (k, n)):
        raise ValueError("expected and heard counts must be nonnegative integers")
    if not math.isfinite(float(window_s)) or not math.isfinite(float(lam)) or window_s < 0 or lam < 0:
        raise ValueError("window duration and background rate must be finite and nonnegative")
    distance = abs(n - k)
    if distance > (0 if k <= 2 else 1):
        return None
    mu = float(lam) * float(window_s)
    return float(min(1.0, sum(float(poisson.pmf(count, mu)) for count in range(max(0, k - distance), k + distance + 1))))


def combine(ps) -> float:
    """Multiply independent round likelihoods; an empty sequence has mass one."""
    probabilities = [float(p) for p in ps]
    if any(not math.isfinite(p) or not 0 <= p <= 1 for p in probabilities):
        raise ValueError("probabilities must be finite and between zero and one")
    return float(math.prod(probabilities))


def format_odds(p: float) -> str:
    p = float(p)
    if not math.isfinite(p) or not 0 <= p <= 1:
        raise ValueError("probability must be finite and between zero and one")
    if p == 0 or p < 1 / 1_000_000_000:
        return "1 in over a billion"
    odds = 1 / p
    if odds >= 1_000_000_000:
        return "1 in 1 billion"
    if odds >= 1_000_000:
        millions = f"{odds / 1_000_000:.1f}".rstrip("0").rstrip(".")
        return f"1 in {millions} million"
    return f"1 in {round(odds):,}"
