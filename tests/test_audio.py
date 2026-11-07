import threading
from types import SimpleNamespace

import numpy as np
import pytest
from scipy.signal import resample_poly
import soundfile as sf

from heartbeat import audio
from heartbeat.audio import MicSource, SAMPLE_RATE, WavSource, list_devices
from heartbeat.events import EventBus


@pytest.mark.parametrize("rate", [48000, 44100, 8000])
def test_wav_conversion_preserves_filter_context_and_sample_time(tmp_path, rate):
    rng = np.random.default_rng(9)
    frames = rate + 23
    samples = rng.normal(0, 0.08, (frames, 2)).astype(np.float32)
    path = tmp_path / "stereo.wav"
    sf.write(path, samples, rate, subtype="FLOAT")

    blocks = list(WavSource(path))
    converted = np.concatenate([block.x for block in blocks])
    divisor = np.gcd(rate, SAMPLE_RATE)
    expected = resample_poly(samples.mean(axis=1), SAMPLE_RATE // divisor, rate // divisor)
    np.testing.assert_allclose(converted, expected, atol=2e-7, rtol=2e-6)
    assert len(converted) == (frames * SAMPLE_RATE + rate - 1) // rate
    count = 0
    for index, block in enumerate(blocks):
        assert block.t0 == count / SAMPLE_RATE
        assert block.x.dtype == np.float32
        assert block.x.ndim == 1
        assert len(block.x) <= 256
        if index < len(blocks) - 1:
            assert len(block.x) == 256
        count += len(block.x)


def test_wav_native_rate_partial_final_block_and_bounded_samples(tmp_path):
    samples = np.array([np.nan, np.inf, -np.inf, 1.5, -1.5, 0.25], dtype=np.float32)
    path = tmp_path / "native.wav"
    sf.write(path, samples, SAMPLE_RATE, subtype="FLOAT")
    blocks = list(WavSource(path, block=4))
    assert [len(block.x) for block in blocks] == [4, 2]
    np.testing.assert_array_equal(np.concatenate([block.x for block in blocks]), [0, 1, -1, 1, -1, 0.25])


def test_wav_realtime_pacing_does_not_change_stream_time(tmp_path, monkeypatch):
    path = tmp_path / "paced.wav"
    sf.write(path, np.zeros(700, dtype=np.float32), SAMPLE_RATE)
    clock = [100.0]
    sleeps = []

    def sleep(delay):
        sleeps.append(delay)
        clock[0] += delay

    monkeypatch.setattr(audio.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(audio.time, "sleep", sleep)
    blocks = list(WavSource(path, realtime=True))
    assert [block.t0 for block in blocks] == [0.0, 256 / SAMPLE_RATE, 512 / SAMPLE_RATE]
    assert sum(sleeps) == pytest.approx(512 / SAMPLE_RATE)


def test_wav_close_stops_iteration(tmp_path):
    path = tmp_path / "close.wav"
    sf.write(path, np.zeros(1024, dtype=np.float32), SAMPLE_RATE)
    source = WavSource(path)
    stream = iter(source)
    next(stream)
    source.close()
    assert list(stream) == []


def _config(queue_blocks=32):
    return SimpleNamespace(sample_rate=SAMPLE_RATE, block=256, queue_blocks=queue_blocks)


def _fake_input(monkeypatch, chunks, *, fail_native=False, fail_all=False, rate=48000, status=None, active=True):
    streams, attempts = [], []

    class Stream:
        def __init__(self, **kwargs):
            attempts.append(kwargs)
            if fail_all or (fail_native and kwargs["samplerate"] == SAMPLE_RATE):
                raise RuntimeError("Unsupported sample rate")
            self.callback = kwargs["callback"]
            self.active = active
            self.closed = False
            streams.append(self)

        def start(self):
            for chunk in chunks:
                self.callback(chunk, len(chunk), None, status)

        def stop(self):
            self.active = False

        def close(self):
            self.closed = True

    fake = SimpleNamespace(
        InputStream=Stream,
        query_devices=lambda *args: {"default_samplerate": rate},
    )
    monkeypatch.setattr(audio, "_sounddevice", lambda: fake)
    return streams, attempts


def test_microphone_queue_drops_oldest_and_times_delivered_samples(monkeypatch):
    chunks = [np.full((256, 1), index / 10, dtype=np.float32) for index in range(4)]
    streams, _ = _fake_input(monkeypatch, chunks)
    events = []
    bus = EventBus()
    bus.subscribe(events.append)
    source = MicSource(cfg=_config(queue_blocks=2), bus=bus)
    stream = iter(source)
    blocks = [next(stream), next(stream)]
    source.close()
    assert list(stream) == []
    assert source.overruns == 2
    assert [block.t0 for block in blocks] == [0, 256 / SAMPLE_RATE]
    np.testing.assert_allclose(blocks[0].x, 0.2)
    np.testing.assert_allclose(blocks[1].x, 0.3)
    assert events[0].data["code"] == "audio_overrun"
    assert events[0].data["overruns"] == 2
    assert events[0].t == 0
    assert streams[0].closed


def test_microphone_falls_back_to_default_rate_without_chunk_artifacts(monkeypatch):
    rng = np.random.default_rng(10)
    original = rng.normal(0, 0.1, (768 * 6, 1)).astype(np.float32)
    chunks = list(np.split(original, 6))
    streams, attempts = _fake_input(monkeypatch, chunks, fail_native=True)
    events = []
    bus = EventBus()
    bus.subscribe(events.append)
    source = MicSource(cfg=_config(), bus=bus)
    stream = iter(source)
    blocks = [next(stream) for _ in range(4)]
    source.close()
    stream.close()
    assert [attempt["samplerate"] for attempt in attempts] == [16000, 48000]
    assert [attempt["blocksize"] for attempt in attempts] == [256, 768]
    expected = resample_poly(original[:, 0], 1, 3)
    np.testing.assert_allclose(np.concatenate([block.x for block in blocks]), expected[:1024], atol=2e-7)
    assert [block.t0 for block in blocks] == [index * 256 / SAMPLE_RATE for index in range(4)]
    assert events[0].data["code"] == "audio_resampling"
    assert source.input_rate == 48000
    assert streams[0].closed


def test_unavailable_microphone_reports_error_and_ends(monkeypatch):
    _fake_input(monkeypatch, [], fail_all=True)
    events = []
    bus = EventBus()
    bus.subscribe(events.append)
    assert list(MicSource(cfg=_config(), bus=bus)) == []
    assert events[0].type == "error"
    assert events[0].data["code"] == "audio_open_failed"


def test_input_status_and_inactive_stream_are_reported(monkeypatch):
    _fake_input(monkeypatch, [np.zeros((256, 1))], status="input overflow", active=False)
    bus = EventBus()
    events = []
    bus.subscribe(events.append)
    blocks = list(MicSource(cfg=_config(), bus=bus))
    assert len(blocks) == 1
    assert [event.data["code"] for event in events] == ["audio_input_status", "audio_input_stopped"]


def test_close_wakes_a_waiting_microphone_consumer(monkeypatch):
    started = threading.Event()
    streams, _ = _fake_input(monkeypatch, [])
    source = MicSource(cfg=_config())

    def consume():
        started.set()
        list(source)

    worker = threading.Thread(target=consume, daemon=True)
    worker.start()
    assert started.wait(timeout=1)
    source.close()
    worker.join(timeout=1)
    assert not worker.is_alive()
    assert all(stream.closed for stream in streams)


def test_device_listing_marks_input_and_output_defaults(monkeypatch):
    fake = SimpleNamespace(
        default=SimpleNamespace(device=(1, 0)),
        query_devices=lambda: [
            {"name": "Speakers", "max_input_channels": 0, "max_output_channels": 2},
            {"name": "Contact mic", "max_input_channels": 1, "max_output_channels": 0},
        ],
    )
    monkeypatch.setattr(audio, "_sounddevice", lambda: fake)
    devices = list_devices()
    assert devices[0]["is_default_output"]
    assert devices[1]["is_default_input"]
    assert devices[1]["default"]
    assert devices[1]["index"] == 1


def test_device_query_failure_emits_error_and_returns_no_devices(monkeypatch):
    def unavailable():
        raise RuntimeError("PortAudio unavailable")

    monkeypatch.setattr(audio, "_sounddevice", unavailable)
    events = []
    bus = EventBus()
    bus.subscribe(events.append)
    assert list_devices(bus=bus) == []
    assert events[0].type == "error"
    assert events[0].data["code"] == "audio_devices_failed"
    assert "PortAudio unavailable" in events[0].data["message"]
