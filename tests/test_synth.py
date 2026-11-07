import numpy as np
import pytest

from heartbeat.config import load_profile
from heartbeat import synth


@pytest.mark.parametrize("name", ["knock", "footstep", "clap", "speech_like", "drop"])
def test_sound_generators_are_seeded_finite_mono_float32(name):
    generate = getattr(synth, name)
    x = generate(seed=41)
    np.testing.assert_array_equal(x, generate(seed=41))
    assert not np.array_equal(x, generate(seed=42))
    assert x.ndim == 1 and x.dtype == np.float32
    assert np.isfinite(x).all()
    assert np.max(np.abs(x)) <= 1
    assert np.max(np.abs(x)) == pytest.approx(load_profile().audio.synth["peak"])


def test_knock_is_a_short_decaying_contact_sound():
    x = synth.knock(seed=22)
    early = np.sqrt(np.mean(x[:800] ** 2))
    late = np.sqrt(np.mean(x[-800:] ** 2))
    assert late < early / 5


def test_footstep_has_a_low_frequency_thump():
    x = synth.footstep(seed=23)
    frequencies = np.fft.rfftfreq(len(x), 1 / synth.SAMPLE_RATE)
    dominant = frequencies[np.argmax(np.abs(np.fft.rfft(x)))]
    assert 45 <= dominant <= 125


def test_speech_like_noise_is_band_limited():
    x = synth.speech_like(seed=5)
    frequencies = np.fft.rfftfreq(len(x), 1 / synth.SAMPLE_RATE)
    power = np.abs(np.fft.rfft(x)) ** 2
    assert power[(frequencies >= 280) & (frequencies <= 3020)].sum() / power.sum() > 0.99


def test_background_has_requested_rms_and_seed():
    x = synth.background(-50, 3, seed=9)
    rms_dbfs = 20 * np.log10(np.sqrt(np.mean(x.astype(np.float64) ** 2)))
    assert rms_dbfs == pytest.approx(-50, abs=1e-5)
    assert x.shape == (3 * synth.SAMPLE_RATE,)
    np.testing.assert_array_equal(x, synth.background(-50, 3, seed=9))
    assert not np.array_equal(x, synth.background(-50, 3, seed=10))


def test_scene_ground_truth_is_sample_aligned_and_omits_outside_events():
    events = [(0.987654, "knock", -6), (2.0, "clap", -10), (4.1, "drop", 0)]
    x, truth = synth.scene(4, events=events, seed=6)
    assert x.dtype == np.float32 and x.shape == (4 * synth.SAMPLE_RATE,)
    assert truth == [{"t": round(0.987654 * synth.SAMPLE_RATE) / synth.SAMPLE_RATE, "kind": "knock"},
                     {"t": 2.0, "kind": "clap"}]
    repeat, repeat_truth = synth.scene(4, events=events, seed=6)
    np.testing.assert_array_equal(x, repeat)
    assert repeat_truth == truth
    # Ground truth denotes real waveform starts, not an unrelated label list.
    for event in truth:
        start = round(event["t"] * synth.SAMPLE_RATE)
        assert np.max(np.abs(x[start : start + 800])) > 0.05


def test_default_scene_contains_knocks_and_distractors_from_profile():
    x, truth = synth.scene(10, seed=3)
    settings = load_profile().audio.synth["scene"]
    assert len(x) == 10 * synth.SAMPLE_RATE
    assert {event["kind"] for event in truth} == set(settings["kinds"])
    assert truth[0]["t"] == settings["first_event_s"]
    assert truth[1]["t"] - truth[0]["t"] == pytest.approx(settings["interval_s"])


def test_scene_invalid_sound_or_timing_has_clear_error():
    with pytest.raises(ValueError, match="unknown synthetic sound"):
        synth.scene(2, [(1, "unknown", 0)])
    with pytest.raises(ValueError, match="nonnegative"):
        synth.scene(2, [(-1, "knock", 0)])
    with pytest.raises(ValueError, match="duration_s"):
        synth.scene(-1, [])


def test_empty_scene_and_explicit_no_events():
    x, truth = synth.scene(0, [])
    assert x.shape == (0,) and truth == []
    _, truth = synth.scene(2, [])
    assert truth == []
