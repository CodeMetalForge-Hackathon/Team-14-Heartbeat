#!/usr/bin/env python3
"""Bench-test the Heartbeat knocker using only pyserial and the standard library.

Examples (use ``uv run --with pyserial python`` if pyserial is not installed)::

    python tools/knocker_test.py --list
    python tools/knocker_test.py --port /dev/cu.usbmodem1101 ping
    python tools/knocker_test.py --port /dev/cu.usbmodem1101 knock 3 --interval 450
    python tools/knocker_test.py --port /dev/cu.usbmodem1101 test
    python tools/knocker_test.py --port /dev/cu.usbmodem1101 cal 60 95 70
    python tools/knocker_test.py --self-test

Opening a port can reset the board. No command is sent until its READY line has
been received. The self-test never imports pyserial or opens a port.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import re
import sys
import time
from typing import Any


# Protocol limits and P02-required defaults; keep aligned with the firmware.
BAUD_RATE = 115200
KNOCK_COUNT_RANGE = (1, 9)
INTERVAL_MS_RANGE = (250, 2000)
ANGLE_RANGE = (0, 180)
HOLD_MS_RANGE = (30, 200)
DEFAULT_INTERVAL_MS = 450
RESET_WAIT_S = 2.0
READY_TIMEOUT_S = 2.0
READ_TIMEOUT_S = 0.1
REPLY_MARGIN_S = 2.0
DETACH_DELAY_S = 0.150
MAX_REPLY_BYTES = 256


class KnockerError(Exception):
    """A timeout, protocol mismatch or firmware error safe to show to a human."""


@dataclass(frozen=True)
class Reply:
    kind: str
    values: tuple[int, ...] = ()
    reason: str = ""


@dataclass(frozen=True)
class Request:
    text: str
    expected: Reply
    timeout_s: float


def _number(token: str, bounds: tuple[int, int]) -> int:
    if re.fullmatch(r"[0-9]+", token) is None:
        raise KnockerError(f"invalid integer in reply: {token!r}")
    # Bound the token length before int() so corrupt serial data stays cheap.
    if len(token) > MAX_REPLY_BYTES:
        raise KnockerError("integer in reply is too long")
    value = int(token)
    if not bounds[0] <= value <= bounds[1]:
        raise KnockerError(f"reply value {value} outside {bounds[0]}..{bounds[1]}")
    return value


def _calibration(tokens: list[str], *, named: bool) -> tuple[int, ...]:
    if len(tokens) != 3:
        raise KnockerError("calibration reply must contain rest, strike and hold")
    values = []
    for token, label, bounds in zip(
        tokens,
        ("rest", "strike", "hold"),
        (ANGLE_RANGE, ANGLE_RANGE, HOLD_MS_RANGE),
    ):
        if named:
            prefix = f"{label}="
            if not token.startswith(prefix):
                raise KnockerError(f"expected {prefix} in calibration reply")
            token = token[len(prefix) :]
        values.append(_number(token, bounds))
    return tuple(values)


def parse_reply(line: str) -> Reply:
    """Parse one complete protocol reply, rejecting malformed or extra fields."""
    text = line.strip(" \t\r\n")
    if not text or any((ord(char) < 32 and char != "\t") or ord(char) == 127 for char in text):
        raise KnockerError("empty reply or control character in reply")
    if not text.isascii():
        raise KnockerError("reply is not ASCII")
    parts = text.split()
    if parts[0] == "ERR":
        if len(parts) < 2:
            raise KnockerError("ERR reply has no reason")
        return Reply("ERR", reason=" ".join(parts[1:]))
    if parts == ["PONG", "knocker", "v1"]:
        return Reply("PONG")
    if parts[:3] == ["READY", "knocker", "v1"]:
        return Reply("READY", _calibration(parts[3:], named=True))
    if parts == ["OK", "TEST"]:
        return Reply("TEST")
    if len(parts) == 3 and parts[:2] == ["OK", "K"]:
        return Reply("K", (_number(parts[2], KNOCK_COUNT_RANGE),))
    if parts[:2] == ["OK", "CAL"]:
        # The contract leaves the CAL suffix open; accept either exact echo form.
        values = parts[2:]
        return Reply("CAL", _calibration(values, named=bool(values and "=" in values[0])))
    raise KnockerError(f"unrecognized reply: {text!r}")


def validate_reply(reply: Reply, request: Request) -> None:
    if reply.kind == "ERR":
        raise KnockerError(f"firmware returned ERR {reply.reason}")
    if reply != request.expected:
        raise KnockerError(
            f"unexpected reply to {request.text!r}: "
            f"expected {request.expected}, received {reply}"
        )


def _bounded_argument(label: str, bounds: tuple[int, int]):
    def parse(text: str) -> int:
        try:
            value = int(text)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(f"{label} must be an integer") from exc
        if not bounds[0] <= value <= bounds[1]:
            raise argparse.ArgumentTypeError(
                f"{label} must be between {bounds[0]} and {bounds[1]}"
            )
        return value

    return parse


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--list", action="store_true", help="list serial ports, then exit")
    mode.add_argument("--self-test", action="store_true", help="check canned replies without hardware or pyserial")
    parser.add_argument("--port", help="serial device, for example /dev/cu.usbmodem1101")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("ping", help="check firmware identity")
    knock = commands.add_parser("knock", help="perform 1..9 strikes")
    knock.add_argument("n", type=_bounded_argument("n", KNOCK_COUNT_RANGE))
    knock.add_argument("--interval", type=_bounded_argument("interval", INTERVAL_MS_RANGE), default=DEFAULT_INTERVAL_MS, metavar="MS")
    commands.add_parser("test", help="perform the firmware's three-strike test")
    cal = commands.add_parser("cal", help="save servo calibration")
    cal.add_argument("rest", type=_bounded_argument("rest", ANGLE_RANGE))
    cal.add_argument("strike", type=_bounded_argument("strike", ANGLE_RANGE))
    cal.add_argument("hold", type=_bounded_argument("hold", HOLD_MS_RANGE))
    return parser


def make_request(args: argparse.Namespace) -> Request:
    if args.command == "ping":
        return Request("PING", Reply("PONG"), REPLY_MARGIN_S)
    if args.command == "knock":
        timeout = args.n * args.interval / 1000.0 + DETACH_DELAY_S + REPLY_MARGIN_S
        return Request(f"K {args.n} {args.interval}", Reply("K", (args.n,)), timeout)
    if args.command == "test":
        # Allow even the longest legal pattern, without coupling to TEST's preset.
        timeout = KNOCK_COUNT_RANGE[1] * INTERVAL_MS_RANGE[1] / 1000.0
        return Request("TEST", Reply("TEST"), timeout + DETACH_DELAY_S + REPLY_MARGIN_S)
    if args.command == "cal":
        values = (args.rest, args.strike, args.hold)
        return Request("CAL " + " ".join(map(str, values)), Reply("CAL", values), REPLY_MARGIN_S)
    raise KnockerError("a command is required")


def _read_line(port: Any, deadline: float) -> str | None:
    line = bytearray()
    while time.monotonic() < deadline:
        byte = port.read(1)
        if not byte:
            continue
        if byte in (b"\r", b"\n"):
            if line:
                return line.decode("ascii", errors="replace")
            continue
        line.extend(byte)
        if len(line) > MAX_REPLY_BYTES:
            raise KnockerError("serial reply exceeds maximum line length")
    if line:
        raise KnockerError(f"timeout with incomplete reply: {line.decode('ascii', errors='replace')!r}")
    return None


def _print_reply(line: str, started: float, phase: str) -> None:
    elapsed_ms = (time.monotonic() - started) * 1000.0
    print(f"< {line} ({elapsed_ms:.1f} ms {phase})", flush=True)


def run_command(serial_module: Any, port_name: str, request: Request) -> None:
    with serial_module.Serial(
        port_name, baudrate=BAUD_RATE, timeout=READ_TIMEOUT_S, write_timeout=REPLY_MARGIN_S
    ) as port:
        opened = time.monotonic()
        time.sleep(RESET_WAIT_S)
        deadline = time.monotonic() + READY_TIMEOUT_S
        while True:
            line = _read_line(port, deadline)
            if line is None:
                raise KnockerError("timeout waiting for READY after board reset; no command sent")
            _print_reply(line, opened, "since open")
            try:
                reply = parse_reply(line)
            except KnockerError:
                # ESP32 boot diagnostics may precede the protocol's READY line.
                continue
            if reply.kind == "ERR":
                raise KnockerError(f"firmware returned ERR {reply.reason} during startup")
            if reply.kind == "READY":
                break

        print(f"> {request.text}", flush=True)
        sent = time.monotonic()
        payload = (request.text + "\n").encode("ascii")
        if port.write(payload) != len(payload):
            raise KnockerError("serial write was incomplete")
        line = _read_line(port, sent + request.timeout_s)
        if line is None:
            raise KnockerError(f"timeout waiting for reply to {request.text!r} ({request.timeout_s:.2f} s)")
        _print_reply(line, sent, "RTT")
        validate_reply(parse_reply(line), request)


def self_test() -> int:
    valid = [
        ("PONG knocker v1", Reply("PONG")),
        (" \tPONG knocker v1\r\n", Reply("PONG")),
        ("READY knocker v1 rest=60 strike=95 hold=70", Reply("READY", (60, 95, 70))),
        ("READY knocker v1 rest=0 strike=180 hold=200", Reply("READY", (0, 180, 200))),
        ("OK K 1", Reply("K", (1,))),
        ("OK K 9\r\n", Reply("K", (9,))),
        ("OK TEST", Reply("TEST")),
        ("OK CAL 60 95 70", Reply("CAL", (60, 95, 70))),
        ("OK CAL rest=60 strike=95 hold=70", Reply("CAL", (60, 95, 70))),
        ("ERR range", Reply("ERR", reason="range")),
        ("ERR bad command", Reply("ERR", reason="bad command")),
    ]
    invalid = [
        "", " \r\n", "ERR", "PONG", "PONG knocker v2", "PONG knocker v1 extra",
        "PONG\nknocker v1", "PONG knocker v1\x00", "ERR range\x7f", "PONG knocker v1\N{SNOWMAN}",
        "READY knocker v1", "READY knocker v1 rest=60 strike=95 hold=70 extra",
        "READY knocker v1 strike=95 rest=60 hold=70",
        "READY knocker v1 rest=181 strike=95 hold=70",
        "OK K", "OK K 0", "OK K 10", "OK K -1", "OK K 1.0", "OK K 1 extra",
        "OK TEST extra", "OK CAL", "OK CAL 60 95", "OK CAL 60 95 29",
        "OK CAL 60 95 201", "OK CAL 60 181 70", "OK CAL +60 95 70",
        "OK CAL rest=60 95 hold=70", "unknown",
    ]
    count = 0
    for text, expected in valid:
        actual = parse_reply(text)
        if actual != expected:
            raise AssertionError(f"{text!r}: expected {expected}, received {actual}")
        count += 1
    for text in invalid:
        try:
            parse_reply(text)
        except KnockerError:
            count += 1
        else:
            raise AssertionError(f"accepted malformed reply: {text!r}")
    parser = build_parser()
    commands = [
        (["ping"], "PONG knocker v1"),
        (["knock", "9", "--interval", "2000"], "OK K 9"),
        (["test"], "OK TEST"),
        (["cal", "60", "95", "70"], "OK CAL 60 95 70"),
    ]
    for argv, text in commands:
        request = make_request(parser.parse_args(argv))
        validate_reply(parse_reply(text), request)
        for wrong in ("ERR range", "OK K 2", "READY knocker v1 rest=60 strike=95 hold=70"):
            try:
                validate_reply(parse_reply(wrong), request)
            except KnockerError:
                count += 1
            else:
                raise AssertionError(f"accepted mismatched reply {wrong!r} for {request.text}")
        count += 1
    maximum = make_request(parser.parse_args(["knock", "9", "--interval", "2000"]))
    if maximum.timeout_s <= 18.15:
        raise AssertionError("maximum knock pattern has no response timeout margin")
    print(f"Self-test passed ({count + 1} checks; no hardware or pyserial needed).")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.list or args.self_test:
        if args.port or args.command:
            parser.error("--list and --self-test cannot be combined with --port or a command")
    elif not args.port or not args.command:
        parser.error("provide --port and a command, or use --list or --self-test")
    if args.self_test:
        return self_test()
    try:
        import serial
        from serial.tools import list_ports
    except ImportError:
        print("error: pyserial is required; run uv run --with pyserial python tools/knocker_test.py ...", file=sys.stderr)
        return 1
    try:
        if args.list:
            ports = sorted(list_ports.comports(), key=lambda port: port.device)
            for port in ports:
                print(f"{port.device}\t{port.description}\t{port.hwid}")
            if not ports:
                print("No serial ports found.")
        else:
            run_command(serial, args.port, make_request(args))
    except (serial.SerialException, OSError, KnockerError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("error: interrupted", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
