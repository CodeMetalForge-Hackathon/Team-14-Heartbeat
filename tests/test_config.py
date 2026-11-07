from pathlib import Path

import pytest

from heartbeat.config import load_profile


def test_named_profile_and_reserved_sections():
    profile = load_profile("rubble")
    assert profile.audio.sample_rate == 16000
    assert profile.audio.block == 256
    assert profile.onset.highpass_hz == 60
    assert profile.onset.threshold_db == 12
    assert profile.classifier.name == "none"
    assert profile.classifier.threshold == 0.5
    assert profile.dialog["first_pattern"] == 3 and profile.prompter["gate_tail_s"] == 0.6 and profile.server == {}


def test_custom_profile_inherits_and_preserves_future_sections(tmp_path):
    path = tmp_path / "custom.yaml"
    path.write_text("onset:\n  threshold_db: 14\ndialog:\n  future_key: 42\n")
    profile = load_profile(path)
    assert profile.onset.threshold_db == 14
    assert profile.onset.highpass_hz == 60
    assert profile.dialog == {**load_profile("rubble").dialog, "future_key": 42}


@pytest.mark.parametrize("text,match", [
    ("audio:\n  mystery: 1", "Unknown audio keys"),
    ("onset:\n  threshhold_db: 12", "Unknown onset keys"),
    ("classifier:\n  extra: 1", "Unknown classifier keys"),
    ("other: {}", "Unknown profile keys"),
    ("audio:\n  synth:\n    knock:\n      typo: 2", "Unknown audio.synth.knock key"),
    ("onset: []", "must be a mapping"),
    ("audio:\n  sample_rate: 48000", "Audio contract"),
    ("audio:\n  queue_blocks: 0", "at least 1"),
    ("audio:\n  level_hz: 0", "positive"),
    ("onset:\n  highpass_hz: 9000", "Nyquist"),
    ("onset:\n  rearm_db: 12", "below threshold_db"),
    ("classifier:\n  threshold: 2", "at most 1"),
    ("onset:\n  threshold_db: .nan", "finite number"),
    ("audio:\n  block: true", "finite number"),
    ("classifier:\n  name: null", "nonempty string"),
])
def test_profile_rejects_unknown_or_invalid_settings(tmp_path, text, match):
    path = tmp_path / "bad.yaml"
    path.write_text(text)
    with pytest.raises(ValueError, match=match):
        load_profile(path)


def test_profile_load_does_not_depend_on_working_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert load_profile("rubble").audio.sample_rate == 16000


def test_profile_missing_path_has_context(tmp_path):
    with pytest.raises(ValueError, match="Cannot load profile"):
        load_profile(tmp_path / "missing.yaml")


def test_profile_can_find_installed_wheel_data(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from heartbeat import config

    source = config._PROFILE_DIR / "rubble.yaml"
    installed = tmp_path / "environment" / "rubble.yaml"
    installed.parent.mkdir()
    installed.write_bytes(source.read_bytes())
    entry = SimpleNamespace(name="rubble.yaml", locate=lambda: installed)
    monkeypatch.setattr(config, "_PROFILE_DIR", tmp_path / "absent-source-tree")
    monkeypatch.setattr(config, "distribution", lambda name: SimpleNamespace(files=[entry]))
    assert load_profile("rubble").onset.threshold_db == 12
