"""Bounded audio input, always yielding 16 kHz mono float32 sample time."""

from __future__ import annotations

from dataclasses import dataclass
import importlib
import logging
import math
from pathlib import Path
import queue
import threading
import time
from typing import TYPE_CHECKING, Iterator, Protocol

import numpy as np
from scipy.signal import resample_poly
import soundfile as sf

if TYPE_CHECKING:
    from heartbeat.config import AudioConfig
    from heartbeat.events import EventBus


SAMPLE_RATE = 16000
log = logging.getLogger(__name__)


@dataclass
class AudioBlock:
    t0: float
    x: np.ndarray


class AudioSource(Protocol):
    def __iter__(self) -> Iterator[AudioBlock]: ...

    def close(self) -> None: ...


def _mono(samples: np.ndarray) -> np.ndarray:
    samples = np.asarray(samples, dtype=np.float32)
    if samples.ndim == 2:
        samples = samples.mean(axis=1, dtype=np.float32)
    if samples.ndim != 1:
        raise ValueError("Audio must be a mono vector or a frames-by-channels array")
    return np.clip(np.nan_to_num(samples, nan=0.0, posinf=1.0, neginf=-1.0), -1.0, 1.0)


class _StreamingResampler:
    """Apply resample_poly with overlapping, phase-aligned bounded windows.

    SciPy's default FIR has half-length 10 * max(up, down). Keep that input
    context on both sides, and defer outputs requiring future input. Aligning
    the retained window to ``down`` preserves the global polyphase alignment.
    Only ``finish`` zero-pads the end, just as resampling a complete file does.
    """

    def __init__(self, source_rate: int) -> None:
        divisor = math.gcd(int(source_rate), SAMPLE_RATE)
        self.up = SAMPLE_RATE // divisor
        self.down = int(source_rate) // divisor
        self._context = math.ceil(10 * max(self.up, self.down) / self.up) + 1
        self._buffer = np.empty(0, dtype=np.float32)
        self._start = 0
        self._received = 0
        self._emitted = 0

    def feed(self, samples: np.ndarray) -> np.ndarray:
        samples = _mono(samples)
        if self.up == self.down:
            return samples
        self._buffer = np.concatenate((self._buffer, samples))
        self._received += len(samples)
        safe_input = max(0, self._received - self._context)
        safe_output = (safe_input * self.up + self.down - 1) // self.down
        return self._take(safe_output)

    def finish(self) -> np.ndarray:
        if self.up == self.down or not len(self._buffer):
            return np.empty(0, dtype=np.float32)
        final_output = (self._received * self.up + self.down - 1) // self.down
        return self._take(final_output)

    def _take(self, stop: int) -> np.ndarray:
        if stop <= self._emitted:
            return np.empty(0, dtype=np.float32)
        converted = resample_poly(self._buffer, self.up, self.down)
        offset = self._start // self.down * self.up
        result = converted[self._emitted - offset : stop - offset].copy()
        self._emitted = stop
        retain_from = max(0, self._emitted * self.down // self.up - self._context)
        retain_from = retain_from // self.down * self.down
        discard = retain_from - self._start
        self._buffer = self._buffer[discard:].copy()
        self._start = retain_from
        return np.clip(result, -1.0, 1.0).astype(np.float32, copy=False)


class _Blocks:
    def __init__(self, block: int) -> None:
        self.block = block
        self.pending = np.empty(0, dtype=np.float32)

    def feed(self, samples: np.ndarray, *, final: bool = False) -> Iterator[np.ndarray]:
        self.pending = np.concatenate((self.pending, samples))
        while len(self.pending) >= self.block:
            result = self.pending[: self.block].copy()
            self.pending = self.pending[self.block :]
            yield result
        if final and len(self.pending):
            result = self.pending.copy()
            self.pending = np.empty(0, dtype=np.float32)
            yield result


class WavSource:
    def __init__(self, path: str | Path, realtime: bool = False, block: int = 256) -> None:
        if block <= 0:
            raise ValueError("Audio block size must be positive")
        self.path = Path(path)
        self.realtime = realtime
        self.block = block
        self._closed = False

    def __iter__(self) -> Iterator[AudioBlock]:
        delivered = 0
        started = time.monotonic() if self.realtime else 0.0
        with sf.SoundFile(self.path) as wav:
            converter = _StreamingResampler(wav.samplerate)
            blocks = _Blocks(self.block)
            read_size = max(1, math.ceil(self.block * wav.samplerate / SAMPLE_RATE))
            while not self._closed:
                raw = wav.read(read_size, dtype="float32", always_2d=True)
                final = len(raw) == 0
                samples = converter.finish() if final else converter.feed(raw)
                for samples_block in blocks.feed(samples, final=final):
                    if self._closed:
                        return
                    t0 = delivered / SAMPLE_RATE
                    if self.realtime:
                        delay = started + t0 - time.monotonic()
                        if delay > 0:
                            time.sleep(delay)
                    delivered += len(samples_block)
                    yield AudioBlock(t0, samples_block)
                if final:
                    return

    def close(self) -> None:
        self._closed = True


def _sounddevice():
    # WAV replay remains usable even when the host has no PortAudio library.
    return importlib.import_module("sounddevice")


def list_devices(*, bus: EventBus | None = None) -> list[dict]:
    """Return device records with explicit default-input/output markers."""
    try:
        sd = _sounddevice()
        defaults = sd.default.device
        result = []
        for index, item in enumerate(sd.query_devices()):
            device = dict(item)
            device.update(
                index=index,
                is_default_input=index == defaults[0],
                is_default_output=index == defaults[1],
                default=index == defaults[0],
            )
            result.append(device)
        return result
    except Exception as exc:
        message = f"Could not list audio devices: {exc}"
        if bus is not None:
            bus.publish("error", 0.0, source="audio", code="audio_devices_failed", message=message)
        else:
            log.warning("%s", message)
        return []


class MicSource:
    def __init__(
        self,
        device: int | str | None = None,
        block: int = 256,
        *,
        bus: EventBus | None = None,
        cfg: AudioConfig | None = None,
    ) -> None:
        if cfg is None:
            from heartbeat.config import load_profile

            cfg = load_profile("rubble").audio
        if block <= 0 or cfg.queue_blocks <= 0:
            raise ValueError("Audio block and queue sizes must be positive")
        if cfg.sample_rate != SAMPLE_RATE:
            raise ValueError("Heartbeat audio must use a 16000 Hz processing rate")
        self.device = device
        self.block = block
        self.bus = bus
        self._queue: queue.Queue[np.ndarray | None] = queue.Queue(cfg.queue_blocks)
        self._closed = threading.Event()
        self._lock = threading.Lock()
        self._stream = None
        self._delivered = 0
        self.overruns = 0
        self._reported_overruns = 0
        self._callback_status = ""
        self.input_rate = SAMPLE_RATE

    @property
    def overrun_count(self) -> int:
        return self.overruns

    def _report(self, message: str, *, severity: str = "error", **data) -> None:
        if self.bus is not None:
            self.bus.publish(
                "error", self._delivered / SAMPLE_RATE,
                source="audio", severity=severity, message=message, **data,
            )
        else:
            log.warning("%s", message)

    def _callback(self, indata, frames, time_info, status) -> None:
        if self._closed.is_set():
            return
        if status:
            with self._lock:
                self._callback_status = str(status)
        samples = np.asarray(indata, dtype=np.float32).copy()
        if not len(samples):
            return
        try:
            self._queue.put_nowait(samples)
        except queue.Full:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            with self._lock:
                self.overruns += 1
            try:
                self._queue.put_nowait(samples)
            except queue.Full:
                # close() may have inserted its wakeup sentinel concurrently.
                pass

    def _open_at(self, sd, sample_rate: int):
        stream = None
        try:
            stream = sd.InputStream(
                device=self.device, samplerate=sample_rate,
                channels=1, dtype="float32",
                blocksize=max(1, round(self.block * sample_rate / SAMPLE_RATE)),
                callback=self._callback,
            )
            stream.start()
            return stream
        except Exception:
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass
            raise

    def _open(self) -> bool:
        try:
            sd = _sounddevice()
            try:
                stream = self._open_at(sd, SAMPLE_RATE)
            except Exception as first_error:
                default_rate = int(round(sd.query_devices(self.device, "input")["default_samplerate"]))
                if default_rate <= 0 or default_rate == SAMPLE_RATE:
                    raise first_error
                stream = self._open_at(sd, default_rate)
                self.input_rate = default_rate
                self._report(
                    f"Input does not support 16000 Hz; resampling from {default_rate} Hz",
                    severity="warning", code="audio_resampling", input_rate=default_rate,
                )
            with self._lock:
                self._stream = stream
            if self._closed.is_set():
                self.close()
                return False
            return True
        except Exception as exc:
            self._report(f"Could not open microphone: {exc}", code="audio_open_failed")
            return False

    def _warnings(self) -> None:
        with self._lock:
            overruns, status = self.overruns, self._callback_status
            self._callback_status = ""
        if overruns != self._reported_overruns:
            self._report(
                f"Audio consumer lagged; dropped {overruns} oldest input blocks",
                severity="warning", code="audio_overrun", overruns=overruns,
                dropped_blocks=overruns - self._reported_overruns,
            )
            self._reported_overruns = overruns
        if status:
            self._report(f"Audio input status: {status}", severity="warning", code="audio_input_status")

    def __iter__(self) -> Iterator[AudioBlock]:
        if self._closed.is_set() or not self._open():
            self.close()
            return
        converter = _StreamingResampler(self.input_rate)
        blocks = _Blocks(self.block)
        try:
            while not self._closed.is_set():
                self._warnings()
                try:
                    raw = self._queue.get(timeout=self.block / SAMPLE_RATE)
                except queue.Empty:
                    if not getattr(self._stream, "active", True):
                        self._report("Microphone stream stopped", code="audio_input_stopped")
                        return
                    continue
                if raw is None:
                    return
                for samples in blocks.feed(converter.feed(raw)):
                    if self._closed.is_set():
                        return
                    t0 = self._delivered / SAMPLE_RATE
                    self._delivered += len(samples)
                    yield AudioBlock(t0, samples)
        except Exception as exc:
            self._report(f"Audio input failed: {exc}", code="audio_read_failed")
        finally:
            self.close()

    def close(self) -> None:
        self._closed.set()
        with self._lock:
            stream, self._stream = self._stream, None
        if stream is not None:
            for operation in ("stop", "close"):
                try:
                    getattr(stream, operation)()
                except Exception as exc:
                    self._report(f"Microphone {operation} failed: {exc}", code="audio_close_failed")
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(None)
            except (queue.Empty, queue.Full):
                pass
