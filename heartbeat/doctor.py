"""Explicit hardware diagnostics and a hardware-free, CPU pipeline benchmark.

The ``probe_*`` functions are the boundary for host I/O. Importing this module
does not open devices; tests replace these probes or the existing source and
serial primitives. Only ``run_doctor`` is intended to touch real hardware.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import importlib
import math
from pathlib import Path
import queue
import re
import resource
import shutil
import socket
import subprocess
import sys
import threading
import time

import numpy as np
from serial.tools import list_ports
import soundfile as sf
import yaml

from .audio import SAMPLE_RATE, list_devices
from .classify import make_classifier
from .config import _profile_dir
from .sources import open_input
from .stickmic import (
    READY_TIMEOUT_S, _identify_stick, canonical_port, claim_port,
    new_serial_connection, release_port,
)


_ROOT = Path(__file__).resolve().parent.parent
_IDENTITIES = {
    "PONG stickmic v1": "stick",
    "PONG knocker v1": "knocker",
    "PONG rover v1": "rover",
}
# Knocker/rover protocol baud; the stick uses the selected audio profile.
_CONTROL_BAUD = 115200


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str
    hint: str
    skipped: bool = False
    warning: bool = False


def _validate_settings(settings: dict) -> dict:
    if (not isinstance(settings, dict) or not {"doctor", "bench"}.issubset(settings)
            or set(settings) - {"doctor", "bench", "deploy"}):
        raise ValueError("Diagnostics settings must contain doctor and bench mappings")
    positive = {
        "doctor": ("audio_seconds", "audio_stall_timeout_s", "audio_timeout_s",
                   "audio_close_timeout_s", "min_free_disk_bytes", "serial_timeout_s",
                   "serial_read_poll_s", "serial_write_timeout_s", "max_serial_line_bytes",
                   "command_timeout_s", "max_temp_c"),
        "bench": ("seconds",),
    }
    if "deploy" in settings:
        positive["deploy"] = ("restart_s",)
    for section, names in positive.items():
        values = settings[section]
        if not isinstance(values, dict):
            raise ValueError(f"Diagnostics {section} must be a mapping")
        for name in names:
            value = values.get(name)
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or value <= 0):
                raise ValueError(f"Diagnostics {section}.{name} must be a positive finite number")
    doctor, bench = settings["doctor"], settings["bench"]
    rms = doctor.get("min_audio_rms")
    if isinstance(rms, bool) or not isinstance(rms, (int, float)) or not 0 <= rms <= 1:
        raise ValueError("Diagnostics doctor.min_audio_rms must be in [0, 1]")
    if int(doctor["max_serial_line_bytes"]) != doctor["max_serial_line_bytes"]:
        raise ValueError("Diagnostics doctor.max_serial_line_bytes must be an integer")
    if round(doctor["audio_seconds"] * SAMPLE_RATE) < 1:
        raise ValueError("Diagnostics doctor.audio_seconds must contain at least one sample")
    if round(bench["seconds"] * SAMPLE_RATE) < 1:
        raise ValueError("Diagnostics bench.seconds must contain at least one sample")
    for name in ("seed", "people"):
        value = bench.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"Diagnostics bench.{name} must be a nonnegative integer")
    if not isinstance(bench.get("noise"), bool):
        raise ValueError("Diagnostics bench.noise must be a boolean")
    for name in ("can_move", "hurt"):
        if bench.get(name) not in {"yes", "no", "unclear"}:
            raise ValueError(f"Diagnostics bench.{name} must be yes, no, or unclear")
    return settings


def load_settings(path=None) -> dict:
    """Read standalone diagnostics tuning; never add it to the Profile contract."""
    target = Path(path) if path is not None else _profile_dir() / "diagnostics.yaml"
    with target.open(encoding="utf-8") as stream:
        return _validate_settings(yaml.safe_load(stream))


def _settings(settings) -> dict:
    return load_settings() if settings is None else _validate_settings(settings)


def probe_audio_devices() -> list[dict]:
    return list_devices()


def _probe_identity(port: str, baud: int, settings: dict, *, stick=False) -> str | None:
    """Identify a claimed port without starting capture or moving hardware."""
    cfg = settings["doctor"]
    claim = canonical_port(port)
    if not claim_port(claim):
        return None
    connection = None
    try:
        connection = new_serial_connection(
            port, baud, cfg["serial_read_poll_s"],
            write_timeout=cfg["serial_write_timeout_s"],
        )
        if stick:
            # A fresh SE port open can reset the board despite low modem lines.
            # Reuse boot-line recovery and allow the runtime startup deadline.
            _identify_stick(connection, time.monotonic() + READY_TIMEOUT_S, time.monotonic)
            return "PONG stickmic v1"
        if connection.write(b"PING\n") != len(b"PING\n"):
            raise OSError("Incomplete PING write")
        deadline = time.monotonic() + cfg["serial_timeout_s"]
        pending = bytearray()
        while time.monotonic() < deadline:
            byte = connection.read(1)
            if not byte:
                if not getattr(connection, "is_open", True):
                    raise OSError("Serial device disconnected")
                continue
            if byte in (b"\r", b"\n"):
                line = pending.decode("ascii", errors="replace")
                pending.clear()
                if line in _IDENTITIES:
                    return line
            else:
                pending.extend(byte)
                if len(pending) > cfg["max_serial_line_bytes"]:
                    # Ignore binary/overlong lines completely; never accept a
                    # suffix that merely resembles a protocol identity.
                    while byte and byte not in (b"\r", b"\n") and time.monotonic() < deadline:
                        byte = connection.read(1)
                    pending.clear()
        return None
    finally:
        try:
            if connection is not None:
                connection.close()
        finally:
            release_port(claim)


def probe_serial_ports(profile, settings: dict) -> dict:
    result = {"stick": [], "knocker": [], "rover": [], "ports": [], "errors": []}
    seen = set()
    for entry in sorted(list_ports.comports(), key=lambda item: str(getattr(item, "device", item))):
        port = str(getattr(entry, "device", entry))
        key = canonical_port(port)
        if key in seen:
            continue
        seen.add(key)
        result["ports"].append(port)
        if getattr(entry, "is_open", False):
            result["errors"].append(f"{port}: already open; skipped")
            continue
        for baud, stick in ((profile.audio.stick_baud, True), (_CONTROL_BAUD, False)):
            try:
                identity = _probe_identity(port, baud, settings, stick=stick)
                kind = _IDENTITIES.get(identity)
                if kind is not None and ((kind == "stick") == stick):
                    result[kind].append(port)
                    break
            except Exception as exc:
                result["errors"].append(f"{port} at {baud}: {exc}")
    return result


def probe_audio(profile, device, settings: dict) -> dict:
    """Read the selected runtime source, with stall and overall deadlines.

    The worker lets MicSource's otherwise-live queue be interrupted on a silent
    or disconnected host. ``close`` is bounded too, so a broken backend cannot
    hold the rest of the diagnostic checklist hostage.
    """
    from .events import EventBus

    cfg = settings["doctor"]
    result = {"samples": 0, "duration_s": 0.0, "rms": 0.0, "peak": 0.0, "errors": []}
    bus = EventBus()
    bus.subscribe(lambda event: result["errors"].append(str(event.data.get("message", event.data)))
                  if event.type == "error" and event.data.get("severity") != "warning" else None)
    if device == "stick":
        ports = probe_serial_ports(profile, settings)["stick"]
        if not ports:
            raise OSError("No port answered PONG stickmic v1")
        device = "stick:" + ports[0]
    source = open_input(device, profile, bus)
    packets = queue.Queue(maxsize=1)
    stopping = threading.Event()
    target = round(cfg["audio_seconds"] * SAMPLE_RATE)

    def send(packet):
        while not stopping.is_set():
            try:
                packets.put(packet, timeout=cfg["audio_close_timeout_s"])
                return
            except queue.Full:
                pass

    def read():
        try:
            for block in source:
                if stopping.is_set():
                    break
                send(("block", block))
            send(("end", None))
        except Exception as exc:
            send(("error", str(exc)))

    worker = threading.Thread(target=read, name="heartbeat-doctor-audio", daemon=True)
    started = time.monotonic()
    worker.start()
    sum_squares = 0.0
    try:
        while result["samples"] < target:
            remaining = cfg["audio_timeout_s"] - (time.monotonic() - started)
            if remaining <= 0:
                raise TimeoutError("Audio capture exceeded its overall timeout")
            try:
                kind, value = packets.get(timeout=min(cfg["audio_stall_timeout_s"], remaining))
            except queue.Empty as exc:
                raise TimeoutError("Audio source stalled before delivering the requested samples") from exc
            if kind == "error":
                raise OSError(value)
            if kind == "end":
                raise OSError("Audio source ended before delivering the requested samples")
            samples = np.asarray(value.x)
            if (samples.dtype != np.float32 or samples.ndim != 1 or not len(samples)
                    or not np.all(np.isfinite(samples)) or np.any(np.abs(samples) > 1)):
                raise ValueError("Audio source must deliver finite mono float32 samples in [-1, 1]")
            if not math.isfinite(value.t0) or round(value.t0 * SAMPLE_RATE) != result["samples"]:
                raise ValueError("Audio timestamps must follow the delivered sample count at 16000 Hz")
            samples = samples[:target - result["samples"]]
            sum_squares += float(np.dot(samples.astype(np.float64), samples.astype(np.float64)))
            result["peak"] = max(result["peak"], float(np.max(np.abs(samples))))
            result["samples"] += len(samples)
        result["rms"] = math.sqrt(sum_squares / result["samples"])
        if result["rms"] <= cfg["min_audio_rms"]:
            result["errors"].append("Selected microphone delivered silent or near-silent audio")
    except Exception as exc:
        result["errors"].append(str(exc))
    finally:
        stopping.set()

        def close():
            try:
                source.close()
            except Exception as exc:
                result["errors"].append(f"Audio shutdown failed: {exc}")

        closer = threading.Thread(target=close, name="heartbeat-doctor-close", daemon=True)
        closer.start()
        closer.join(cfg["audio_close_timeout_s"])
        worker.join(cfg["audio_close_timeout_s"])
        if closer.is_alive() or worker.is_alive():
            result["errors"].append("Audio backend did not stop within the close timeout")
    result["duration_s"] = result["samples"] / SAMPLE_RATE
    return result


def _sounddevice():
    return importlib.import_module("sounddevice")


def probe_speaker(speaker_device, play: bool, profile, settings: dict) -> dict:
    outputs = [item for item in probe_audio_devices() if item.get("max_output_channels", 0) > 0]
    result = {"available": False, "devices": outputs, "played": False, "detail": "No speaker output found"}
    if not outputs and speaker_device is None:
        return result
    backend = _sounddevice()
    backend.check_output_settings(device=speaker_device, channels=1, samplerate=SAMPLE_RATE)
    result.update(available=True, detail="; ".join(_device_label(item) for item in outputs))
    if play:
        directory = Path(profile.prompter["voice_dir"])
        path = (directory if directory.is_absolute() else _ROOT / directory) / "reassure.wav"
        samples, rate = sf.read(path, dtype="float32")
        if rate != SAMPLE_RATE or samples.ndim != 1 or not len(samples) or not np.all(np.isfinite(samples)):
            raise ValueError("reassure.wav must contain finite 16000 Hz mono audio")
        try:
            backend.play(samples, samplerate=rate, device=speaker_device, blocking=True)
        finally:
            backend.stop()
        result.update(played=True, detail=result["detail"] + "; played reassure.wav (confirm audibility by ear)")
    return result


def probe_classifier(profile) -> dict:
    """Exercise inference too: runtime fallback can differ from startup choice."""
    from .onset import CLIP_SAMPLES, PRE_SAMPLES, Onset
    from .synth import knock

    classifier = make_classifier(profile)
    sound = knock(cfg=profile.audio.synth)
    clip = np.zeros(CLIP_SAMPLES, dtype=np.float32)
    count = min(len(sound), CLIP_SAMPLES - PRE_SAMPLES)
    clip[PRE_SAMPLES:PRE_SAMPLES + count] = sound[:count]
    detection = classifier.classify(Onset(
        t=PRE_SAMPLES / SAMPLE_RATE, peak=float(np.max(np.abs(clip))),
        snr_db=profile.onset.threshold_db, gated=False, clip=clip,
    ))
    return {"requested": profile.classifier.name, "active": str(detection.classifier),
            "detail": f"requested {profile.classifier.name}; active {detection.classifier} (CPU inference passed)"}


def probe_clap(profile, *, request=None, clock=None) -> Check:
    """Strict health/identity check and five synthetic classifications; no fallback."""
    from .classifiers.clap import ClapClassifier
    from .onset import CLIP_SAMPLES, PRE_SAMPLES, Onset
    from .synth import knock

    clock = clock or time.perf_counter
    cfg = replace(profile.classifier, clap={**profile.classifier.clap, "on_failure": "stop"})
    try:
        classifier = ClapClassifier(cfg, request=request)
        sound = knock(cfg=profile.audio.synth)
        clip = np.zeros(CLIP_SAMPLES, np.float32)
        count = min(len(sound), CLIP_SAMPLES - PRE_SAMPLES)
        clip[PRE_SAMPLES:PRE_SAMPLES + count] = sound[:count]
        onset = Onset(t=PRE_SAMPLES / SAMPLE_RATE, peak=float(np.max(np.abs(clip))),
                      snr_db=profile.onset.threshold_db, gated=False, clip=clip)
        durations = []
        for _ in range(5):
            started = clock()
            classifier.classify(onset)
            durations.append((clock() - started) * 1000)
        median, maximum = float(np.median(durations)), max(durations)
        ok = maximum < classifier.settings["timeout_s"] * 500
        detail = f"identity matched; 5 classifications: median {median:.1f} ms, max {maximum:.1f} ms"
        hint = "" if ok else "CLAP too slow for live use here: raise timeout_s or run the service on a faster machine."
        return Check("CLAP service", ok, detail, hint, warning=not ok)
    except Exception as exc:
        return Check("CLAP service", False, str(exc), "Start the matching CLAP service and check its endpoint/profile.")


def probe_disk(path) -> int:
    path = Path(path).resolve()
    while not path.exists() and path != path.parent:
        path = path.parent
    return shutil.disk_usage(path).free


def probe_pi(settings: dict) -> dict:
    command = shutil.which("vcgencmd") if sys.platform.startswith("linux") else None
    if command is None:
        return {"available": False, "temp_c": None, "throttled": None,
                "detail": "vcgencmd unavailable; Pi-only checks skipped"}
    values = []
    for argument in ("measure_temp", "get_throttled"):
        values.append(subprocess.run(
            [command, argument], capture_output=True, text=True, check=True,
            timeout=settings["doctor"]["command_timeout_s"],
        ).stdout.strip())
    temperature = re.fullmatch(r"temp=([0-9.]+)'C", values[0])
    throttling = re.fullmatch(r"throttled=(0x[0-9a-fA-F]+)", values[1])
    if temperature is None or throttling is None:
        raise ValueError("Could not parse vcgencmd temperature/throttling response")
    return {"available": True, "temp_c": float(temperature[1]), "throttled": int(throttling[1], 16),
            "detail": "; ".join(values)}


def probe_network() -> dict:
    hostname = socket.gethostname().split(".", 1)[0]
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)}
    except socket.gaierror:
        addresses = set()
    # Resolving a hostname can return only loopback on a newly imaged Pi.
    # Linux SIOCGIFADDR inspects local interfaces without external traffic.
    if sys.platform.startswith("linux"):
        import fcntl
        import struct

        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as connection:
            for _, interface in socket.if_nameindex():
                try:
                    request = struct.pack("256s", interface.encode()[:15])
                    reply = fcntl.ioctl(connection.fileno(), 0x8915, request)
                    addresses.add(socket.inet_ntoa(reply[20:24]))
                except OSError:
                    continue
    return {"hostname": hostname,
            "addresses": sorted(address for address in addresses if not address.startswith("127."))}


def _device_label(item: dict) -> str:
    name = str(item.get("name", "unnamed"))
    marker = " [USB]" if "usb" in name.casefold() else ""
    return f"{item.get('index', '?')}: {name}{marker}"


def run_doctor(profile, *, device="stick", speaker_device=None, play=False, settings=None) -> list[Check]:
    settings = _settings(settings)
    cfg = settings["doctor"]
    checks = [Check("python", sys.version_info[:2] == (3, 11),
                    f"Python {sys.version.split()[0]}", "Use uv sync with the pinned Python 3.11 interpreter.")]
    stick_selected = device == "stick" or isinstance(device, str) and device.startswith("stick:")
    try:
        inputs = [item for item in probe_audio_devices() if item.get("max_input_channels", 0) > 0]
        checks.append(Check("audio_inputs", bool(inputs), "; ".join(_device_label(item) for item in inputs)
                            or "No standard audio inputs; the serial stick does not use PortAudio",
                            "Use --device stick, or select an input index from hb devices.",
                            skipped=not inputs and stick_selected))
    except Exception as exc:
        checks.append(Check("audio_inputs", False, str(exc), "Install PortAudio and reconnect the USB input.",
                            skipped=stick_selected))
    serials = {kind: [] for kind in ("stick", "knocker", "rover")}
    serial_error = ""
    try:
        serials.update(probe_serial_ports(profile, settings))
        serial_error = "; ".join(serials.get("errors", []))
    except Exception as exc:
        serial_error = str(exc)
    for kind in ("stick", "knocker", "rover"):
        ports = serials[kind]
        detail = ", ".join(ports) if ports else f"No exact PONG {('stickmic' if kind == 'stick' else kind)} v1 reply"
        if not ports and serial_error:
            detail += "; " + serial_error
        checks.append(Check(kind, bool(ports), detail,
                            "Connect matching firmware; stop other serial users; on Pi check dialout membership.",
                            skipped=not ports and (kind == "rover" or kind == "stick" and not stick_selected)))
    try:
        selected = "stick:" + serials["stick"][0] if device == "stick" and serials["stick"] else device
        if selected == "stick":
            raise OSError("No identified stick microphone is available for the stream test")
        audio = probe_audio(profile, selected, settings)
        errors = audio.get("errors", [])
        enough = audio["samples"] >= round(cfg["audio_seconds"] * SAMPLE_RATE)
        audible = audio["rms"] > cfg["min_audio_rms"]
        detail = f"{audio['duration_s']:.3f} s at 16000 Hz; RMS {audio['rms']:.6f}; peak {audio['peak']:.6f}"
        checks.append(Check("audio_stream", enough and audible and not errors,
                            detail + ("; " + "; ".join(errors) if errors else ""),
                            "Reconnect the selected input, check permissions, and tap the contact mic during the test."))
    except Exception as exc:
        checks.append(Check("audio_stream", False, str(exc), "Choose --device stick:<port> or a valid audio input."))
    try:
        speaker = probe_speaker(speaker_device, play, profile, settings)
        available = speaker["available"]
        detail = speaker["detail"] if available else speaker["detail"] + "; knock-only prompts remain viable"
        checks.append(Check("speaker", available, detail,
                            "Pi 5 has no output jack: connect USB/Bluetooth audio, select --speaker-device, or use prompter.voice: none.",
                            skipped=not available and not play))
    except Exception as exc:
        checks.append(Check("speaker", False, f"{exc}; knock-only prompts remain viable",
                            "Check aplay -l and --speaker-device, or use prompter.voice: none.", skipped=not play))
    active = None
    try:
        if profile.classifier.name == "clap":
            checks.append(probe_clap(profile))
            active = "clap"  # Models live in the external service, not this process.
        else:
            classifier = probe_classifier(profile)
            active = classifier["active"]
            checks.append(Check("classifier", active != "none" or profile.classifier.name == "none", classifier["detail"],
                                "Run uv sync --extra ml, then uv run --no-sync --extra ml python tools/fetch_models.py --allow-network."))
    except Exception as exc:
        checks.append(Check("classifier", False, str(exc), "Check the classifier profile, model files, and --extra ml."))
    try:
        model_paths = (Path(profile.classifier.model_dir) / "yamnet.tflite",
                       Path(profile.classifier.model_dir) / "yamnet_class_map.csv", Path(profile.classifier.head_path))
        present, absent = [], []
        for path in model_paths:
            (present if path.is_file() else absent).append(str(path))
        checks.append(Check("models", not absent,
                            "present: " + (", ".join(present) or "none") + "; missing: " + (", ".join(absent) or "none"),
                            "Run uv run --no-sync --extra ml python tools/fetch_models.py --allow-network; train a head separately if needed.",
                            skipped=bool(absent) and active in {"heuristic", "none", "head_dsp", "yamnet", "clap"}))
    except Exception as exc:
        checks.append(Check("models", False, str(exc), "Check model paths and filesystem permissions."))
    try:
        free = probe_disk(profile.server["runs_dir"])
        checks.append(Check("disk", free >= cfg["min_free_disk_bytes"], f"{free / 1024**3:.2f} GiB free for runs",
                            "Copy completed runs off the Pi and free space before a recording session."))
    except Exception as exc:
        checks.append(Check("disk", False, str(exc), "Check the runs directory and its filesystem permissions."))
    try:
        pi = probe_pi(settings)
        ok = not pi["available"] or (pi["temp_c"] < cfg["max_temp_c"] and not pi["throttled"] & 0xF)
        checks.append(Check("pi", ok, pi["detail"], "Use cooling and the official 27 W supply; historical throttle bits persist until reboot.",
                            skipped=not pi["available"]))
    except Exception as exc:
        checks.append(Check("pi", False, str(exc), "Run vcgencmd measure_temp and vcgencmd get_throttled on the Pi."))
    try:
        network = probe_network()
        port = profile.server["port"]
        urls = [f"http://{address}:{port}" for address in network["addresses"]]
        urls.append(f"http://{network['hostname']}.local:{port}")
        checks.append(Check("dashboard", bool(network["addresses"]), "; ".join(urls),
                            "Run hb run --dashboard; connect the phone to the same network. On Pi, hostname -I shows its IP."))
    except Exception as exc:
        checks.append(Check("dashboard", False, str(exc), "Join the same Wi-Fi/hotspot and check hostname -I on the Pi."))
    return checks


def format_doctor(checks: list[Check]) -> str:
    lines = []
    for check in checks:
        marker = "⚠️" if check.warning else "✅" if check.ok or check.skipped else "❌"
        suffix = " (skipped)" if check.skipped else ""
        lines.append(f"{marker} {check.name}{suffix}: {check.detail}")
        if check.hint:
            lines.append(f"   Hint: {check.hint}")
    return "\n".join(lines)


class _Timed:
    """Time named public calls while preserving the wrapped object's API."""

    def __init__(self, target, durations, methods, names=None):
        self.target, self.durations, self.methods, self.names = target, durations, methods, names

    def __getattr__(self, name):
        value = getattr(self.target, name)
        if name not in self.methods:
            return value

        def call(*args, **kwargs):
            started = time.perf_counter()
            try:
                result = value(*args, **kwargs)
                if self.names is not None:
                    actual = str(result.classifier)
                    if actual not in self.names:
                        self.names.append(actual)
                return result
            finally:
                self.durations.append(time.perf_counter() - started)

        return call


class _TimedSource:
    def __init__(self, source):
        self.source, self.synthesis_s = source, 0.0

    def __iter__(self):
        iterator = iter(self.source)
        while True:
            started = time.perf_counter()
            try:
                block = next(iterator)
            except StopIteration:
                self.synthesis_s += time.perf_counter() - started
                return
            self.synthesis_s += time.perf_counter() - started
            yield block

    def schedule(self, *args):
        started = time.perf_counter()
        try:
            self.source.schedule(*args)
        finally:
            self.synthesis_s += time.perf_counter() - started

    def close(self):
        self.source.close()


def _latency(values) -> dict:
    return {"mean_ms": float(np.mean(values) * 1000) if values else 0.0,
            "p95_ms": float(np.percentile(values, 95) * 1000) if values else 0.0,
            "calls": len(values)}


def peak_rss_mib() -> float:
    """ru_maxrss is bytes on macOS and KiB on Linux; this is process lifetime peak."""
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return float(rss / (1024**2 if sys.platform == "darwin" else 1024))


def run_bench(profile, *, seconds=None, classifier=None, seed=None, settings=None) -> dict:
    from .dialog import DialogEngine
    from .events import EventBus
    from .onset import OnsetDetector
    from .prompter import FakePrompter
    from .runner import DialogSession, Pipeline, SimulationSource

    settings = _settings(settings)
    cfg = settings["bench"]
    seconds = cfg["seconds"] if seconds is None else seconds
    seed = cfg["seed"] if seed is None else seed
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or round(seconds * SAMPLE_RATE) <= 0:
        raise ValueError("Benchmark seconds must be finite and contain at least one sample")
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise ValueError("Benchmark seed must be a nonnegative integer")
    started = time.perf_counter()
    if classifier is not None:
        profile = replace(profile, classifier=replace(profile.classifier, name=classifier))
    profile = replace(profile, dialog=dict(profile.dialog, auto_challenge=True, auto_interview=True))
    bus = EventBus()
    errors, actual_names = [], []
    counts = {"events": 0, "detections": 0}

    def collect(event):
        counts["events"] += 1
        if event.type == "sound":
            counts["detections"] += 1
        elif event.type == "error":
            errors.append(dict(event.data))

    bus.subscribe(collect)
    stages = {name: [] for name in ("onset", "classifier", "dialog")}
    chosen = make_classifier(profile, bus=bus)
    source = _TimedSource(SimulationSource(profile, duration_s=seconds, seed=seed, noise=cfg["noise"]))
    pipeline = Pipeline(source, _Timed(OnsetDetector(profile.onset), stages["onset"], {"process", "flush"}),
                        _Timed(chosen, stages["classifier"], {"classify"}, actual_names), bus, profile)
    prompter = FakePrompter(bus, cfg=profile.prompter, clock=lambda: pipeline.t, seed=seed,
                            people=cfg["people"], can_move=cfg["can_move"], hurt=cfg["hurt"])

    def engine_factory(*args, **kwargs):
        return _Timed(DialogEngine(*args, **kwargs), stages["dialog"],
                      {"on_sound", "tick", "on_prompts_finished", "command", "gate_until", "snapshot"})

    session = DialogSession(pipeline, prompter, rng_seed=seed, schedule=source.schedule,
                            stop_when_finished=False, engine_factory=engine_factory)
    startup_s = time.perf_counter() - started
    started = time.perf_counter()
    try:
        pipeline.run()
    finally:
        pipeline.stop()
        session.close()
    snapshot = session.engine.snapshot() if session.engine is not None else {}
    elapsed = time.perf_counter() - started
    active = str(getattr(chosen, "name", "none" if type(chosen).__name__ == "PassThroughClassifier" else type(chosen).__name__))
    if active not in actual_names:
        actual_names.append(active)
    audio_seconds = pipeline.t
    if audio_seconds <= 0:
        raise RuntimeError("Benchmark pipeline did not process audio")
    processing_s = max(0.0, elapsed - source.synthesis_s)
    return {
        "audio_seconds": audio_seconds, "pipeline_wall_s": elapsed, "startup_s": startup_s,
        "synthesis_s": source.synthesis_s, "processing_s": processing_s,
        "real_time_factor": elapsed / audio_seconds,
        "processing_real_time_factor": processing_s / audio_seconds,
        "stages": {name: _latency(values) for name, values in stages.items()},
        "peak_rss_mib": peak_rss_mib(), "classifier": ", ".join(actual_names),
        "classifiers": actual_names, "requested_classifier": profile.classifier.name,
        "detections": counts["detections"], "events": counts["events"], "errors": errors,
        "status": snapshot.get("status", "NO_SIGNS"),
        "scope": "CPU full Pipeline + DialogSession + FakePrompter; RTF includes synthetic generation, event dispatch and prompt-worker coordination. Model/source startup is separate. Hardware I/O, black-box disk writes, dashboard and network are excluded. RSS is the process-lifetime peak.",
    }


def format_bench(result: dict) -> str:
    lines = ["| Metric | Value |", "| --- | --- |",
             f"| Classifier actually run | {result['classifier']} (requested {result['requested_classifier']}) |",
             f"| Synthetic audio | {result['audio_seconds']:.3f} s |",
             f"| Full-pipeline real-time factor (wall/audio; lower is faster) | {result['real_time_factor']:.4f} |",
             f"| Pipeline wall time (includes synthesis) | {result['pipeline_wall_s']:.3f} s |",
             f"| Startup / model initialization | {result['startup_s']:.3f} s |",
             f"| Synthetic generation during pipeline | {result['synthesis_s']:.3f} s |",
             f"| Processing-only real-time factor | {result['processing_real_time_factor']:.4f} |"]
    for name in ("onset", "classifier", "dialog"):
        stage = result["stages"][name]
        lines.append(f"| {name.capitalize()} latency (mean / p95; {stage['calls']} calls) | {stage['mean_ms']:.3f} / {stage['p95_ms']:.3f} ms |")
    lines.extend([f"| Peak RSS (process lifetime) | {result['peak_rss_mib']:.2f} MiB |",
                  f"| Detections / events | {result['detections']} / {result['events']} |",
                  f"| Reported errors / warnings | {len(result['errors'])} |", "", result["scope"]])
    return "\n".join(lines)
