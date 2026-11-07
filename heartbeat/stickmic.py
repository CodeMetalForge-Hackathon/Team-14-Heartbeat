"""M5StickC serial audio and a nonblocking LCD status subscriber.

Transport deadlines bound hardware failures only. Audio timestamps always come
from the number of delivered samples, including silence for lost frames.
"""

from __future__ import annotations

import binascii
import importlib
import logging
import math
import os
from pathlib import Path
import queue
import struct
import subprocess
import sys
import threading
import time
from typing import Iterator

import numpy as np

from .audio import AudioBlock, SAMPLE_RATE


log = logging.getLogger(__name__)
MAGIC = b"\xa5\x5a"
BLOCK_SAMPLES = 256
FRAME_BYTES = 520
BAUD_RATE = 921600
# Protocol/transport bounds, not audio or detector tuning parameters.
READY_TIMEOUT_S = 3.0
READ_POLL_S = 0.05
WRITE_TIMEOUT_S = 0.25
FRAME_IDLE_TIMEOUT_S = 1.0
PROBE_TIMEOUT_S = 0.35
MAX_LINE_BYTES = 512
MAX_STATUS_TEXT = 40
SEQ_MODULUS = 1 << 16
SEQ_HALF_RANGE = SEQ_MODULUS // 2
_PORTS: set[str] = set()
_PORTS_LOCK = threading.Lock()


def canonical_port(port: str) -> str:
    """Share reservations between equivalent paths, including macOS aliases."""
    result = os.path.realpath(os.fspath(port))
    if sys.platform == "darwin" and result.startswith("/dev/tty."):
        result = "/dev/cu." + result[len("/dev/tty.") :]
    return result


def _os_port_in_use(port: str) -> bool:
    """Inspect OS ownership without opening (and possibly resetting) the board."""
    if not Path(port).exists():
        return False
    names = {Path(port).name}
    if sys.platform == "darwin" and Path(port).name.startswith(("cu.", "tty.")):
        names.update({"cu." + Path(port).name.split(".", 1)[1],
                      "tty." + Path(port).name.split(".", 1)[1]})
    for directory in (Path("/var/lock"), Path("/run/lock"), Path("/tmp")):
        for name in names:
            lock = directory / f"LCK..{name}"
            try:
                if not lock.exists():
                    continue
                raw = lock.read_bytes().strip()
                try:
                    pid = int(raw)
                except ValueError:
                    # Some UUCP applications store a native binary PID.
                    pid = int.from_bytes(raw, sys.byteorder) if len(raw) == 4 else 0
                if pid <= 0:
                    return True
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    continue
                return True
            except PermissionError:
                return True
            except OSError:
                continue
    if sys.platform.startswith("linux"):
        deadline = time.monotonic() + 0.1
        try:
            for process in Path("/proc").iterdir():
                if time.monotonic() >= deadline:
                    return True
                if not process.name.isdigit():
                    continue
                try:
                    for descriptor in (process / "fd").iterdir():
                        try:
                            if canonical_port(os.readlink(descriptor)) == port:
                                return True
                        except OSError:
                            continue
                except OSError:
                    continue
        except OSError:
            pass
    elif sys.platform == "darwin":
        paths = [str(Path("/dev") / name) for name in names]
        try:
            result = subprocess.run(
                ["/usr/sbin/lsof", "-nP", "-t", "--", *paths],
                capture_output=True, timeout=0.1, check=False,
            )
            return bool(result.stdout.strip())
        except subprocess.TimeoutExpired:
            return True  # An uncertain owner is safer to skip during discovery.
        except OSError:
            pass
    return False


def port_in_use(port: str) -> bool:
    """Check Heartbeat reservations and externally owned serial devices."""
    port = canonical_port(port)
    with _PORTS_LOCK:
        if port in _PORTS:
            return True
    return _os_port_in_use(port)


def claim_port(port: str) -> bool:
    """Reserve a port until release_port; the OS exclusive open remains final."""
    port = canonical_port(port)
    if port_in_use(port):
        return False
    with _PORTS_LOCK:
        if port in _PORTS:
            return False
        _PORTS.add(port)
    return True


def release_port(port: str) -> None:
    with _PORTS_LOCK:
        _PORTS.discard(canonical_port(port))


def new_serial_connection(
    port: str, baud: int, timeout: float, *,
    write_timeout: float = WRITE_TIMEOUT_S, serial_factory=None,
):
    """Configure modem lines before opening, avoiding ESP32 auto-reset pulses.

    Callers own the reservation and must close the returned connection. The
    optional factory is for hardware-free tests, never a different wire format.
    """
    factory = serial_factory or importlib.import_module("serial").Serial
    connection = factory()
    try:
        connection.dtr = False
        connection.rts = False
        connection.baudrate = int(baud)
        connection.timeout = float(timeout)
        connection.write_timeout = float(write_timeout)
        connection.exclusive = True
        connection.port = port
        opener = getattr(connection, "open", None)
        if opener is not None:
            opener()
        return connection
    except Exception:
        try:
            connection.close()
        except Exception:
            pass
        raise


def crc16(data: bytes | bytearray) -> int:
    """CRC-16/CCITT-FALSE: poly 0x1021, initial 0xffff, no reflection/xor."""
    return binascii.crc_hqx(data, 0xFFFF)


def _read_line(connection, deadline: float, monotonic) -> str | None:
    pending = bytearray()
    while monotonic() < deadline:
        connection.timeout = min(READ_POLL_S, max(0.0, deadline - monotonic()))
        byte = connection.read(1)
        if not byte:
            if not getattr(connection, "is_open", True):
                raise OSError("Serial device disconnected")
            continue
        if byte in (b"\r", b"\n"):
            if pending:
                return pending.decode("ascii", errors="replace")
            continue
        pending.extend(byte)
        if len(pending) > MAX_LINE_BYTES:
            # An abandoned stream can still be sending binary at attach time.
            # Keep a bounded suffix so startup reaches its STOP/PING recovery.
            del pending[:-MAX_LINE_BYTES]
    return None


def _is_stick_pong(line: str | None) -> bool:
    expected = "PONG stickmic v1"
    if line == expected:
        return True
    if line is not None and line.endswith(expected):
        prefix = line[:-len(expected)]
        # STOP has no ACK. Its last binary frame may precede PONG without a
        # newline; accept the exact identity after that non-text prefix only.
        return any(ord(char) < 32 or ord(char) > 126 for char in prefix)
    return False


class StickMicSource:
    def __init__(
        self, port: str, baud: int = BAUD_RATE, gain_db: float = 0.0, bus=None, *,
        serial_factory=None, monotonic=None,
    ) -> None:
        if not isinstance(port, str) or not port.strip():
            raise ValueError("A stick microphone serial port is required")
        if baud <= 0 or not math.isfinite(gain_db):
            raise ValueError("Stick baud must be positive and gain must be finite")
        self.port, self.baud, self.gain_db, self.bus = port, int(baud), float(gain_db), bus
        self.serial_factory = serial_factory
        self.monotonic = monotonic or time.monotonic
        self.bad_frames = 0
        self.missing_frames = 0
        self.stale_frames = 0
        self.garbage_bytes = 0
        self._gain = 10.0 ** (self.gain_db / 20.0)
        self._connection = None
        self._write_lock = threading.Lock()
        self._closed = threading.Event()
        self._streaming = False
        self._claimed = False
        self._claim_key = canonical_port(port)
        self._delivered = 0

    def _report(self, message: str, *, code: str, severity: str = "error", **data) -> None:
        if self.bus is not None:
            self.bus.publish(
                "error", self._delivered / SAMPLE_RATE, source="stickmic",
                code=code, severity=severity, message=message,
                bad_frames=self.bad_frames, missing_frames=self.missing_frames,
                stale_frames=self.stale_frames, **data,
            )
        else:
            log.warning("%s", message)

    def _write(self, command: str) -> bool:
        with self._write_lock:
            if self._closed.is_set() or self._connection is None:
                return False
            payload = (command + "\n").encode("ascii")
            if self._connection.write(payload) != len(payload):
                raise OSError("Incomplete stick microphone command write")
        return True

    def _open(self) -> bool:
        if self._closed.is_set():
            return False
        try:
            self._claim_key = canonical_port(self.port)
            if not claim_port(self._claim_key):
                raise OSError(f"Serial port is already in use: {self.port}")
            self._claimed = True
            connection = new_serial_connection(
                self.port, self.baud, READ_POLL_S, serial_factory=self.serial_factory,
            )
            with self._write_lock:
                if self._closed.is_set():
                    connection.close()
                    return False
                self._connection = connection
            deadline = self.monotonic() + READY_TIMEOUT_S
            ready = False
            while not self._closed.is_set():
                line = _read_line(connection, deadline, self.monotonic)
                if line is None:
                    break
                if line.startswith("READY stickmic v1"):
                    ready = True
                    break
                if line.startswith(("READY ", "PONG ")):
                    raise OSError(f"Serial device is not a stick microphone: {line}")
            if not ready and not self._write("STOP"):
                return False
            if not self._write("PING"):
                return False
            deadline = self.monotonic() + PROBE_TIMEOUT_S
            while not self._closed.is_set():
                line = _read_line(connection, deadline, self.monotonic)
                if _is_stick_pong(line):
                    break
                if line is not None and line.endswith("PONG stickmic v1"):
                    # A final binary suffix may itself be printable ASCII.
                    # Ask again now that STOP has established text mode, and
                    # require a fresh identity within the original deadline.
                    if not self._write("PING"):
                        return False
                    continue
                if line is None:
                    raise TimeoutError("No PONG stickmic v1 reply")
                if line.startswith(("PONG", "READY knocker")):
                    raise OSError(f"Unexpected stick microphone reply: {line}")
            if not self._write("START"):
                return False
            self._streaming = True
            return True
        except Exception as exc:
            if not self._closed.is_set():
                self._report(f"Could not open stick microphone: {exc}", code="stick_open_failed")
            return False

    def _frames(self) -> Iterator[tuple[int, np.ndarray]]:
        pending = bytearray()
        deadline = self.monotonic() + FRAME_IDLE_TIMEOUT_S
        while not self._closed.is_set():
            start = pending.find(MAGIC)
            if start < 0:
                keep = 1 if pending.endswith(MAGIC[:1]) else 0
                self.garbage_bytes += len(pending) - keep
                pending[:] = pending[-1:] if keep else b""
            elif start:
                self.garbage_bytes += start
                del pending[:start]
            if pending.startswith(MAGIC) and len(pending) >= 6:
                seq, count = struct.unpack_from("<HH", pending, 2)
                if count != BLOCK_SAMPLES:
                    self.bad_frames += 1
                    self._report("Invalid stick frame sample count; resynchronizing",
                                 code="stick_bad_frame", severity="warning")
                    del pending[0]
                    continue
                if len(pending) >= FRAME_BYTES:
                    frame = pending[:FRAME_BYTES]
                    expected_crc = struct.unpack_from("<H", frame, FRAME_BYTES - 2)[0]
                    if crc16(frame[2:-2]) != expected_crc:
                        self.bad_frames += 1
                        self._report("Stick frame CRC failed; resynchronizing",
                                     code="stick_bad_frame", severity="warning")
                        del pending[0]
                        continue
                    del pending[:FRAME_BYTES]
                    deadline = self.monotonic() + FRAME_IDLE_TIMEOUT_S
                    samples = np.frombuffer(frame, dtype="<i2", count=BLOCK_SAMPLES, offset=6)
                    samples = samples.astype(np.float32) / np.float32(32768.0)
                    samples *= self._gain
                    np.clip(samples, -1.0, 1.0, out=samples)
                    yield seq, samples
                    # Time spent by the classifier/consumer is not a serial
                    # transport stall, even when a Pi takes longer than 1 s.
                    deadline = self.monotonic() + FRAME_IDLE_TIMEOUT_S
                    continue
            if self.monotonic() >= deadline:
                raise TimeoutError("Stick microphone stopped sending valid audio frames")
            connection = self._connection
            if connection is None or not getattr(connection, "is_open", True):
                raise OSError("Stick microphone disconnected")
            connection.timeout = min(READ_POLL_S, max(0.0, deadline - self.monotonic()))
            chunk = connection.read(FRAME_BYTES - len(pending))
            if chunk:
                pending.extend(chunk)

    def _block(self, samples: np.ndarray) -> AudioBlock:
        block = AudioBlock(self._delivered / SAMPLE_RATE, samples)
        self._delivered += BLOCK_SAMPLES
        return block

    def __iter__(self) -> Iterator[AudioBlock]:
        try:
            if not self._open():
                return
            expected = 0
            first = True
            for seq, samples in self._frames():
                # START resets the firmware to zero. A first frame in the upper
                # half is also accepted as an explicit wrap-test/late baseline.
                if first and seq >= SEQ_HALF_RANGE:
                    expected = seq
                first = False
                gap = (seq - expected) % SEQ_MODULUS
                if gap >= SEQ_HALF_RANGE:
                    self.stale_frames += 1
                    self._report("Duplicate or stale stick frame ignored",
                                 code="stick_stale_frame", severity="warning")
                    continue
                if gap:
                    self.missing_frames += gap
                    self._report(
                        f"Missing {gap} stick audio frames; inserting silence",
                        code="stick_frame_gap", severity="warning", missing_blocks=gap,
                    )
                    for _ in range(gap):
                        if self._closed.is_set():
                            return
                        yield self._block(np.zeros(BLOCK_SAMPLES, dtype=np.float32))
                if self._closed.is_set():
                    return
                yield self._block(samples)
                expected = (seq + 1) % SEQ_MODULUS
        except Exception as exc:
            if not self._closed.is_set():
                self._report(f"Stick microphone input failed: {exc}", code="stick_read_failed")
        finally:
            self.close()

    def send_status(self, status: str, text: str = "") -> bool:
        if status not in {"NO_SIGNS", "POSSIBLE", "CONFIRMED"}:
            raise ValueError(f"Unknown stick status: {status}")
        text = " ".join(str(text).split()).encode("ascii", errors="replace").decode("ascii")
        try:
            if not self._streaming:
                return False
            return self._write(f"STATUS {status} {text[:MAX_STATUS_TEXT]}")
        except Exception as exc:
            if not self._closed.is_set():
                self._report(f"Stick status write failed: {exc}", code="stick_status_failed")
            return False

    def close(self) -> None:
        self._closed.set()
        errors = []
        with self._write_lock:
            connection, self._connection = self._connection, None
            self._streaming = False
            if connection is not None:
                try:
                    if getattr(connection, "is_open", True):
                        if connection.write(b"STOP\n") != len(b"STOP\n"):
                            raise OSError("Incomplete STOP command write")
                except Exception as exc:
                    errors.append(exc)
                try:
                    connection.close()
                except Exception as exc:
                    errors.append(exc)
            if self._claimed:
                release_port(self._claim_key)
                self._claimed = False
        for exc in errors:
            self._report(f"Stick microphone shutdown failed: {exc}", code="stick_close_failed")


class StickStatusSink:
    """Keep only the newest LCD update; event publication never performs I/O."""

    def __init__(self, source: StickMicSource) -> None:
        self.source = source
        self._status = "NO_SIGNS"
        self._odds = ""
        self._answers: dict = {}
        self._queue: queue.Queue[tuple[str, str]] = queue.Queue(maxsize=1)
        self._closed = threading.Event()
        self._writer = threading.Thread(target=self._run, name="stick-status", daemon=True)
        self._writer.start()

    def __call__(self, event) -> None:
        if self._closed.is_set() or event.type not in {"state", "round_result", "answer"}:
            return
        data = event.data
        if data.get("status") in {"NO_SIGNS", "POSSIBLE", "CONFIRMED"}:
            self._status = data["status"]
        if event.type == "state":
            self._answers = dict(data.get("answers", self._answers))
            self._odds = str(data.get("odds", self._odds))
        elif event.type == "round_result":
            self._odds = str(data.get("odds", self._odds))
        elif data.get("question"):
            self._answers[data["question"]] = data.get("value")
        parts = []
        if self._odds and self._odds != "1 in 1":
            parts.append(self._odds)
        people = self._answers.get("q_people")
        if people is not None:
            parts.append(f"{people} {'person' if people == 1 else 'people'}")
        move = self._answers.get("q_move")
        if move is not None:
            parts.append({"yes": "can move", "no": "cannot move"}.get(move, f"move: {move}"))
        hurt = self._answers.get("q_hurt")
        if hurt is not None:
            parts.append(f"hurt: {hurt}")
        item = (self._status, " | ".join(parts)[:MAX_STATUS_TEXT])
        try:
            self._queue.put_nowait(item)
        except queue.Full:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait(item)
            except queue.Full:
                pass

    def _run(self) -> None:
        while not self._closed.is_set():
            try:
                item = self._queue.get(timeout=READ_POLL_S)
            except queue.Empty:
                continue
            while not self._closed.is_set():
                try:
                    newer = self._queue.get_nowait()
                except queue.Empty:
                    break
                item = newer
            try:
                sent = self.source.send_status(*item)
            except Exception:
                log.exception("Stick LCD status update failed")
                sent = True
            if sent is False:
                # Opening may still be in progress. Retry unless a newer state
                # already occupies the one-item mailbox.
                try:
                    self._queue.put_nowait(item)
                except queue.Full:
                    pass
                self._closed.wait(READ_POLL_S)

    def close(self) -> None:
        self._closed.set()
        if threading.current_thread() is not self._writer:
            self._writer.join(timeout=2 * WRITE_TIMEOUT_S)


def find_stick_ports(*, bus=None, serial_factory=None, port_lister=None, monotonic=None) -> list[str]:
    """Recover abandoned streams, then identify idle sticks with a fast PING."""
    monotonic = monotonic or time.monotonic
    try:
        ports = (port_lister() if port_lister is not None else
                 importlib.import_module("serial.tools.list_ports").comports())
    except Exception as exc:
        message = f"Could not list stick microphone ports: {exc}"
        if bus is not None:
            bus.publish("error", 0.0, source="stickmic", code="stick_scan_failed",
                        severity="warning", message=message)
        else:
            log.warning("%s", message)
        return []
    result = []
    for entry in sorted(ports, key=lambda item: str(getattr(item, "device", item))):
        port = str(getattr(entry, "device", entry))
        claim_key = canonical_port(port)
        if getattr(entry, "is_open", False) or not claim_port(claim_key):
            continue
        connection = None
        try:
            connection = new_serial_connection(
                port, BAUD_RATE, READ_POLL_S, write_timeout=READ_POLL_S,
                serial_factory=serial_factory,
            )
            deadline = monotonic() + PROBE_TIMEOUT_S
            if connection.write(b"STOP\n") != len(b"STOP\n"):
                continue
            if connection.write(b"PING\n") != len(b"PING\n"):
                continue
            while monotonic() < deadline:
                line = _read_line(connection, deadline, monotonic)
                if _is_stick_pong(line):
                    result.append(port)
                    break
                if line is not None and line.endswith("PONG stickmic v1"):
                    if connection.write(b"PING\n") != len(b"PING\n"):
                        break
                    continue
                if line is None or line.startswith("PONG"):
                    break
        except Exception as exc:
            log.debug("Stick microphone probe skipped %s: %s", port, exc)
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass
            release_port(claim_key)
    return result
