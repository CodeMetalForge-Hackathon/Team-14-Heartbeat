"""Detection checks use synthetic scenes and independent impulse/noise fixtures."""

import numpy as np
import pytest
from scipy.signal import butter, sosfilt

from heartbeat.audio import AudioBlock
from heartbeat.config import load_profile
from heartbeat.onset import CLIP_SAMPLES, HOP_SAMPLES, PRE_SAMPLES, SAMPLE_RATE, OnsetDetector
from heartbeat.synth import background, scene


def detect(x, *, chunk=HOP_SAMPLES, origin=0.0):
    detector = OnsetDetector(load_profile())
    found = []
    for start in range(0, len(x), chunk):
        found.extend(detector.process(AudioBlock(origin + start / SAMPLE_RATE, x[start : start + chunk])))
    found.extend(detector.flush())
    return found


def independent_hit_scene(times, duration_s=4.0):
    """A short 650 Hz contact resonance, without using the knock generator."""
    rng = np.random.default_rng(382)
    x = (rng.standard_normal(round(duration_s * SAMPLE_RATE)) * 10 ** (-50 / 20)).astype(np.float32)
    t = np.arange(round(0.1 * SAMPLE_RATE)) / SAMPLE_RATE
    hit = 0.5 * np.sin(2 * np.pi * 650 * t) * np.exp(-t / 0.01)
    for when in times:
        start = round(when * SAMPLE_RATE)
        end = min(len(x), start + len(hit))
        x[start:end] += hit[: end - start]
    return x


def test_twenty_knocks_among_distractors():
    events = []
    distractors = ("footstep", "clap", "speech_like", "drop")
    for index in range(20):
        events.append((1.037 + index * 3, "knock", -6 - index % 4))
        events.append((2.1 + index * 3, distractors[index % len(distractors)], -12))
    x, truth = scene(62, events, seed=39)
    found = detect(x)
    knocks = [event["t"] for event in truth if event["kind"] == "knock"]
    matched = sum(any(abs(onset.t - t) <= 0.025 for onset in found) for t in knocks)
    assert len(knocks) == 20
    assert matched >= 19


@pytest.mark.parametrize("seed", [1, 29])
def test_stationary_background_has_no_onsets(seed):
    assert detect(background(-50, 30, seed=seed)) == []


def test_independent_stationary_white_noise_has_no_onsets():
    rng = np.random.default_rng(402)
    x = (rng.standard_normal(30 * SAMPLE_RATE) * 10 ** (-50 / 20)).astype(np.float32)
    assert detect(x) == []


def test_sixty_ms_double_hit_is_one_onset():
    found = detect(independent_hit_scene([1.0, 1.06]))
    assert len(found) == 1
    assert abs(found[0].t - 1.0) <= 0.025


@pytest.mark.parametrize("seed", range(20))
def test_realistic_double_hit_decay_does_not_retrigger(seed):
    x, _ = scene(3, [(1.0, "knock", -6), (1.06, "knock", -6)], seed=seed)
    found = detect(x)
    assert len(found) == 1
    assert abs(found[0].t - 1.0) <= 0.025


@pytest.mark.parametrize("gap", [0.2, 0.3])
@pytest.mark.parametrize("seed", range(20))
def test_separate_realistic_knocks_beyond_refractory_remain_distinct(seed, gap):
    x, _ = scene(3, [(1.0, "knock", -6), (1.0 + gap, "knock", -6)], seed=seed)
    found = detect(x)
    assert len(found) == 2
    assert abs(found[0].t - 1.0) <= 0.025
    assert abs(found[1].t - (1.0 + gap)) <= 0.025


def test_sustained_sound_does_not_create_periodic_onsets():
    x = independent_hit_scene([], duration_s=8)
    t = np.arange(5 * SAMPLE_RATE) / SAMPLE_RATE
    x[SAMPLE_RATE : 6 * SAMPLE_RATE] += (0.2 * np.sin(2 * np.pi * 700 * t)).astype(np.float32)
    found = detect(x)
    assert len(found) == 1
    assert abs(found[0].t - 1.0) <= 0.025


def test_configured_initial_floor_allows_a_first_hop_transient():
    found = detect(independent_hit_scene([0.001], duration_s=2))
    assert len(found) == 1 and found[0].t == 0.0
    np.testing.assert_array_equal(found[0].clip[:PRE_SAMPLES], 0)


def test_clips_use_raw_audio_with_exact_preroll_and_peak():
    x = independent_hit_scene([1.007])
    found = detect(x)
    assert len(found) == 1
    onset = found[0]
    sample = round(onset.t * SAMPLE_RATE)
    assert onset.clip.dtype == np.float32
    assert onset.clip.shape == (CLIP_SAMPLES,)
    np.testing.assert_array_equal(onset.clip, x[sample - PRE_SAMPLES : sample - PRE_SAMPLES + CLIP_SAMPLES])
    assert onset.peak == float(np.max(np.abs(x[sample : sample + 800])))


def test_emission_waits_for_the_complete_clip():
    x = independent_hit_scene([1.0])
    detector = OnsetDetector(load_profile().onset)
    found = []
    for start in range(0, len(x), HOP_SAMPLES):
        fresh = detector.process(AudioBlock(start / SAMPLE_RATE, x[start : start + HOP_SAMPLES]))
        for onset in fresh:
            assert start + HOP_SAMPLES >= round(onset.t * SAMPLE_RATE) - PRE_SAMPLES + CLIP_SAMPLES
        found.extend(fresh)
    assert len(found) == 1
    assert detector.flush() == []


def test_flush_zero_pads_a_clip_at_eof_and_is_idempotent():
    x = independent_hit_scene([0.41], duration_s=0.5)
    detector = OnsetDetector(load_profile())
    for start in range(0, len(x), HOP_SAMPLES):
        assert detector.process(AudioBlock(start / SAMPLE_RATE, x[start : start + HOP_SAMPLES])) == []
    found = detector.flush()
    assert len(found) == 1
    onset = found[0]
    start = round(onset.t * SAMPLE_RATE) - PRE_SAMPLES
    np.testing.assert_array_equal(onset.clip[: len(x) - start], x[start:])
    np.testing.assert_array_equal(onset.clip[len(x) - start :], 0)
    assert detector.flush() == []


def test_sample_clock_is_invariant_to_chunk_size():
    x = independent_hit_scene([1.007, 2.131])
    regular = detect(x)
    irregular = detect(x, chunk=197, origin=11.0)
    assert len(regular) == len(irregular) == 2
    for left, right in zip(regular, irregular):
        assert right.t == left.t + 11.0
        assert round(left.t * SAMPLE_RATE) % HOP_SAMPLES == 0
        np.testing.assert_array_equal(left.clip, right.clip)


def test_open_gate_closes_and_finite_gate_marks_only_its_onsets():
    x = independent_hit_scene([1.0, 2.0, 3.0], duration_s=5)
    detector = OnsetDetector(load_profile())
    detector.gate(0.9, float("inf"))
    detector.gate(2.9, 3.1)
    found = []
    closed = False
    for start in range(0, len(x), HOP_SAMPLES):
        t = start / SAMPLE_RATE
        if t >= 1.5 and not closed:
            detector.gate(0.9, 1.5)
            closed = True
        found.extend(detector.process(AudioBlock(t, x[start : start + HOP_SAMPLES])))
    found.extend(detector.flush())
    assert [onset.gated for onset in found] == [True, False, True]


def test_highpass_state_and_meter_match_whole_signal_reference():
    rng = np.random.default_rng(72)
    t = np.arange(HOP_SAMPLES * 30) / SAMPLE_RATE
    x = (0.01 * rng.standard_normal(len(t)) + 0.02 * np.sin(2 * np.pi * 30 * t)).astype(np.float32)
    cfg = load_profile().onset
    filtered = sosfilt(butter(2, cfg.highpass_hz, "highpass", fs=SAMPLE_RATE, output="sos"), x)
    detector = OnsetDetector(cfg)
    for start in range(0, len(x), HOP_SAMPLES):
        detector.process(AudioBlock(start / SAMPLE_RATE, x[start : start + HOP_SAMPLES]))
        expected = 20 * np.log10(np.sqrt(np.mean(filtered[start : start + HOP_SAMPLES] ** 2)))
        assert detector.level_db == pytest.approx(expected)


def test_floor_history_excludes_post_onset_hops():
    x = independent_hit_scene([1.0], duration_s=2)
    detector = OnsetDetector(load_profile())
    floors = []
    for start in range(0, round(1.25 * SAMPLE_RATE), HOP_SAMPLES):
        detector.process(AudioBlock(start / SAMPLE_RATE, x[start : start + HOP_SAMPLES]))
        if 1.01 <= start / SAMPLE_RATE <= 1.24:
            floors.append(detector.floor_db)
    assert len(floors) > 1
    assert max(floors) == min(floors)
