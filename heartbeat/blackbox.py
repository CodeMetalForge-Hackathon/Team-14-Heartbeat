"""Asynchronous evidence recording, integrity checks and offline after-action tools.

The chain detects edits relative to its recorded root; it is not a signature.
An attacker able to replace both the complete chain and mutable metadata can
forge a new recording. Preserve an external copy of the final root for stronger
custody. Overflow, write failures and incomplete shutdown make verification fail.
"""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from dataclasses import asdict, dataclass, fields, is_dataclass, replace
from datetime import datetime, timezone
import hashlib
from importlib import metadata
import json
import math
from pathlib import Path
import platform
import queue
import socket
import subprocess
import sys
import threading

import numpy as np
import soundfile as sf

from .events import to_jsonable

SAMPLE_RATE = 16000  # Audio contract.
CLIP_SAMPLES = 15600  # Classifier input contract.
ZERO_HASH = "0" * 64
_STOP = object()


def canonical_json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


@dataclass(frozen=True)
class BlackBoxConfig:
    queue_size: int
    level_interval_s: float
    raw_flush_s: float
    replay_tolerance_s: float
    git_timeout_s: float

    @classmethod
    def parse(cls, profile):
        server = profile.server if hasattr(profile, "server") else profile["server"]
        raw = server["blackbox"]
        expected = {field.name for field in fields(cls)}
        unknown, missing = set(raw) - expected, expected - set(raw)
        if unknown or missing:
            raise ValueError(f"Invalid server.blackbox keys: unknown={sorted(unknown)}, missing={sorted(missing)}")
        result = cls(**raw)
        if isinstance(result.queue_size, bool) or not isinstance(result.queue_size, int) or result.queue_size < 1:
            raise ValueError("server.blackbox.queue_size must be a positive integer")
        for name in expected - {"queue_size"}:
            value = getattr(result, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"server.blackbox.{name} must be a finite positive number")
        return result


class BlackBox:
    """One writer owns every file; publisher callbacks only copy and enqueue.

    Subscribe this object to the event bus, attach ``on_audio`` to the pipeline's
    raw-block hook, then unsubscribe and call ``close`` after session shutdown.
    ``bus`` is optional and lets writer failures also reach live subscribers.
    Queue overflow raises once to the caller's error handler, records an error
    on the writer thread and permanently invalidates the recording's metadata.
    """

    def __init__(self, run_dir, profile, argv, *, bus=None):
        self.run_dir = Path(run_dir).absolute()
        self.cfg = BlackBoxConfig.parse(profile)
        self._profile = to_jsonable(asdict(profile) if is_dataclass(profile) else deepcopy(profile))
        self._argv = list(sys.argv if argv is None else argv)
        self._bus = bus
        self._queue = queue.Queue(maxsize=self.cfg.queue_size)
        self._lock = threading.Lock()
        self._closing = threading.Event()
        self._ready = threading.Event()
        self._accepting = True
        self._pending_notice = None
        self._last_t = 0.0
        self._state = {
            "run_id": self.run_dir.name, "run_dir": str(self.run_dir),
            "events": 0, "clips": 0, "raw_samples": 0, "raw_flushed_samples": 0,
            "dropped_events": 0, "dropped_audio": 0, "error": None,
            "failed": False, "closed": False, "integrity_valid": True,
            "final_hash": ZERO_HASH,
        }
        self._thread = threading.Thread(target=self._writer, name="heartbeat-blackbox", daemon=True)
        self._thread.start()
        self._ready.wait()

    def snapshot(self) -> dict:
        with self._lock:
            result = dict(self._state)
        result["dropped"] = result["dropped_events"] + result["dropped_audio"]
        return result

    status = snapshot

    def __call__(self, event) -> None:
        # No paths, JSON encoding, hashing or file operations on this thread.
        clip = None if event.clip is None else np.array(event.clip, dtype=np.float32, copy=True)
        self._enqueue("event", (str(event.type), float(event.t), str(event.wall), deepcopy(event.data), clip), event.t)

    def on_audio(self, block) -> None:
        self._enqueue("audio", (float(block.t0), np.array(block.x, dtype=np.float32, copy=True)), block.t0)

    def _enqueue(self, kind, payload, t):
        with self._lock:
            if not self._accepting:
                return
            self._last_t = max(self._last_t, float(t))
            if self._state["failed"]:
                self._state["dropped_events" if kind == "event" else "dropped_audio"] += 1
                return
            try:
                self._queue.put_nowait((kind, payload))
                return
            except queue.Full:
                self._state["dropped_events" if kind == "event" else "dropped_audio"] += 1
                first = self._state["error"] is None
                self._state["integrity_valid"] = False
                message = "Black-box queue overflow: evidence was dropped; recording is invalid"
                self._state["error"] = self._state["error"] or message
                if first:
                    self._pending_notice = (self._last_t, message)
        if first:
            raise RuntimeError(message)

    def close(self) -> None:
        """Stop accepting data, drain accepted records and finish valid headers."""
        with self._lock:
            if not self._accepting:
                already_closing = True
            else:
                already_closing = False
                self._accepting = False
                self._closing.set()
                try:
                    self._queue.put_nowait(_STOP)
                except queue.Full:
                    pass  # The writer observes _closing after draining the queue.
        if threading.current_thread() is self._thread:
            return
        if not already_closing or self._thread.is_alive():
            self._thread.join()

    def _metadata(self):
        versions = {}
        for name in ("heartbeat", "numpy", "scipy", "soundfile", "sounddevice", "PyYAML", "pyserial",
                     "fastapi", "uvicorn", "websockets", "scikit-learn", "joblib", "ai-edge-litert"):
            try:
                versions[name] = metadata.version(name)
            except metadata.PackageNotFoundError:
                versions[name] = None
        try:
            result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=Path(__file__).resolve().parent.parent,
                                    capture_output=True, text=True, timeout=self.cfg.git_timeout_s, check=False)
            commit = result.stdout.strip() if result.returncode == 0 else None
        except (OSError, subprocess.SubprocessError):
            commit = None
        return {
            "schema_version": 1, "run_id": self.run_dir.name, "profile": self._profile,
            "argv": to_jsonable(self._argv), "git_commit": commit, "hostname": socket.gethostname(),
            "platform": platform.platform(), "python": sys.version, "packages": versions,
            "start_time": datetime.now(timezone.utc).isoformat(), "sample_rate": SAMPLE_RATE,
            "closed": False, "completed": False, "integrity_valid": True,
        }

    def _write_meta(self, meta):
        temporary = self.run_dir / "meta.json.tmp"
        temporary.write_text(json.dumps(meta, sort_keys=True, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        temporary.replace(self.run_dir / "meta.json")

    def _fail(self, exc):
        message = f"Black-box writer failed: {exc}"
        with self._lock:
            self._state.update(failed=True, integrity_valid=False)
            self._state["error"] = self._state["error"] or message
        if self._bus is not None:
            self._bus.publish("error", self._last_t, source="blackbox", message=message, integrity_valid=False)

    def _write_event(self, stream, kind, t, wall, data, clip):
        if kind == "level":
            if self._last_level is not None and t - self._last_level < self.cfg.level_interval_s:
                return
            self._last_level = t
        with self._lock:
            seq = self._state["events"] + 1
            prev = self._state["final_hash"]
        data = to_jsonable(data)
        if kind == "sound":
            if clip is None or clip.shape != (CLIP_SAMPLES,) or not np.isfinite(clip).all():
                raise ValueError("sound event is missing its finite 15,600-sample clip")
            relative = f"clips/{seq}.wav"
            path = self.run_dir / relative
            sf.write(path, clip, SAMPLE_RATE, subtype="PCM_16")
            data["clip"], data["clip_sha256"] = relative, _sha256(path)
        payload = {"seq": seq, "t": float(t), "wall": wall, "type": kind, "data": data}
        digest = hashlib.sha256((prev + canonical_json(payload)).encode("utf-8")).hexdigest()
        stream.write(canonical_json(dict(payload, prev=prev, hash=digest)) + "\n")
        stream.flush()
        with self._lock:
            self._state["events"] = seq
            self._state["final_hash"] = digest
            if kind == "sound":
                self._state["clips"] += 1

    def _notice(self, stream):
        with self._lock:
            notice, self._pending_notice = self._pending_notice, None
        if notice is not None:
            t, message = notice
            self._write_event(stream, "error", t, datetime.now(timezone.utc).isoformat(),
                              {"source": "blackbox", "message": message, "integrity_valid": False}, None)

    def _write_audio(self, raw, t0, x):
        if not math.isfinite(t0) or t0 < 0 or x.ndim != 1 or not np.isfinite(x).all():
            raise ValueError("raw audio must be finite mono samples on a nonnegative stream clock")
        with self._lock:
            expected = self._state["raw_samples"]
        if round(t0 * SAMPLE_RATE) != expected:
            raise ValueError(f"raw audio discontinuity: expected sample {expected}, got {round(t0 * SAMPLE_RATE)}")
        raw.write(x)
        with self._lock:
            self._state["raw_samples"] += len(x)
            samples = self._state["raw_samples"]
        if samples - self._flushed >= self._flush_samples:
            raw.flush()
            self._flushed = samples
            with self._lock:
                self._state["raw_flushed_samples"] = samples

    def _writer(self):
        stream, raw, meta = None, None, None
        self._last_level = None
        self._flushed = 0
        self._flush_samples = max(1, round(self.cfg.raw_flush_s * SAMPLE_RATE))
        try:
            if self.run_dir.exists() and any(self.run_dir.iterdir()):
                raise FileExistsError(f"refusing to overwrite nonempty run directory {self.run_dir}")
            self.run_dir.mkdir(parents=True, exist_ok=True)
            (self.run_dir / "clips").mkdir()
            meta = self._metadata()
            self._write_meta(meta)
            stream = (self.run_dir / "events.jsonl").open("x", encoding="utf-8")
            raw = sf.SoundFile(self.run_dir / "raw.wav", "w", samplerate=SAMPLE_RATE, channels=1, subtype="PCM_16")
            raw.flush()
            self._ready.set()
            while True:
                if self._closing.is_set() and self._queue.empty():
                    break
                item = self._queue.get()
                if item is _STOP:
                    break
                self._notice(stream)
                kind, payload = item
                if kind == "event":
                    self._write_event(stream, *payload)
                else:
                    self._write_audio(raw, *payload)
            self._notice(stream)
        except Exception as exc:
            self._fail(exc)
        finally:
            self._ready.set()
            for handle in (raw, stream):
                if handle is not None:
                    try:
                        handle.close()
                        if handle is raw:
                            with self._lock:
                                self._state["raw_flushed_samples"] = self._state["raw_samples"]
                    except Exception as exc:
                        self._fail(exc)
            with self._lock:
                final = dict(self._state)
            if meta is not None:
                try:
                    final.update(closed=True, completed=final["integrity_valid"] and not final["failed"])
                    meta.update(final)
                    meta["end_time"] = datetime.now(timezone.utc).isoformat()
                    meta["raw_sha256"] = _sha256(self.run_dir / "raw.wav")
                    self._write_meta(meta)
                except Exception as exc:
                    self._fail(exc)
            with self._lock:
                self._state["closed"] = True


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    events: int
    clips: int
    seq: int | None = None
    reason: str | None = None

    def __str__(self):
        return (f"OK {self.events} events, {self.clips} clips" if self.ok
                else f"TAMPERED at seq {self.seq}: {self.reason}")


def _records(run_dir):
    with (Path(run_dir) / "events.jsonl").open(encoding="utf-8") as stream:
        return [json.loads(line) for line in stream]


def verify_run(run_dir) -> VerifyResult:
    run_dir = Path(run_dir)
    events, clips, seq, prev = 0, 0, 0, ZERO_HASH
    try:
        meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
        if not isinstance(meta, dict):
            raise ValueError("metadata must be an object")
        with (run_dir / "events.jsonl").open(encoding="utf-8") as stream:
            for seq, line in enumerate(stream, 1):
                record = json.loads(line)
                if set(record) != {"seq", "t", "wall", "type", "data", "prev", "hash"}:
                    raise ValueError("invalid record fields")
                if record["seq"] != seq or isinstance(record["seq"], bool) or not isinstance(record["seq"], int):
                    raise ValueError("nonconsecutive sequence")
                if record["prev"] != prev:
                    raise ValueError("previous hash mismatch")
                if not isinstance(record["data"], dict):
                    raise ValueError("event data must be an object")
                if (not isinstance(record["type"], str) or not isinstance(record["wall"], str)
                        or isinstance(record["t"], bool) or not isinstance(record["t"], (int, float))
                        or not math.isfinite(record["t"]) or record["t"] < 0):
                    raise ValueError("invalid event type, wall time or stream timestamp")
                payload = {key: record[key] for key in ("seq", "t", "wall", "type", "data")}
                digest = hashlib.sha256((prev + canonical_json(payload)).encode("utf-8")).hexdigest()
                if record["hash"] != digest:
                    raise ValueError("record hash mismatch")
                if record["type"] == "sound":
                    data = record["data"]
                    if data.get("clip") != f"clips/{seq}.wav":
                        raise ValueError("invalid clip path")
                    path = run_dir / data["clip"]
                    if _sha256(path) != data.get("clip_sha256"):
                        raise ValueError("clip hash mismatch")
                    info = sf.info(path)
                    if (info.samplerate, info.channels, info.frames, info.subtype) != (SAMPLE_RATE, 1, CLIP_SAMPLES, "PCM_16"):
                        raise ValueError("clip audio format mismatch")
                    clips += 1
                prev = digest
                events += 1
        seq = events + 1
        if any(meta.get(key) is not True for key in ("integrity_valid", "closed", "completed")):
            raise ValueError(meta.get("error") or "recording is incomplete or invalid")
        if meta.get("events") != events or meta.get("final_hash") != prev:
            raise ValueError("final chain root or event count mismatch (possible truncation)")
        if meta.get("clips") != clips:
            raise ValueError("clip count mismatch")
        info = sf.info(run_dir / "raw.wav")
        if (info.samplerate, info.channels, info.subtype) != (SAMPLE_RATE, 1, "PCM_16") or info.frames != meta.get("raw_samples"):
            raise ValueError("raw audio format or duration mismatch")
        if _sha256(run_dir / "raw.wav") != meta.get("raw_sha256"):
            raise ValueError("raw audio hash mismatch")
        return VerifyResult(True, events, clips)
    except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
        return VerifyResult(False, events, clips, seq, str(exc))


def _profile_from_metadata(data):
    from .config import AudioConfig, ClassifierConfig, OnsetConfig, Profile

    return Profile(AudioConfig(**data["audio"]), OnsetConfig(**data["onset"]), ClassifierConfig(**data["classifier"]),
                   data["dialog"], data["prompter"], data["server"])


def compare_sounds(original, replayed, tolerance_s):
    """One-to-one nearest timestamp matches; classification changes stay visible."""
    tolerance_samples = round(tolerance_s * SAMPLE_RATE)
    available = set(range(len(replayed)))
    matches, missing = [], []
    for old in original:
        candidates = [index for index in available
                      if abs(round(replayed[index]["t"] * SAMPLE_RATE) - round(old["t"] * SAMPLE_RATE)) <= tolerance_samples]
        if not candidates:
            missing.append(old["t"])
            continue
        index = min(candidates, key=lambda candidate: (abs(replayed[candidate]["t"] - old["t"]), candidate))
        new = replayed[index]
        available.remove(index)
        matches.append({"original_t": old["t"], "replayed_t": new["t"],
                        "original_is_knock": old["is_knock"], "replayed_is_knock": new["is_knock"]})
    return {"matched": len(matches), "missing": len(missing), "new": len(available),
            "original": len(original), "replayed": len(replayed), "matches": matches,
            "missing_times": missing, "new_times": [replayed[index]["t"] for index in sorted(available)],
            "changed": sum(item["original_is_knock"] != item["replayed_is_knock"] for item in matches)}


def replay_run(path, profile=None, classifier=None) -> dict:
    """Replay a run's PCM recording (including prompt gates), or a standalone WAV."""
    from .audio import WavSource
    from .classify import make_classifier
    from .config import load_profile
    from .events import EventBus
    from .onset import OnsetDetector

    path = Path(path)
    records = []
    if path.is_dir():
        check = verify_run(path)
        if not check.ok:
            raise ValueError(str(check))
        meta = json.loads((path / "meta.json").read_text(encoding="utf-8"))
        records = _records(path)
        if profile is None:
            profile = _profile_from_metadata(meta["profile"])
        wav = path / "raw.wav"
    else:
        wav = path
    if profile is None or isinstance(profile, (str, Path)):
        profile = load_profile("rubble" if profile is None else profile)
    if classifier is not None:
        profile = replace(profile, classifier=replace(profile.classifier, name=classifier))
    detector = OnsetDetector(profile.onset)
    starts = {}
    for record in records:
        if record["type"] == "prompt_start":
            starts[record["data"].get("batch", record["seq"])] = record["t"]
        elif record["type"] == "prompt_end":
            start = starts.pop(record["data"].get("batch"), None)
            if start is not None:
                detector.gate(start, record["t"] + profile.prompter["gate_tail_s"])
    for start in starts.values():
        detector.gate(start, math.inf)
    bus = EventBus()
    errors = []
    bus.subscribe(lambda event: errors.append(event.to_dict()) if event.type == "error" else None)
    model = make_classifier(profile, bus=bus)
    source = WavSource(wav, block=profile.audio.block)
    replayed = []

    def classify(onsets):
        for onset in onsets:
            det = model.classify(onset)
            replayed.append({"t": float(det.t), "is_knock": bool(det.is_knock)})

    try:
        for block in source:
            classify(detector.process(block))
        classify(detector.flush())
    finally:
        source.close()
    original = [{"t": item["t"], "is_knock": bool(item["data"].get("is_knock"))}
                for item in records if item["type"] == "sound"]
    tolerance = BlackBoxConfig.parse(profile).replay_tolerance_s
    result = compare_sounds(original, replayed, tolerance)
    result["errors"] = errors
    return result


def report_run(run_dir) -> Path:
    """Write a human-readable after-action report, including evidence integrity."""
    run_dir = Path(run_dir)
    records = _records(run_dir)
    meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    check = verify_run(run_dir)
    sounds = [record for record in records if record["type"] == "sound"]
    knocks = sum(bool(record["data"].get("is_knock")) and not record["data"].get("gated") for record in sounds)
    states = [record["data"] for record in records if record["type"] == "state"]
    final = states[-1] if states else {}
    duration = sf.info(run_dir / "raw.wav").duration
    rounds = [record for record in records if record["type"] == "round_result"]
    answers = final.get("answers")
    if answers is None:
        answers = {record["data"]["question"]: record["data"]["value"] for record in records if record["type"] == "answer"}
    approvals = [record for record in records if record["type"] == "approval"]
    errors = [record for record in records if record["type"] == "error"]
    lines = [f"# Heartbeat after-action report: {run_dir.name}", "", f"Evidence integrity: **{check}**", "",
             "The hash chain is tamper-evident, not digitally signed. A complete rewrite of both the chain "
             "and mutable metadata cannot be detected without an external copy of the final root.", "",
             "## Duration", "", f"{duration:.3f} seconds of 16 kHz mono raw audio.",
             f"Started: {meta.get('start_time', 'unknown')}.", "", "## Sounds and knocks", "",
             f"{len(sounds)} sounds; {knocks} accepted, ungated knocks; "
             f"{sum(bool(record['data'].get('gated')) for record in sounds)} gated sounds.",
             f"Classifiers: {dict(Counter(record['data'].get('classifier', 'unknown') for record in sounds))}.",
             "", "## Background rate", "",
             f"Final passive estimate: {final.get('lambda_per_min', 'unavailable')} events per minute.",
             "", "## Rounds", "", "| Round | Time (s) | Asked | Heard | p | Matched |",
             "| --- | --- | --- | --- | --- | --- |"]
    for record in rounds:
        data = record["data"]
        lines.append(f"| {data['round']} | {record['t']:.3f} | {data['expected']} | {data['heard']} | {data['p']} | {data['matched']} |")
    if not rounds:
        lines.append("| — | — | — | — | — | — |")
    latest_odds = next((record["data"]["odds"] for record in reversed(records) if "odds" in record["data"]), "unavailable")
    lines += ["", "## Final odds, answers and approvals", "", f"Final status: {final.get('status', 'unavailable')}.",
              f"Independent-background model odds: {latest_odds}.",
              "These are model likelihoods, not a probability a person is present. Periodic or correlated noise "
              "and repeated trials can violate the independence assumption.", "", f"Answers: {json.dumps(answers, sort_keys=True)}.",
              f"Approvals: {len(approvals)}."]
    for record in approvals:
        lines.append(f"- {record['t']:.3f}s: operator {record['data'].get('operator') or 'unspecified'}; approved={record['data'].get('approved')}.")
    lines += ["", "## Errors", ""]
    lines += [f"- {record['t']:.3f}s [{record['data'].get('severity', 'error')}]: {record['data'].get('message', record['data'])}"
              for record in errors] or ["None recorded."]
    path = run_dir / "report.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
