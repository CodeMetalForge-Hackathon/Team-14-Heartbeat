"""Lazy Raspberry Pi MJPEG capture with one retained complete frame.

``captured_monotonic`` is the host's complete-frame receipt time, *not* a
sensor exposure timestamp. The controller must stop and settle before asking
for a new frame; this interface cannot prove the sensor exposure time.

Flags: https://www.raspberrypi.com/documentation/accessories/camera-software.html
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import os
import shutil
import subprocess
import sys
import threading
from time import monotonic


SOI = b"\xff\xd8"
SOF_MARKERS = frozenset({0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                         0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF})


class CameraError(RuntimeError):
    """A sanitized capture failure; arbitrary subprocess text is never exposed."""


@dataclass(frozen=True)
class CameraFrame:
    jpeg: bytes
    captured_monotonic: float
    sequence: int


def _positive(value, name: str, *, integer: bool = False):
    try:
        valid = (not isinstance(value, bool) and isinstance(value, (int, float))
                 and value > 0 and math.isfinite(value)
                 and (not integer or isinstance(value, int)))
    except OverflowError:
        valid = False
    if not valid:
        kind = "integer" if integer else "number"
        raise CameraError(f"camera.{name} must be a finite positive {kind}")
    return int(value) if integer else float(value)


class _JPEGFraming:
    """Incremental marker framing, including header segments and scan stuffing.

    Segment payloads can contain an EOI-looking byte pair (for example inside
    metadata), so searching for the next FF D9 alone is insufficient. This is a
    bounded framing parser, not a full JPEG pixel decoder.
    """

    def __init__(self):
        self.position = len(SOI)
        self.in_scan = False
        self.saw_scan = False
        self.saw_sof = False

    def end(self, data: bytearray) -> int | None:
        while self.position < len(data):
            if self.in_scan:
                marker_start = data.find(b"\xff", self.position)
                if marker_start < 0:
                    self.position = len(data)
                    return None
            else:
                marker_start = self.position
                if data[marker_start] != 0xFF:
                    raise CameraError("Camera returned malformed JPEG framing")
            code_at = marker_start + 1
            while code_at < len(data) and data[code_at] == 0xFF:
                code_at += 1
            if code_at >= len(data):
                self.position = marker_start
                return None
            code = data[code_at]
            if self.in_scan and (code == 0x00 or 0xD0 <= code <= 0xD7):
                self.position = code_at + 1
                continue
            self.in_scan = False
            if code == 0xD9:
                if not self.saw_scan:
                    raise CameraError("Camera JPEG is missing image data")
                return code_at + 1
            if code in {0x00, 0xD8} or 0xD0 <= code <= 0xD7:
                raise CameraError("Camera returned an invalid JPEG marker")
            if code == 0x01:  # TEM is the only other marker without a length.
                self.position = code_at + 1
                continue
            length_at = code_at + 1
            if length_at + 2 > len(data):
                self.position = marker_start
                return None
            size = int.from_bytes(data[length_at:length_at + 2], "big")
            if size < 2:
                raise CameraError("Camera returned an invalid JPEG segment")
            segment_end = length_at + size
            if segment_end > len(data):
                self.position = marker_start
                return None
            if code in SOF_MARKERS:
                if size < 8:
                    raise CameraError("Camera returned an invalid JPEG image header")
                self.saw_sof = True
            if code == 0xDA:
                if not self.saw_sof or size < 6:
                    raise CameraError("Camera returned an invalid JPEG scan header")
                self.saw_scan = True
                self.in_scan = True
            self.position = segment_end
        return None


class RpicamCamera:
    """Persistent rpicam-vid MJPEG process, launched only by first capture().

    Required cfg keys: ``command`` (executable string or argv prefix), ``width``,
    ``height``, ``fps``, ``frame_timeout_s``, ``max_frame_bytes``,
    ``shutdown_timeout_s``, ``cancel_poll_s``, ``stream_read_bytes``.
    All subprocess output is private: stderr is drained and discarded, and
    errors report only a fixed diagnostic and, where available, exit status.

    Only one latest complete frame is retained. The transient stdout buffer is
    bounded by max_frame_bytes + stream_read_bytes, excluding that latest frame.
    A framing failure or frame timeout retires the camera rather than silently
    reusing stale data or restarting the hardware.
    """

    def __init__(self, cfg: dict):
        required = ("command", "width", "height", "fps", "frame_timeout_s",
                    "max_frame_bytes", "shutdown_timeout_s", "cancel_poll_s",
                    "stream_read_bytes")
        if not isinstance(cfg, dict) or any(name not in cfg for name in required):
            raise CameraError("Missing camera configuration; see the profile")
        command = cfg["command"]
        if isinstance(command, str):
            command = [command]
        if (not isinstance(command, (list, tuple)) or not command
                or any(not isinstance(arg, str) or not arg or "\x00" in arg for arg in command)):
            raise CameraError("camera.command must be an executable or argv prefix")
        self._command = list(command)
        self._width = _positive(cfg["width"], "width", integer=True)
        self._height = _positive(cfg["height"], "height", integer=True)
        self._fps = _positive(cfg["fps"], "fps")
        self._frame_timeout = _positive(cfg["frame_timeout_s"], "frame_timeout_s")
        self._frame_limit = _positive(cfg["max_frame_bytes"], "max_frame_bytes", integer=True)
        self._shutdown_timeout = _positive(cfg["shutdown_timeout_s"], "shutdown_timeout_s")
        self._cancel_poll = _positive(cfg["cancel_poll_s"], "cancel_poll_s")
        self._read_size = _positive(cfg["stream_read_bytes"], "stream_read_bytes", integer=True)
        if self._frame_limit < 4 or self._read_size > self._frame_limit:
            raise CameraError("Invalid camera frame/buffer size limits")
        self._condition = threading.Condition()
        self._stop_lock = threading.Lock()
        self._closed = False
        self._failure: str | None = None
        self._process: subprocess.Popen | None = None
        self._threads: list[threading.Thread] = []
        self._latest: CameraFrame | None = None
        self._sequence = 0

    def _start_locked(self) -> None:
        """Called with the condition held; construction never opens hardware."""
        if self._process is not None:
            return
        if sys.platform != "linux":
            raise CameraError("Raspberry Pi camera mode requires Raspberry Pi OS/Linux; run it on the Pi")
        if shutil.which(self._command[0]) is None:
            raise CameraError("Camera executable unavailable; install rpicam-apps and check camera.command")
        argv = [*self._command, "--codec", "mjpeg", "--timeout", "0", "--nopreview",
                "--width", str(self._width), "--height", str(self._height),
                "--framerate", str(self._fps), "--flush", "--output", "-"]
        child_environment = os.environ.copy()
        child_environment.pop("OPENAI_API_KEY", None)
        try:
            process = subprocess.Popen(argv, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       bufsize=0, shell=False, env=child_environment)
        except OSError:
            raise CameraError("Could not start camera; check rpicam-apps and camera permissions") from None
        self._process = process
        self._threads = [
            threading.Thread(target=self._read_stdout, args=(process,), daemon=True,
                             name="heartbeat-camera-jpeg"),
            threading.Thread(target=self._drain_stderr, args=(process,), daemon=True,
                             name="heartbeat-camera-stderr"),
        ]
        for thread in self._threads:
            thread.start()

    def capture(self, after: float, cancel: threading.Event | None = None) -> CameraFrame:
        """Wait for a complete JPEG received strictly after monotonic ``after``.

        Cancel/close/failure wake the wait; a canceled call does not relaunch or
        stop an otherwise healthy camera. Receipt freshness is not exposure
        freshness. Callers should use a host stop/settle boundary for ``after``.
        """
        try:
            valid_after = (not isinstance(after, bool) and isinstance(after, (int, float))
                           and math.isfinite(after))
        except OverflowError:
            valid_after = False
        if not valid_after:
            raise CameraError("Camera freshness boundary must be a finite monotonic time")
        deadline = monotonic() + self._frame_timeout
        timed_out = False
        with self._condition:
            if self._closed:
                raise CameraError("Camera is closed")
            if self._failure is not None:
                raise CameraError(self._failure)
            if cancel is not None and cancel.is_set():
                raise CameraError("Camera capture canceled")
            self._start_locked()
            while True:
                if self._closed:
                    raise CameraError("Camera is closed")
                if self._failure is not None:
                    raise CameraError(self._failure)
                if cancel is not None and cancel.is_set():
                    raise CameraError("Camera capture canceled")
                if self._latest is not None and self._latest.captured_monotonic > after:
                    return self._latest
                remaining = deadline - monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                self._condition.wait(min(remaining, self._cancel_poll))
        if timed_out:
            self._fail("Timed out waiting for a fresh camera frame; check the camera connection")
            raise CameraError("Timed out waiting for a fresh camera frame; check the camera connection")
        raise CameraError("Camera capture failed")

    def _read_stdout(self, process: subprocess.Popen) -> None:
        buffer = bytearray()
        framing = _JPEGFraming()
        try:
            if process.stdout is None:
                raise CameraError("Camera stdout is unavailable")
            while True:
                chunk = process.stdout.read(self._read_size)
                if not chunk:
                    code = process.poll()
                    suffix = "" if code is None else f" (exit {code})"
                    raise CameraError("Camera stream ended; check camera connection" + suffix)
                buffer.extend(chunk)
                while buffer:
                    if not buffer.startswith(SOI):
                        start = buffer.find(SOI)
                        if start < 0:
                            # Preserve a possible marker split across two reads.
                            buffer[:] = buffer[-1:] if buffer[-1] == 0xFF else b""
                            break
                        del buffer[:start]
                        framing = _JPEGFraming()
                    end = framing.end(buffer)
                    if end is None:
                        if len(buffer) > self._frame_limit:
                            raise CameraError("Camera JPEG exceeded the configured frame limit")
                        break
                    if end > self._frame_limit:
                        raise CameraError("Camera JPEG exceeded the configured frame limit")
                    jpeg = bytes(buffer[:end])
                    received = monotonic()
                    del buffer[:end]
                    framing = _JPEGFraming()
                    with self._condition:
                        if self._closed or self._failure is not None:
                            return
                        self._sequence += 1
                        self._latest = CameraFrame(jpeg, received, self._sequence)
                        self._condition.notify_all()
        except CameraError as exc:
            self._fail(str(exc))
        except (OSError, ValueError):
            self._fail("Camera stream read failed; check the camera connection")

    def _drain_stderr(self, process: subprocess.Popen) -> None:
        # Do not retain, decode, or log arbitrary driver output (including paths).
        try:
            if process.stderr is not None:
                while process.stderr.read(self._read_size):
                    pass
        except (OSError, ValueError):
            with self._condition:
                expected = self._closed or self._failure is not None
            if not expected:
                self._fail("Camera diagnostics stream failed; restart camera mode")

    def _fail(self, message: str) -> None:
        with self._condition:
            if not self._closed and self._failure is None:
                self._failure = message
                self._latest = None
            self._condition.notify_all()
        self._terminate_process()

    def _terminate_process(self) -> None:
        # Serialize termination so capture timeout, reader failure, and close
        # cannot race through terminate/kill/wait on the same child.
        with self._stop_lock:
            process = self._process
            if process is None:
                return
            try:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=self._shutdown_timeout / 2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=self._shutdown_timeout / 2)
                else:
                    process.wait(timeout=self._shutdown_timeout)
            except (OSError, subprocess.TimeoutExpired):
                # Capture is already inhibited; never surface subprocess text.
                with self._condition:
                    if self._failure is None and not self._closed:
                        self._failure = "Camera process did not stop within its shutdown deadline"
                    self._condition.notify_all()

    def close(self) -> None:
        """Wake waiting captures, stop the child and retire stream readers."""
        deadline = monotonic() + self._shutdown_timeout
        with self._condition:
            self._closed = True
            self._latest = None
            self._condition.notify_all()
        self._terminate_process()
        current = threading.current_thread()
        for thread in self._threads:
            if thread is not current:
                thread.join(max(0.0, deadline - monotonic()))
        process = self._process
        if process is not None:
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass
