import math

import pytest

from heartbeat.stats import BackgroundRate, combine, format_odds, round_p


def rate(window=600, prior_events=1, prior_s=60, floor=1 / 600):
    return BackgroundRate(window, prior_events, prior_s, floor)


def test_prior_without_observation_is_one_per_minute():
    background = rate()
    assert background.observed_s == 0
    assert background.count == 0
    assert background.lambda_per_s * 60 == 1


def test_explicit_passive_exposure_excludes_active_gaps_and_accepts_delayed_knocks():
    background = rate()
    background.observe(0, 10)
    background.advance(100)
    background.observe(100, 105)
    assert background.add(5)  # Arrived late, but belonged to passive listening.
    assert background.add(102)
    assert not background.add(50)  # Prompt/window/gate gap.
    assert not background.add(110)  # Not observed yet.
    assert background.observed_s == 15
    assert background.count == 2
    assert background.lambda_per_s == pytest.approx(3 / 75)


def test_sliding_horizon_clips_exposure_and_counts_by_real_stream_time():
    background = rate(window=10)
    background.observe(0, 5)
    background.add(1)
    background.add(4)
    background.advance(12)
    assert background.observed_s == 3
    assert background.count == 1
    background.observe(12, 15)
    assert background.observed_s == 3
    assert background.count == 0
    background.advance(30)
    assert background.observed_s == 0 and background.count == 0


def test_overlapping_and_nested_intervals_do_not_shrink_or_double_count_exposure():
    background = rate()
    background.observe(0, 20)
    background.observe(5, 10)
    background.observe(18, 25)
    assert background.observed_s == 25
    assert background.add(19)


def test_quiet_observation_reduces_rate_but_never_below_floor():
    background = rate(prior_events=0, floor=1 / 600)
    background.observe(0, 600)
    assert background.lambda_per_s == 1 / 600


def test_poisson_exact_match_matches_hand_calculation():
    mu = 0.1
    assert round_p(3, 3, 6, 1 / 60) == pytest.approx(math.exp(-mu) * mu ** 3 / math.factorial(3))


def test_poisson_tolerance_uses_observed_distance_not_entire_allowed_band():
    mu = 0.6
    expected = math.exp(-mu) * sum(mu ** n / math.factorial(n) for n in (2, 3, 4))
    assert round_p(3, 2, 6, 0.1) == pytest.approx(expected)
    assert round_p(3, 4, 6, 0.1) == pytest.approx(expected)
    assert round_p(3, 3, 6, 0.1) < expected
    assert round_p(2, 1, 6, 0.1) is None
    assert round_p(2, 3, 6, 0.1) is None
    assert round_p(4, 2, 6, 0.1) is None


def test_zero_rate_zero_duration_and_large_poisson_mean_are_finite():
    assert round_p(0, 0, 6, 0) == 1
    assert round_p(3, 3, 0, 1) == 0
    assert 0 <= round_p(3, 3, 600, 1000) <= 1


def test_combined_probability_is_product_of_supplied_rounds():
    assert combine([0.1, 0.02, 0.5]) == pytest.approx(0.001)
    assert combine([]) == 1
    with pytest.raises(ValueError):
        combine([0.5, float("nan")])


@pytest.mark.parametrize("probability,words", [
    (1 / 40, "1 in 40"), (1 / 2300, "1 in 2,300"),
    (1 / 2_300_000, "1 in 2.3 million"), (1e-12, "1 in over a billion"),
    (0, "1 in over a billion"), (1, "1 in 1"),
])
def test_odds_use_plain_words(probability, words):
    assert format_odds(probability) == words


@pytest.mark.parametrize("probability", [-1, 2, float("nan"), float("inf")])
def test_invalid_probability_has_clear_error(probability):
    with pytest.raises(ValueError):
        format_odds(probability)
