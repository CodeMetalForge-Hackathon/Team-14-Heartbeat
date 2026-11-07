import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf

from heartbeat import cli


class FakeMic:
    amplitude = 0.25
    interrupted = False
    broken = False
    reported_error = False
    instances = []

    def __init__(self, *args, **kwargs):
        self.closed = False
        self.kwargs = kwargs
        self.instances.append(self)

    def __iter__(self):
        if self.broken:
            raise RuntimeError("microphone unavailable")
        for index in range(4):
            yield SimpleNamespace(
                t0=index * 256 / 16000,
                x=np.full(256, self.amplitude, dtype=np.float32),
            )
            if self.interrupted:
                raise KeyboardInterrupt
            if self.reported_error:
                self.kwargs["bus"].publish("error", 256 / 16000, message="stream stopped")
                return

    def close(self):
        self.closed = True


@pytest.fixture
def fake_recording(monkeypatch, tmp_path):
    FakeMic.amplitude = 0.25
    FakeMic.interrupted = False
    FakeMic.broken = False
    FakeMic.reported_error = False
    FakeMic.instances = []
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "MicSource", FakeMic)
    monkeypatch.setattr(cli, "_countdown", lambda seconds: None)
    return ["record", "--label", "knock", "--surface", " Brick / Wood ", "--desc", "Test #1"]


def test_record_fixed_samples_pcm16_and_collision_safe_names(fake_recording):
    args = fake_recording + ["--seconds", "0.025", "--device", "2"]
    assert cli.main(args) == 0
    assert cli.main(args) == 0
    paths = sorted(Path("data/raw").glob("*.wav"))
    assert [path.name for path in paths] == [
        "knock__brick-wood__test-1__01.wav", "knock__brick-wood__test-1__02.wav"
    ]
    for path in paths:
        assert sf.info(path).subtype == "PCM_16"
        samples, rate = sf.read(path, dtype="float32")
        assert rate == 16000
        assert samples.shape == (400,)
        np.testing.assert_allclose(samples, 0.25, atol=1 / 32768)
    assert all(source.closed for source in FakeMic.instances)
    assert FakeMic.instances[0].kwargs["device"] == 2


@pytest.mark.parametrize("amplitude, message", [(1.0, "clipped"), (0.001, "very quiet")])
def test_record_warnings(fake_recording, capsys, amplitude, message):
    FakeMic.amplitude = amplitude
    assert cli.main(fake_recording + ["--seconds", "0.016"]) == 0
    assert message in capsys.readouterr().out


def test_record_until_ctrl_c_keeps_audio(fake_recording):
    FakeMic.interrupted = True
    assert cli.main(fake_recording) == 0
    path = next(Path("data/raw").glob("*.wav"))
    assert sf.info(path).frames == 256
    assert FakeMic.instances[-1].closed


def test_record_device_error_is_reported_and_empty_file_removed(fake_recording, capsys):
    FakeMic.broken = True
    assert cli.main(fake_recording + ["--seconds", "0.016"]) == 1
    assert "microphone unavailable" in capsys.readouterr().err
    assert list(Path("data/raw").glob("*.wav")) == []
    assert FakeMic.instances[-1].closed


def test_record_partial_audio_survives_reported_device_failure(fake_recording, capsys):
    FakeMic.reported_error = True
    assert cli.main(fake_recording) == 1
    assert "stream stopped" in capsys.readouterr().err
    path = next(Path("data/raw").glob("*.wav"))
    assert sf.info(path).frames == 256
    assert FakeMic.instances[-1].closed


def test_devices_uses_inventory_without_opening_input(monkeypatch, capsys):
    monkeypatch.setattr(cli, "find_stick_ports", lambda **kwargs: [], raising=False)
    monkeypatch.setattr(cli, "list_devices", lambda **kwargs: [{
        "index": 4, "name": "Test input", "max_input_channels": 1,
        "max_output_channels": 0, "is_default_input": True,
    }])
    assert cli.main(["devices"]) == 0
    output = capsys.readouterr().out
    assert "Test input" in output
    assert "in" in output


def test_device_query_failure_is_an_error_event(monkeypatch, capsys):
    monkeypatch.setattr(cli, "find_stick_ports", lambda **kwargs: [], raising=False)
    def unavailable(*, bus):
        bus.publish("error", 0.0, message="audio backend unavailable")
        return []

    monkeypatch.setattr(cli, "list_devices", unavailable)
    assert cli.main(["devices"]) == 1
    assert "audio backend unavailable" in capsys.readouterr().err


def test_synth_writes_seeded_pcm_and_ground_truth(tmp_path):
    out = tmp_path / "scene.wav"
    args = ["synth", "--out", str(out), "--seconds", "3", "--seed", "11"]
    assert cli.main(args) == 0
    first_audio = out.read_bytes()
    first_truth = Path(str(out) + ".json").read_bytes()
    assert cli.main(args) == 0
    assert out.read_bytes() == first_audio
    assert Path(str(out) + ".json").read_bytes() == first_truth
    truth = json.loads(first_truth)
    assert truth and all(set(event) == {"t", "kind"} for event in truth)
    assert sf.info(out).frames == 48000
    assert sf.info(out).samplerate == 16000


def test_listen_synthetic_wav_never_opens_mic(monkeypatch, tmp_path, capsys):
    out = tmp_path / "scene.wav"
    assert cli.main(["synth", "--out", str(out), "--seconds", "3"]) == 0

    def no_microphone(*args, **kwargs):
        pytest.fail("WAV replay must not open a microphone")

    monkeypatch.setattr(cli, "MicSource", no_microphone)
    assert cli.main(["listen", "--wav", str(out)]) == 0
    output = capsys.readouterr().out
    assert "KNOCK" in output
    assert "dBFS" in output


def test_listen_missing_wav_is_reported(tmp_path, capsys):
    assert cli.main(["listen", "--wav", str(tmp_path / "missing.wav")]) == 1
    assert "error:" in capsys.readouterr().err


@pytest.mark.parametrize("seconds", ["0", "-1", "nan", "inf"])
def test_invalid_durations_fail_before_audio_access(seconds):
    with pytest.raises(SystemExit):
        cli.main(["synth", "--out", "unused.wav", "--seconds", seconds])


def test_listen_device_and_wav_are_exclusive():
    with pytest.raises(SystemExit):
        cli.main(["listen", "--device", "0", "--wav", "unused.wav"])
