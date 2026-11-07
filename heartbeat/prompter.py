"""Blocking prompt backends and a hardware-free virtual survivor.

Hardware deadlines use monotonic time only to bound I/O. All published event
timestamps come from the injected audio stream clock.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, fields
import importlib
import logging
import math
from pathlib import Path
import re
import sys
import time
from typing import Any, Protocol, TextIO

import numpy as np
import soundfile as sf

from .audio import SAMPLE_RATE
from .dialog import Action, PromptKnocks, PromptVoice
from .events import EventBus


VOICE_TEXT = {
    "copy_knocks": "Rescue team. If you can hear us, knock back the same number of times.",
    "we_hear_you": "We hear you. Help is coming. Save your strength.",
    "q_people": "Knock once for each person with you.",
    "q_move": "Can you move? Knock two times for yes. One time for no.",
    "q_hurt": "Are you hurt? Knock two times for yes. One time for no.",
    "reassure": "We are still here. Knock if anything changes.",
}
BAUD_RATE = 115200  # Fixed knocker v1 wire protocol.
MAX_REPLY_BYTES = 256  # Bound corrupt serial lines, as in the P02 test tool.
_ROOT = Path(__file__).resolve().parent.parent
log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PrompterConfig:
    voice: str
    gate_tail_s: float
    voice_dir: str
    manual_knock_tail_s: float
    manual_voice_words_per_s: float
    manual_voice_min_s: float
    serial_ready_timeout_s: float
    serial_reply_margin_s: float
    serial_ping_timeout_s: float
    serial_read_poll_s: float
    serial_write_timeout_s: float
    fake_reply_delay_min_s: float
    fake_reply_delay_max_s: float
    fake_jitter_s: float
    fake_knock_interval_s: float
    fake_knock_gain_db: float
    sim_duration_s: float
    sim_no_survivor_duration_s: float
    sim_background_dbfs: float
    sim_chunk_s: float
    sim_initial_knock_times_s: tuple[float, ...]
    sim_initial_knock_gain_db: float
    sim_noise_first_s: float
    sim_noise_interval_s: float
    sim_noise_gain_db: float
    sim_noise_kinds: tuple[str, ...]

    @classmethod
    def from_dict(cls, value: PrompterConfig | Mapping[str, Any] | None = None) -> PrompterConfig:
        if isinstance(value, cls):
            return value
        if value is not None and not isinstance(value, Mapping):
            raise ValueError("prompter configuration must be a mapping")
        from .config import load_profile

        data = dict(load_profile("rubble").prompter)
        data.update(value or {})
        expected = {item.name for item in fields(cls)}
        unknown, missing = data.keys() - expected, expected - data.keys()
        if unknown:
            raise ValueError(f"Unknown prompter keys: {', '.join(sorted(unknown))}")
        if missing:
            raise ValueError(f"Missing prompter keys: {', '.join(sorted(missing))}")
        for name, item in data.items():
            if name in {"voice", "voice_dir", "sim_initial_knock_times_s", "sim_noise_kinds"}:
                continue
            if isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(item):
                raise ValueError(f"prompter.{name} must be a finite number")
            if not name.endswith(("_db", "_dbfs")) and item < 0:
                raise ValueError(f"prompter.{name} must be nonnegative")
        for name in (
            "manual_voice_words_per_s", "serial_ready_timeout_s", "serial_reply_margin_s",
            "serial_ping_timeout_s", "serial_read_poll_s", "serial_write_timeout_s",
            "fake_knock_interval_s", "sim_duration_s", "sim_no_survivor_duration_s",
            "sim_chunk_s", "sim_noise_interval_s",
        ):
            if data[name] <= 0:
                raise ValueError(f"prompter.{name} must be positive")
        if data["fake_reply_delay_min_s"] > data["fake_reply_delay_max_s"]:
            raise ValueError("prompter.fake_reply_delay_min_s must not exceed the maximum")
        if data["fake_jitter_s"] * 2 >= data["fake_knock_interval_s"]:
            raise ValueError("prompter.fake_jitter_s must preserve knock order")
        if data["fake_reply_delay_min_s"] - data["fake_jitter_s"] <= data["gate_tail_s"]:
            raise ValueError("Simulated replies must start after prompter.gate_tail_s")
        if round(data["sim_chunk_s"] * SAMPLE_RATE) < 1:
            raise ValueError("prompter.sim_chunk_s must contain at least one audio sample")
        if not isinstance(data["voice"], str) or data["voice"] not in {"auto", "none"}:
            raise ValueError("prompter.voice must be auto or none")
        if not isinstance(data["voice_dir"], str) or not data["voice_dir"].strip():
            raise ValueError("prompter.voice_dir must be a nonempty path")
        initial = data["sim_initial_knock_times_s"]
        if not isinstance(initial, (list, tuple)) or any(
            isinstance(t, bool) or not isinstance(t, (int, float)) or not math.isfinite(t) or t < 0
            for t in initial
        ):
            raise ValueError("prompter.sim_initial_knock_times_s must contain nonnegative times")
        kinds = data["sim_noise_kinds"]
        allowed = {"footstep", "speech_like", "drop", "clap", "knock"}
        if not isinstance(kinds, (list, tuple)) or not kinds or any(
            not isinstance(kind, str) or kind not in allowed for kind in kinds
        ):
            raise ValueError("prompter.sim_noise_kinds must contain known synthetic sounds")
        data["sim_initial_knock_times_s"] = tuple(initial)
        data["sim_noise_kinds"] = tuple(kinds)
        return cls(**data)


class Prompter(Protocol):
    def run(self, action: Action) -> None: ...

    def close(self) -> None: ...


class _BasePrompter:
    def __init__(self, bus=None, *, cfg=None, clock=None):
        self.bus: EventBus | None = bus
        self.cfg = PrompterConfig.from_dict(cfg)
        self.clock: Callable[[], float] = clock if clock is not None else lambda: 0.0
        self._voice_disabled = False
        self._voice_warning_sent = False

    def _emit(self, type: str, **data) -> None:
        if self.bus is not None:
            self.bus.publish(type, float(self.clock()), **data)
        elif type == "error":
            log.warning("%s", data.get("message", data))

    def _error(self, message: str, *, code: str, severity: str = "error", **data) -> None:
        self._emit("error", source="prompter", code=code, severity=severity, message=message, **data)

    def disable_voice(self, reason: str) -> None:
        self._voice_disabled = True
        if not self._voice_warning_sent:
            self._voice_warning_sent = True
            self._error(f"Voice prompts skipped ({reason}); continuing with knock-only prompts.",
                        code="voice_disabled", severity="warning")

    def _skip_voice(self, action: Action) -> bool:
        if not isinstance(action, PromptVoice):
            return False
        if self.cfg.voice == "none":
            self.disable_voice("prompter.voice=none")
        return self._voice_disabled

    def _voice_path(self, key: str) -> Path:
        if key not in VOICE_TEXT and key != "knock":
            raise ValueError(f"Unknown voice prompt: {key}")
        directory = Path(self.cfg.voice_dir)
        return (directory if directory.is_absolute() else _ROOT / directory) / f"{key}.wav"

    def duration(self, action: Action) -> float:
        if isinstance(action, PromptKnocks):
            if action.n < 1 or not math.isfinite(action.interval_s) or action.interval_s <= 0:
                raise ValueError("Knock prompts need a positive count and interval")
            return action.n * action.interval_s + self.cfg.manual_knock_tail_s
        if isinstance(action, PromptVoice):
            path = self._voice_path(action.key)
            try:
                info = sf.info(path)
                if info.frames > 0 and info.samplerate == SAMPLE_RATE and info.channels == 1:
                    return float(info.duration)
            except (OSError, RuntimeError):
                pass
            return max(
                self.cfg.manual_voice_min_s,
                len(VOICE_TEXT[action.key].split()) / self.cfg.manual_voice_words_per_s,
            )
        raise ValueError(f"Unknown prompt action: {action!r}")

    def close(self) -> None:
        pass


class ManualPrompter(_BasePrompter):
    def __init__(self, bus=None, *, cfg=None, clock=None, sleep=None, stream: TextIO | None = None):
        super().__init__(bus, cfg=cfg, clock=clock)
        self.sleep = sleep if sleep is not None else time.sleep
        self.stream = stream if stream is not None else sys.stdout

    def run(self, action: Action) -> None:
        if self._skip_voice(action):
            return
        try:
            duration = self.duration(action)
            if isinstance(action, PromptKnocks):
                message = f"knock {action.n} times now"
                data = {"action": "knocks", "n": action.n, "interval_s": action.interval_s}
            else:
                message = VOICE_TEXT[action.key]
                data = {"action": "voice", "key": action.key}
            print(f"OPERATOR: {message}", file=self.stream, flush=True)
            self._emit("operator", message=message, **data)
            self.sleep(duration)
        except Exception as exc:
            self._error(f"Manual prompt failed: {exc}", code="manual_prompt_failed")


class SpeakerPrompter(_BasePrompter):
    def __init__(self, device=None, *, bus=None, cfg=None, clock=None, audio=None, sleep=None,
                 monotonic=None, manual=None, skip_failed_voice=False):
        super().__init__(bus, cfg=cfg, clock=clock)
        self.device = device
        self._audio = audio
        self.sleep = sleep if sleep is not None else time.sleep
        self.monotonic = monotonic if monotonic is not None else time.monotonic
        self.manual = manual if manual is not None else ManualPrompter(bus, cfg=self.cfg, clock=clock, sleep=sleep)
        self.skip_failed_voice = skip_failed_voice
        self.unavailable_reason = "no usable audio output"

    def _backend(self):
        if self._audio is None:
            self._audio = importlib.import_module("sounddevice")
        return self._audio

    def available(self, *, report=True) -> bool:
        try:
            self._backend().check_output_settings(device=self.device, channels=1, samplerate=SAMPLE_RATE)
            return True
        except Exception as exc:
            self.unavailable_reason = str(exc)
            if report:
                self._error(f"Speaker unavailable: {exc}", code="speaker_unavailable", severity="warning")
            return False

    def _play(self, key: str) -> None:
        samples, rate = sf.read(self._voice_path(key), dtype="float32")
        if rate != SAMPLE_RATE or samples.ndim != 1 or not len(samples):
            raise ValueError(f"Voice asset {key} must be nonempty 16000 Hz mono")
        self._backend().play(samples, samplerate=rate, device=self.device, blocking=True)

    def run(self, action: Action) -> None:
        if self._skip_voice(action):
            return
        try:
            self.duration(action)  # Validate actions before touching the output device.
            if isinstance(action, PromptVoice):
                self._play(action.key)
            else:
                for index in range(action.n):
                    started = self.monotonic()
                    self._play("knock")
                    if index + 1 < action.n:
                        remaining = action.interval_s - (self.monotonic() - started)
                        if remaining > 0:
                            self.sleep(remaining)
        except Exception as exc:
            if isinstance(action, PromptVoice) and self.skip_failed_voice:
                self.disable_voice(f"speaker playback unavailable: {exc}")
                return
            self._error(f"Speaker prompt failed: {exc}; using operator prompt", code="speaker_prompt_failed")
            self.manual.run(action)

    def close(self) -> None:
        if self._audio is not None:
            try:
                self._audio.stop()
            except Exception as exc:
                self._error(f"Speaker shutdown failed: {exc}", code="speaker_close_failed")


class SerialKnocker(_BasePrompter):
    def __init__(self, port: str, *, bus=None, cfg=None, clock=None, serial_factory=None,
                 monotonic=None, sleep=None, manual=None, exclude_ports=None):
        super().__init__(bus, cfg=cfg, clock=clock)
        self.port = port
        self.serial_factory = serial_factory
        self.monotonic = monotonic if monotonic is not None else time.monotonic
        self.manual = manual if manual is not None else ManualPrompter(bus, cfg=self.cfg, clock=clock, sleep=sleep)
        self._connection = None
        self.exclude_ports = set(exclude_ports or ())
        self._claim_key = None

    def _read_line(self, deadline: float) -> str | None:
        pending = bytearray()
        while True:
            remaining = deadline - self.monotonic()
            if remaining <= 0:
                break
            if hasattr(self._connection, "timeout"):
                self._connection.timeout = min(self.cfg.serial_read_poll_s, remaining)
            byte = self._connection.read(1)
            if not byte:
                continue
            if byte in (b"\r", b"\n"):
                if pending:
                    return pending.decode("ascii", errors="replace")
                continue
            pending.extend(byte)
            if len(pending) > MAX_REPLY_BYTES:
                raise OSError("Knocker reply exceeded the line limit")
        if pending:
            raise TimeoutError("Incomplete knocker reply")
        return None

    def _connect(self) -> None:
        if self._connection is not None and getattr(self._connection, "is_open", True):
            return
        from .stickmic import canonical_port, claim_port, new_serial_connection

        self.close()
        claim_key = canonical_port(self.port)
        if claim_key in {canonical_port(port) for port in self.exclude_ports}:
            raise OSError(f"Serial port is excluded: {self.port}")
        if not claim_port(claim_key):
            raise OSError(f"Serial port is already in use: {self.port}")
        self._claim_key = claim_key
        try:
            if self.serial_factory is None:
                # Leave ESP32 reset lines inactive before opening a scan candidate.
                self._connection = new_serial_connection(
                    self.port, BAUD_RATE, self.cfg.serial_read_poll_s,
                    write_timeout=self.cfg.serial_write_timeout_s,
                )
            else:
                # Preserve the existing injected-factory contract for hardware-free callers.
                self._connection = self.serial_factory(
                    self.port, baudrate=BAUD_RATE, timeout=self.cfg.serial_read_poll_s,
                    write_timeout=self.cfg.serial_write_timeout_s,
                )
        except Exception:
            self.close()
            raise
        deadline = self.monotonic() + self.cfg.serial_ready_timeout_s
        while self.monotonic() < deadline:
            line = self._read_line(deadline)
            if line is None:
                break  # Some adapters attach to a board that has already booted.
            if line.startswith("READY knocker v1"):
                return
            if line.startswith("ERR "):
                raise OSError(f"Knocker startup: {line}")

    def _send(self, command: str, expected: str, timeout_s: float) -> None:
        payload = (command + "\n").encode("ascii")
        written = self._connection.write(payload)
        if written != len(payload):
            raise OSError("Incomplete knocker command write")
        deadline = self.monotonic() + timeout_s
        while self.monotonic() < deadline:
            line = self._read_line(deadline)
            if line is None:
                break
            if line == expected:
                return
            if line.startswith(("ERR ", "OK ", "PONG", "READY")):
                raise OSError(f"Unexpected knocker reply: {line}")
        raise TimeoutError(f"No {expected!r} reply to {command!r}")

    def probe(self) -> bool:
        """Identify a board using PING only; never strike during discovery."""
        try:
            self._connect()
            self._send("PING", "PONG knocker v1", self.cfg.serial_ping_timeout_s)
            return True
        except Exception as exc:
            self._error(f"Knocker probe failed on {self.port}: {exc}", code="knocker_probe_failed", severity="warning")
            self.close()
            return False

    def run(self, action: Action) -> None:
        if not isinstance(action, PromptKnocks):
            self.manual.run(action)
            return
        try:
            interval_ms = round(action.interval_s * 1000)
            # Fixed firmware v1 limits, not user-facing tuning knobs.
            if isinstance(action.n, bool) or not isinstance(action.n, int) or not 1 <= action.n <= 9:
                raise ValueError("Knocker count must be an integer from 1 to 9")
            if not 250 <= interval_ms <= 2000:
                raise ValueError("Knocker interval must be 250 to 2000 ms")
            self._connect()
            self._send(
                f"K {action.n} {interval_ms}", f"OK K {action.n}",
                action.n * action.interval_s + self.cfg.serial_reply_margin_s,
            )
        except Exception as exc:
            self._error(f"Knocker failed on {self.port}: {exc}; using operator prompt", code="knocker_prompt_failed")
            self.close()
            self.manual.run(action)

    def close(self) -> None:
        connection, self._connection = self._connection, None
        if connection is not None:
            try:
                connection.close()
            except Exception as exc:
                self._error(f"Knocker shutdown failed: {exc}", code="knocker_close_failed")
        if self._claim_key is not None:
            from .stickmic import release_port

            release_port(self._claim_key)
            self._claim_key = None


class CompositePrompter:
    def __init__(self, voice: Prompter, knocks: Prompter):
        self.voice, self.knocks = voice, knocks

    def run(self, action: Action) -> None:
        (self.voice if isinstance(action, PromptVoice) else self.knocks).run(action)

    def close(self) -> None:
        self.voice.close()
        if self.knocks is not self.voice:
            self.knocks.close()


class FakePrompter(_BasePrompter):
    """Plan prompt completion and scripted answers entirely in stream time."""

    virtual = True

    def __init__(self, bus=None, *, cfg=None, clock=None, people=2, can_move="yes", hurt="yes",
                 no_survivor=False, seed=1):
        super().__init__(bus, cfg=cfg, clock=clock)
        self.people = int(people)
        if self.people < 0:
            raise ValueError("Simulated people count must be nonnegative")
        if can_move not in {"yes", "no", "unclear"} or hurt not in {"yes", "no", "unclear"}:
            raise ValueError("Simulated yes/no answers must be yes, no, or unclear")
        self.can_move, self.hurt = can_move, hurt
        self.no_survivor = bool(no_survivor)
        self.rng = np.random.default_rng(seed)
        self.actions: list[Action] = []

    def run(self, action: Action) -> None:
        self.duration(action)
        self.actions.append(action)

    def respond(self, batch: Sequence[Action], t_end: float) -> list[tuple[float, str, float]]:
        if self.no_survivor:
            return []
        count = 0
        for action in batch:
            if isinstance(action, PromptKnocks):
                count = action.n
            elif isinstance(action, PromptVoice):
                if action.key == "q_people":
                    count = self.people
                elif action.key in {"q_move", "q_hurt"}:
                    answer = self.can_move if action.key == "q_move" else self.hurt
                    count = {"yes": 2, "no": 1, "unclear": 3}[answer]
        delay = float(self.rng.uniform(self.cfg.fake_reply_delay_min_s, self.cfg.fake_reply_delay_max_s))
        result = []
        for index in range(count):
            jitter = float(self.rng.uniform(-self.cfg.fake_jitter_s, self.cfg.fake_jitter_s))
            t = t_end + delay + index * self.cfg.fake_knock_interval_s + jitter
            result.append((t, "knock", self.cfg.fake_knock_gain_db))
        return result

    def schedule_batch(self, actions: Sequence[Action], t_start: float):
        for action in actions:
            self.run(action)
        t_end = float(t_start) + sum(self.duration(action) for action in actions)
        return t_end, self.respond(actions, t_end)


def make_prompter(spec: str, bus=None, *, cfg=None, clock=None, speaker_device=None,
                  serial_factory=None, port_lister=None, audio=None, sleep=None, monotonic=None,
                  exclude_ports=None) -> Prompter:
    cfg = PrompterConfig.from_dict(cfg)
    manual = ManualPrompter(bus, cfg=cfg, clock=clock, sleep=sleep)
    if spec == "manual":
        if cfg.voice == "none":
            manual.disable_voice("prompter.voice=none")
        return manual
    speaker = SpeakerPrompter(
        speaker_device, bus=bus, cfg=cfg, clock=clock, audio=audio, sleep=sleep,
        monotonic=monotonic, manual=manual, skip_failed_voice=True,
    )

    def voice_backend():
        if cfg.voice == "none":
            speaker.disable_voice("prompter.voice=none")
        elif not speaker.available(report=False):
            speaker.disable_voice(speaker.unavailable_reason)
        return speaker

    def speaker_or_manual():
        if speaker.available(report=False):
            if cfg.voice == "none":
                speaker.disable_voice("prompter.voice=none")
            return speaker
        manual.disable_voice(speaker.unavailable_reason)
        return manual

    def knocker(port):
        return SerialKnocker(
            port, bus=bus, cfg=cfg, clock=clock, serial_factory=serial_factory,
            monotonic=monotonic, sleep=sleep, manual=manual, exclude_ports=exclude_ports,
        )

    if spec == "speaker":
        return speaker_or_manual()
    if spec.startswith("/dev/") or re.fullmatch(r"COM[0-9]+", spec, re.IGNORECASE):
        return CompositePrompter(voice_backend(), knocker(spec))
    if spec != "auto":
        raise ValueError(f"Unknown prompter: {spec}")
    try:
        from .stickmic import canonical_port, port_in_use

        excluded = {canonical_port(port) for port in (exclude_ports or ())}
        ports = port_lister() if port_lister is not None else importlib.import_module("serial.tools.list_ports").comports()
        for port in sorted(ports, key=lambda item: str(getattr(item, "device", item))):
            device = str(getattr(port, "device", port))
            if (getattr(port, "is_open", False)
                    or canonical_port(device) in excluded or port_in_use(device)):
                continue
            candidate = knocker(device)
            if candidate.probe():
                return CompositePrompter(voice_backend(), candidate)
    except Exception as exc:
        manual._error(f"Serial discovery failed: {exc}", code="serial_scan_failed", severity="warning")
    return speaker_or_manual()
