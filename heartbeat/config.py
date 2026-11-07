"""Typed profile sections; hardware-independent defaults live in YAML."""

from dataclasses import dataclass, fields
from importlib.metadata import PackageNotFoundError, distribution
from math import isfinite
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class AudioConfig:
    sample_rate: int
    block: int
    queue_blocks: int
    level_hz: float
    console_level_s: float
    meter_min_dbfs: float
    meter_max_dbfs: float
    meter_width: int
    record_countdown_s: int
    record_clip_peak: float
    record_quiet_peak: float
    synth: dict[str, Any]
    stick_baud: int = 921600
    stick_gain_db: float = 0.0


@dataclass(frozen=True)
class OnsetConfig:
    highpass_hz: float
    threshold_db: float
    rearm_db: float
    min_dbfs: float
    refractory_s: float
    floor_window_s: float
    floor_percentile: float
    floor_exclude_s: float
    initial_floor_dbfs: float


@dataclass(frozen=True)
class ClassifierConfig:
    name: str
    threshold: float
    targets: tuple[str, ...] = ("Knock", "Tap", "Thump, thud")
    model_dir: str = "models"
    head_path: str = "models/knock_head.joblib"
    num_threads: int = 2
    features: dict[str, Any] | None = None
    heuristic: dict[str, Any] | None = None
    training: dict[str, Any] | None = None
    evaluation: dict[str, Any] | None = None


@dataclass(frozen=True)
class Profile:
    audio: AudioConfig
    onset: OnsetConfig
    classifier: ClassifierConfig
    dialog: dict[str, Any]
    prompter: dict[str, Any]
    server: dict[str, Any]


_PROFILE_DIR = Path(__file__).resolve().parent.parent / "profiles"
_SECTIONS = ("audio", "onset", "classifier", "dialog", "prompter", "server")


def _profile_dir() -> Path:
    if (_PROFILE_DIR / "rubble.yaml").is_file():
        return _PROFILE_DIR
    # Wheel data installs outside site-packages. RECORD locates it for venv,
    # --user and --target installs without guessing an installation prefix.
    try:
        for entry in distribution("heartbeat").files or ():
            if entry.name == "rubble.yaml":
                return Path(entry.locate()).parent
    except PackageNotFoundError:
        pass
    return _PROFILE_DIR


def _read(path: Path) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as stream:
            data = yaml.safe_load(stream)
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"Cannot load profile {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"Profile {path} must contain a mapping of sections")
    unknown = data.keys() - set(_SECTIONS)
    if unknown:
        raise ValueError(f"Unknown profile keys: {', '.join(map(str, unknown))}")
    for key, value in data.items():
        if not isinstance(value, dict):
            raise ValueError(f"Profile section {key!r} must be a mapping")
    return data


def _merge(default: dict, override: dict) -> dict:
    result = dict(default)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result


def _section(cls, name: str, data: dict):
    expected = {field.name for field in fields(cls)}
    unknown = data.keys() - expected
    if unknown:
        raise ValueError(f"Unknown {name} keys: {', '.join(map(str, unknown))}")
    missing = expected - data.keys()
    if missing:
        raise ValueError(f"Missing {name} keys: {', '.join(sorted(missing))}")
    return cls(**data)


def _number(name: str, value: Any, *, minimum=None, maximum=None, integer=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    if integer and not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} must be at most {maximum}")


def _validate_synth(data: dict, template: dict, prefix="audio.synth"):
    """Reject misspelled synthesis knobs, including nested overrides."""
    if not isinstance(data, dict):
        raise ValueError(f"{prefix} must be a mapping")
    for key, value in data.items():
        if key not in template:
            raise ValueError(f"Unknown {prefix} key: {key}")
        reference = template[key]
        if isinstance(reference, dict):
            _validate_synth(value, reference, f"{prefix}.{key}")
        elif isinstance(reference, (int, float)):
            _number(f"{prefix}.{key}", value, integer=isinstance(reference, int))
        elif isinstance(reference, list):
            if not isinstance(value, list) or not value or not all(isinstance(v, str) for v in value):
                raise ValueError(f"{prefix}.{key} must be a nonempty list of sound names")


def _validate(profile: Profile, synth_template: dict):
    audio, onset, classifier = profile.audio, profile.onset, profile.classifier
    for name in ("sample_rate", "block", "queue_blocks", "meter_width"):
        _number(f"audio.{name}", getattr(audio, name), minimum=1, integer=True)
    if audio.sample_rate != 16000 or audio.block != 256:
        raise ValueError("Audio contract requires sample_rate=16000 and block=256")
    for name in ("level_hz", "console_level_s"):
        _number(f"audio.{name}", getattr(audio, name), minimum=0)
        if getattr(audio, name) == 0:
            raise ValueError(f"audio.{name} must be positive")
    _number("audio.record_countdown_s", audio.record_countdown_s, minimum=0, integer=True)
    for name in ("record_clip_peak", "record_quiet_peak"):
        _number(f"audio.{name}", getattr(audio, name), minimum=0, maximum=1)
    if audio.record_quiet_peak >= audio.record_clip_peak:
        raise ValueError("audio.record_quiet_peak must be below record_clip_peak")
    for name in ("meter_min_dbfs", "meter_max_dbfs"):
        _number(f"audio.{name}", getattr(audio, name))
    if audio.meter_min_dbfs >= audio.meter_max_dbfs:
        raise ValueError("audio.meter_min_dbfs must be below meter_max_dbfs")
    for field in fields(onset):
        _number(f"onset.{field.name}", getattr(onset, field.name))
    if not 0 < onset.highpass_hz < audio.sample_rate / 2:
        raise ValueError("onset.highpass_hz must lie between zero and the Nyquist frequency")
    for name in ("threshold_db", "refractory_s", "floor_exclude_s"):
        _number(f"onset.{name}", getattr(onset, name), minimum=0)
    if not 0 <= onset.rearm_db < onset.threshold_db:
        raise ValueError("onset.rearm_db must be nonnegative and below threshold_db")
    if onset.floor_window_s <= 0:
        raise ValueError("onset.floor_window_s must be positive")
    _number("onset.floor_percentile", onset.floor_percentile, minimum=0, maximum=100)
    if not isinstance(classifier.name, str) or not classifier.name.strip():
        raise ValueError("classifier.name must be a nonempty string")
    _number("classifier.threshold", classifier.threshold, minimum=0, maximum=1)
    _validate_synth(audio.synth, synth_template)


def load_profile(name_or_path: str | Path = "rubble") -> Profile:
    """Load a named or explicit YAML profile, inheriting omitted rubble settings.

    The dialog, prompter and server mappings intentionally remain unrestricted
    until their owning prompts define them. All currently defined keys are strict.
    """
    profile_dir = _profile_dir()
    default_path = profile_dir / "rubble.yaml"
    default = _read(default_path)
    path = Path(name_or_path).expanduser()
    if not path.is_file() and path.parent == Path("."):
        path = profile_dir / (path.name if path.suffix else f"{path.name}.yaml")
    data = default if path.resolve() == default_path.resolve() else _merge(default, _read(path))
    profile = Profile(
        audio=_section(AudioConfig, "audio", data["audio"]),
        onset=_section(OnsetConfig, "onset", data["onset"]),
        classifier=_section(ClassifierConfig, "classifier", data["classifier"]),
        dialog=dict(data.get("dialog", {})),
        prompter=dict(data.get("prompter", {})),
        server=dict(data.get("server", {})),
    )
    _validate(profile, default["audio"]["synth"])
    return profile
