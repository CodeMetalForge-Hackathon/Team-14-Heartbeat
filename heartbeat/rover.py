"""Bounded rover commands with an independent STOP path and stream-time logs.

Constructors never touch serial devices. Transport waits use real elapsed time;
the injected ``clock`` supplies all mission/event timestamps. Untagged serial
replies are routed by command and exact arguments. An interrupted transaction
retires its connection so its delayed completion cannot finish a new move.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
import math
import threading
from typing import Callable

from .config import load_profile
from .stickmic import canonical_port, claim_port, new_serial_connection, release_port


MAX_REPLY_BYTES = 256  # Fixed framing bound, not a motion tuning parameter.


def _finite(value, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    return float(value)


def parse_reply(line: str | bytes) -> dict:
    """Parse one complete v1 reply; reject malformed lines and other devices."""
    if isinstance(line, bytes):
        line = line.decode("ascii")
    if not isinstance(line, str) or len(line) > MAX_REPLY_BYTES:
        raise ValueError("Invalid rover reply")
    raw = line.rstrip("\r\n")
    if not raw or any(ord(c) < 32 or ord(c) > 126 for c in raw):
        raise ValueError("Invalid rover reply text")
    words = raw.split()
    if not words:
        raise ValueError("Empty rover reply")
    kind = words[0]
    if kind in {"READY", "PONG"}:
        if words != [kind, "rover", "v1"]:
            raise ValueError(f"Wrong rover identity: {raw}")
        return {"kind": kind, "device": "rover", "version": "v1", "raw": raw}
    if kind == "ERR" and len(words) > 1:
        return {"kind": "ERR", "reason": " ".join(words[1:]), "raw": raw}
    if kind == "OK" and len(words) >= 2:
        command = words[1]
        counts = {"STOP": 0, "DRIVE": 1, "TURN": 1, "LED": 3, "CAL": 2}
        if command not in counts or len(words) != counts[command] + 2:
            raise ValueError(f"Malformed rover acknowledgement: {raw}")
        try:
            args = [float(word) for word in words[2:]]
        except ValueError as exc:
            raise ValueError(f"Malformed rover acknowledgement: {raw}") from exc
        if not all(math.isfinite(arg) for arg in args):
            raise ValueError("Nonfinite rover acknowledgement")
        if command == "LED" and any(not arg.is_integer() or not 0 <= arg <= 255 for arg in args):
            raise ValueError("Malformed rover LED acknowledgement")
        return {"kind": "OK", "command": command, "args": args, "raw": raw}
    if kind == "STATUS" and len(words) == 4:
        try:
            values = dict(word.split("=", 1) for word in words[1:])
            if values.keys() != {"batt", "moving", "enc"} or values["moving"] not in {"0", "1"}:
                raise ValueError("Invalid status fields")
            battery = float(values["batt"])
            enc = [float(value) for value in values["enc"].split(",")]
            if not math.isfinite(battery) or len(enc) != 2 or not all(map(math.isfinite, enc)):
                raise ValueError("Invalid status numbers")
        except (ValueError, TypeError) as exc:
            raise ValueError(f"Malformed rover status: {raw}") from exc
        return {"kind": "STATUS", "batt": battery, "moving": values["moving"] == "1",
                "enc": enc, "raw": raw}
    raise ValueError(f"Unrecognized rover reply: {raw}")


@dataclass(frozen=True)
class RoverConfig:
    baud: int
    read_timeout_s: float
    write_timeout_s: float
    connect_timeout_s: float
    command_timeout_s: float
    move_timeout_s: float
    heartbeat_s: float
    max_drive_cm: float
    max_turn_deg: float
    max_speed_pct: float
    default_speed_pct: float
    calibration_min: float
    calibration_max: float
    fake_speed_cm_s: float
    fake_turn_deg_s: float


def _config(cfg) -> RoverConfig:
    if isinstance(cfg, RoverConfig):
        result = cfg
    else:
        defaults = load_profile().rover
        supplied = getattr(cfg, "rover", cfg) if cfg is not None else {}
        if not isinstance(supplied, dict):
            raise ValueError("rover configuration must be a mapping")
        values = {**defaults, **supplied}
        result = RoverConfig(**{item.name: values[item.name] for item in fields(RoverConfig)})
    for item in fields(result):
        if _finite(getattr(result, item.name), item.name) <= 0:
            raise ValueError(f"rover.{item.name} must be positive")
    if result.baud != 115200:
        raise ValueError("rover v1 requires 115200 baud")
    for name, bound in (("max_drive_cm", 100), ("max_turn_deg", 180), ("max_speed_pct", 60),
                        ("heartbeat_s", 0.5), ("calibration_max", 100000)):
        if getattr(result, name) > bound:
            raise ValueError(f"rover.{name} cannot exceed firmware bound {bound}")
    if result.calibration_min < 0.001 or result.calibration_min >= result.calibration_max:
        raise ValueError("Invalid rover calibration bounds")
    if not 1 <= result.default_speed_pct <= result.max_speed_pct:
        raise ValueError("Invalid rover default speed")
    return result


class _Commands:
    """Shared validation for actual and simulated transport paths."""

    def _init_commands(self, cfg, bus, clock, engine_is_listening):
        self.cfg = _config(cfg)
        self.bus = bus
        self.clock = clock if clock is not None else lambda: 0.0
        # A standalone rover has no active audio engine. Patrol injects its gate.
        self.engine_is_listening = engine_is_listening or (lambda: False)
        self.moving = False
        self.estopped = False
        self._stop_epoch = 0

    def set_motion_gate(self, callback: Callable[[], bool]) -> None:
        self.engine_is_listening = callback

    def _emit(self, kind, **data):
        if self.bus is not None:
            self.bus.publish("rover", self.clock(), kind=kind, moving=self.moving, **data)

    def _error(self, message, *, command=None):
        self._emit("error", message=str(message), command=command)
        if self.bus is not None:
            self.bus.publish("error", self.clock(), source="rover", message=str(message), command=command)

    def _validate(self, command: str, args: tuple) -> tuple:
        count = {"PING": 0, "STOP": 0, "HB": 0, "STATUS": 0,
                 "DRIVE": 2, "TURN": 2, "LED": 3, "CAL": 2}
        if command not in count or len(args) != count[command]:
            raise ValueError("Invalid rover command")
        numbers = tuple(_finite(value, command) for value in args)
        if command in {"DRIVE", "TURN"}:
            bound = self.cfg.max_drive_cm if command == "DRIVE" else self.cfg.max_turn_deg
            if not -bound <= numbers[0] <= bound:
                raise ValueError(f"{command} exceeds host distance/angle limit")
            if not 1 <= numbers[1] <= self.cfg.max_speed_pct:
                raise ValueError("Rover speed must be between 1 and the profile limit")
            if self.estopped:
                raise ValueError("Rover E-STOP is latched; explicit reset_estop is required")
            if self.engine_is_listening():
                raise ValueError("Rover movement refused while sentinel is listening")
        if command == "LED" and any(not value.is_integer() or not 0 <= value <= 255 for value in numbers):
            raise ValueError("Rover LED channels must be integers from 0 to 255")
        if command == "CAL" and any(not self.cfg.calibration_min <= v <= self.cfg.calibration_max for v in numbers):
            raise ValueError("Rover calibration exceeds profile limits")
        return numbers

    def drive(self, cm, speed_pct=None):
        return self._command("DRIVE", (cm, self.cfg.default_speed_pct if speed_pct is None else speed_pct))

    def turn(self, deg, speed_pct=None):
        return self._command("TURN", (deg, self.cfg.default_speed_pct if speed_pct is None else speed_pct))

    def ping(self):
        return self._command("PING", ())

    def status(self):
        return self._command("STATUS", ())

    def led(self, r, g, b):
        return self._command("LED", (r, g, b))

    def calibrate(self, deg_per_cm, deg_per_turn_deg):
        return self._command("CAL", (deg_per_cm, deg_per_turn_deg))

    def estop(self):
        # Latch before the write so no other thread can race in a new movement.
        self.estopped = True
        return self.stop()

    emergency_stop = estop

    def reset_estop(self):
        """Explicit operator action; never moves the rover."""
        self.estopped = False
        self._emit("estop_reset")


@dataclass
class _Pending:
    command: str
    args: tuple
    done: threading.Event = field(default_factory=threading.Event)
    reply: dict | None = None
    error: str | None = None
    aborted: bool = False

    def matches(self, reply):
        if self.command == "PING":
            return reply["kind"] == "PONG"
        if self.command == "STATUS":
            return reply["kind"] == "STATUS"
        expected = self.args[:1] if self.command in {"DRIVE", "TURN"} else self.args
        return (reply["kind"] == "OK" and reply["command"] == self.command
                and len(reply["args"]) == len(expected)
                and all(math.isclose(a, b, rel_tol=1e-5, abs_tol=1e-5)
                        for a, b in zip(reply["args"], expected)))


class RoverLink(_Commands):
    """Explicitly commanded, exclusive serial link; STOP bypasses move waits.

    ``serial_factory`` follows P07's no-argument constructor contract. The real
    port is opened with DTR/RTS inactive and OS exclusivity before any command.
    Errors are events and false results, so a failed board cannot crash patrol.
    """

    def __init__(self, port, *, cfg=None, bus=None, clock=None, engine_is_listening=None,
                 serial_factory=None, exclude_ports=None):
        self._init_commands(cfg, bus, clock, engine_is_listening)
        self.port = str(port)
        self.serial_factory = serial_factory
        self.exclude_ports = {canonical_port(p) for p in (exclude_ports or ())}
        self._connection = None
        self._claim_key = None
        self._reader = None
        self._fault = None
        self._pending = None
        self._stop_pending = None
        self._lock = threading.RLock()
        self._command_lock = threading.Lock()
        self._connect_lock = threading.Lock()
        self._write_lock = threading.RLock()
        self._stop_lock = threading.Lock()
        self._stopping = threading.Event()
        self._heartbeat_cancel = threading.Event()

    def _write(self, connection, command, args=(), *, epoch=None):
        line = " ".join([command, *(format(value, ".10g") for value in args)])
        payload = (line + "\n").encode("ascii")
        with self._write_lock:
            with self._lock:
                self._validate(command, args)
                if epoch is not None and epoch != self._stop_epoch:
                    raise ValueError("Rover command cancelled by STOP")
                if command in {"DRIVE", "TURN"} and self._stopping.is_set():
                    raise ValueError("Rover movement cancelled by STOP")
                if command == "HB" and not self.moving:
                    return
                if connection is not self._connection:
                    raise OSError("Rover connection was retired")
                if connection.write(payload) != len(payload):
                    raise OSError("Incomplete rover serial write")
        self._emit("command", command=command, args=list(args), line=line)

    def _read_loop(self, connection):
        pending = bytearray()
        try:
            while connection is self._connection:
                byte = connection.read(1)
                if not byte:
                    if not getattr(connection, "is_open", True):
                        raise OSError("Rover serial connection closed")
                    continue
                if byte in (b"\r", b"\n"):
                    if not pending:
                        continue
                    line = bytes(pending)
                    pending.clear()
                    try:
                        reply = parse_reply(line)
                    except (ValueError, UnicodeError) as exc:
                        self._fail_pending(str(exc))
                        self._error(exc)
                        continue
                    with self._lock:
                        if connection is not self._connection:
                            return
                        if reply["kind"] == "ERR":
                            self._fail_pending(reply["reason"])
                        else:
                            for waiter in (self._stop_pending, self._pending):
                                if waiter and not waiter.done.is_set() and waiter.matches(reply):
                                    waiter.reply = reply
                                    waiter.done.set()
                                    break
                    # Release transport waiters before synchronous subscribers:
                    # a command subscriber may itself be waiting for E-STOP.
                    self._emit("reply", reply=reply, line=reply["raw"])
                    if reply["kind"] == "ERR":
                        self._error(reply["reason"])
                    continue
                pending.extend(byte)
                if len(pending) > MAX_REPLY_BYTES:
                    raise OSError("Rover reply exceeded line limit")
        except Exception as exc:
            if connection is self._connection:
                self._fail_pending(str(exc))
                self._error(exc)

    def _fail_pending(self, message):
        with self._lock:
            self.moving = False
            self._fault = message
            self._heartbeat_cancel.set()
            for waiter in (self._pending, self._stop_pending):
                if waiter and not waiter.done.is_set():
                    waiter.error = message
                    waiter.done.set()

    def _perform(self, command, args, timeout, *, stopping=False, epoch=None):
        waiter = _Pending(command, args)
        slot = "_stop_pending" if stopping else "_pending"
        with self._lock:
            connection = self._connection
            setattr(self, slot, waiter)
        try:
            self._write(connection, command, args, epoch=epoch)
            if not waiter.done.wait(timeout):
                raise TimeoutError(f"Rover timed out waiting for {command}")
            if waiter.aborted:
                return None
            if waiter.error:
                raise OSError(waiter.error)
            return waiter.reply
        except (OSError, TimeoutError):
            if epoch is not None:
                self._stopping.set()
            raise
        finally:
            with self._lock:
                if getattr(self, slot) is waiter:
                    setattr(self, slot, None)

    def _connect(self):
        with self._connect_lock:
            if self._connection is not None:
                return
            key = canonical_port(self.port)
            if key in self.exclude_ports or not claim_port(key):
                raise OSError(f"Rover serial port is excluded or in use: {self.port}")
            self._claim_key = key
            try:
                connection = new_serial_connection(
                    self.port, self.cfg.baud, self.cfg.read_timeout_s,
                    write_timeout=self.cfg.write_timeout_s, serial_factory=self.serial_factory,
                )
                self._connection = connection
                self._fault = None
                # Drop old buffered completions before the identity barrier.
                reset = getattr(connection, "reset_input_buffer", None)
                if reset is not None:
                    reset()
                self._reader = threading.Thread(target=self._read_loop, args=(connection,), daemon=True,
                                                name="rover-replies")
                self._reader.start()
                self._perform("PING", (), self.cfg.connect_timeout_s)
            except Exception:
                self._disconnect()
                raise

    def _heartbeat(self, connection, cancel):
        while not cancel.wait(self.cfg.heartbeat_s):
            if not self.moving or connection is not self._connection:
                return
            try:
                self._write(connection, "HB")
            except Exception as exc:
                self._error(exc, command="HB")
                self._fail_pending(str(exc))
                return

    def _command(self, command, args):
        epoch = self._stop_epoch
        try:
            args = self._validate(command, args)
            with self._command_lock:
                if epoch != self._stop_epoch or self._stopping.is_set():
                    raise ValueError("Rover command cancelled by STOP")
                self._validate(command, args)  # Recheck gate after any queued command.
                if self._fault is not None:
                    self._disconnect()
                self._connect()
                if epoch != self._stop_epoch:
                    raise ValueError("Rover command cancelled by STOP")
                motion = command in {"DRIVE", "TURN"}
                if motion:
                    self.moving = True
                    self._heartbeat_cancel = threading.Event()
                    threading.Thread(target=self._heartbeat, args=(self._connection, self._heartbeat_cancel), daemon=True,
                                     name="rover-heartbeat").start()
                try:
                    reply = self._perform(command, args, self.cfg.move_timeout_s if motion else self.cfg.command_timeout_s,
                                          epoch=epoch)
                finally:
                    if motion:
                        self.moving = False
                        self._heartbeat_cancel.set()
                return reply if command == "STATUS" else reply is not None
        except Exception as exc:
            self._error(exc, command=command)
            # Invalid requests do not touch hardware. A transport fault or a
            # timed-out command always attempts STOP before retiring the port.
            if not isinstance(exc, ValueError):
                if self._connection is not None:
                    self.stop()
                else:
                    self._stopping.clear()
                self._disconnect()
            return None if command == "STATUS" else False

    def stop(self):
        """Send STOP without acquiring the command lock, aborting a move waiter."""
        with self._stop_lock:
            with self._lock:
                self._stopping.set()
                self._stop_epoch += 1
                self.moving = False
                self._heartbeat_cancel.set()
                interrupted = self._pending is not None
                if self._pending is not None:
                    self._pending.aborted = True
                    self._pending.done.set()
            success = False
            try:
                self._connect()
                reply = self._perform("STOP", (), self.cfg.command_timeout_s, stopping=True)
                success = reply is not None
                return success
            except Exception as exc:
                self._error(exc, command="STOP")
                return False
            finally:
                if interrupted or not success:
                    self._disconnect()
                self._stopping.clear()

    def _disconnect(self):
        with self._lock:
            connection, self._connection = self._connection, None
            key, self._claim_key = self._claim_key, None
            self.moving = False
            self._heartbeat_cancel.set()
        if connection is not None:
            try:
                connection.close()
            except Exception as exc:
                self._error(exc, command="CLOSE")
        if key is not None:
            release_port(key)

    def close(self):
        if self._connection is not None:
            self.stop()
        self._disconnect()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class FakeRover(_Commands):
    """A hardware-free rover advanced exclusively by supplied stream time.

    Motion commands return receipt immediately. ``duration`` gives the patrol's
    stream deadline; ``advance(t)`` completes it. STOP/ESTOP cancels immediately.
    Every accepted wire command is also recorded in ``trace``.
    """

    virtual = True

    def __init__(self, *, cfg=None, bus=None, clock=None, engine_is_listening=None):
        self._time = 0.0
        self._init_commands(cfg, bus, clock if clock is not None else lambda: self._time,
                            engine_is_listening)
        self.trace: list[dict] = []
        self._finish_t = None
        self._motion = None
        self._lock = threading.RLock()

    def duration(self, command, value, speed_pct=None):
        command = command.upper()
        args = self._validate(command, (value, self.cfg.default_speed_pct if speed_pct is None else speed_pct))
        rate = self.cfg.fake_speed_cm_s if command == "DRIVE" else self.cfg.fake_turn_deg_s
        return abs(args[0]) / rate * self.cfg.default_speed_pct / args[1]

    movement_duration = duration

    def _command(self, command, args):
        try:
            args = self._validate(command, args)
            with self._lock:
                if self.moving and command in {"DRIVE", "TURN", "CAL", "LED"}:
                    raise ValueError("Fake rover is busy")
                item = {"t": self.clock(), "command": command, "args": list(args)}
                self.trace.append(item)
                epoch = self._stop_epoch
                if command in {"DRIVE", "TURN"}:
                    self._finish_t = self.clock() + self.duration(command, *args)
                    self._motion = (command, args)
                    self.moving = True
                self._emit("command", command=command, args=list(args))
                if command in {"DRIVE", "TURN"}:
                    return self._stop_epoch == epoch and self.moving
                reply = {"kind": "STATUS", "batt": None, "moving": self.moving, "enc": [0, 0]} if command == "STATUS" else {"kind": "OK", "command": command, "args": list(args)}
                self._emit("reply", reply=reply)
                return reply if command == "STATUS" else True
        except (ValueError, TypeError) as exc:
            self._error(exc, command=command)
            return None if command == "STATUS" else False

    def advance(self, t):
        t = _finite(t, "stream time")
        if t < self._time:
            raise ValueError("Fake rover stream time cannot run backward")
        with self._lock:
            self._time = t
            if self._finish_t is not None and t >= self._finish_t:
                command, args = self._motion
                self.moving = False
                self._finish_t = self._motion = None
                self._emit("reply", reply={"kind": "OK", "command": command, "args": list(args[:1])})

    def stop(self):
        with self._lock:
            self._stop_epoch += 1
            self._finish_t = self._motion = None
            self.moving = False
            return self._command("STOP", ())

    def close(self):
        self.stop()
