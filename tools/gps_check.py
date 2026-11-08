#!/usr/bin/env python3
"""Bounded, receive-only GNSS check. Reports fix status without coordinates."""

import argparse
from collections import Counter
import json
import math
from pathlib import Path
import time

import serial
from serial.tools import list_ports
import yaml


def parse_nmea(raw):
    line = raw.strip()
    if not line.startswith(b"$"):
        raise ValueError("not an NMEA sentence")
    body, checksum = line[1:].rsplit(b"*", 1)
    if len(checksum) != 2:
        raise ValueError("invalid checksum length")
    actual = 0
    for byte in body:
        actual ^= byte
    if actual != int(checksum, 16):
        raise ValueError("checksum mismatch")
    return body.decode("ascii").split(",")


def valid_coordinate(value, direction, *, latitude):
    if direction not in ({"N", "S"} if latitude else {"E", "W"}):
        return False
    try:
        coordinate = float(value)
        degrees, minutes = divmod(coordinate, 100)
        limit = 90 if latitude else 180
        return (math.isfinite(coordinate) and 0 <= degrees <= limit
                and 0 <= minutes < 60 and (degrees < limit or minutes == 0))
    except ValueError:
        return False


class GPSStatus:
    def __init__(self):
        self.messages = Counter()
        self.invalid = 0
        self.gga = self.rmc = None
        self.visible = {}
        self.gga_fix_samples = 0

    def feed(self, raw, now):
        try:
            fields = parse_nmea(raw)
            kind = fields[0][-3:]
            if kind == "GGA":
                quality, satellites = int(fields[6] or 0), int(fields[7] or 0)
                hdop = float(fields[8]) if fields[8] else None
                position = (valid_coordinate(fields[2], fields[3], latitude=True)
                            and valid_coordinate(fields[4], fields[5], latitude=False))
                if quality < 0 or satellites < 0 or hdop is not None and (not math.isfinite(hdop) or hdop < 0):
                    raise ValueError("invalid GGA values")
                fix = quality in {1, 2, 3, 4, 5} and satellites > 0 and position
                self.gga = dict(at=now, quality=quality, satellites=satellites, hdop=hdop, fix=fix)
                self.gga_fix_samples += int(fix)
            elif kind == "RMC":
                if fields[2] not in {"A", "V"}:
                    raise ValueError("invalid RMC status")
                self.rmc = (now, fields[2])
            elif kind == "GSV":
                count = int(fields[3] or 0)
                if count < 0:
                    raise ValueError("invalid satellite count")
                talker = fields[0][:2]
                self.visible[talker] = max(count, self.visible.get(talker, 0))
            self.messages[kind] += 1
        except (ValueError, UnicodeError, IndexError):
            self.invalid += 1

    def summary(self, now, stale_s):
        gga = self.gga
        rmc_valid = self.rmc is None or now - self.rmc[0] > stale_s or self.rmc[1] == "A"
        return dict(
            valid_sentences=sum(self.messages.values()), sentence_counts=dict(self.messages),
            invalid_sentences=self.invalid,
            current_fix=bool(gga and gga["fix"] and now - gga["at"] <= stale_s and rmc_valid),
            fix_quality=gga["quality"] if gga else None,
            satellites_used=gga["satellites"] if gga else None,
            hdop=gga["hdop"] if gga else None,
            navigation_age_s=round(now - gga["at"], 2) if gga else None,
            rmc_status=self.rmc[1] if self.rmc else None,
            max_visible_by_talker=dict(self.visible), gga_fix_samples=self.gga_fix_samples,
        )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", help="explicit GNSS port; otherwise select one u-blox USB receiver")
    parser.add_argument("--seconds", type=float)
    parser.add_argument("--config", type=Path,
                        default=Path(__file__).resolve().parents[1] / "profiles/gps-check.yaml")
    args = parser.parse_args(argv)
    cfg = yaml.safe_load(args.config.read_text())
    expected = {"duration_s", "baudrate", "read_timeout_s", "report_interval_s", "stale_s", "max_sentence_bytes"}
    if not isinstance(cfg, dict) or cfg.keys() != expected:
        raise ValueError("invalid GPS check configuration")
    if args.seconds is not None:
        cfg["duration_s"] = args.seconds
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0
           for v in cfg.values()):
        raise ValueError("GPS check settings must be positive finite numbers")
    if any(not isinstance(cfg[key], int) for key in ("baudrate", "max_sentence_bytes")):
        raise ValueError("baudrate and max_sentence_bytes must be integers")
    port = args.port
    if port is None:
        # USB vendor identity only: never probe arbitrary rover/microphone ports.
        candidates = [p.device for p in list_ports.comports() if p.vid == 0x1546]
        if len(candidates) != 1:
            raise ValueError(f"Expected one u-blox USB receiver; found {candidates}. Use --port.")
        port = candidates[0]
    status = GPSStatus()
    start = time.monotonic()
    next_report = start + cfg["report_interval_s"]
    print(f"Reading GNSS from {port} for {cfg['duration_s']:g}s; no receiver commands sent.", flush=True)
    with serial.Serial(port, cfg["baudrate"], timeout=cfg["read_timeout_s"], exclusive=True) as gps:
        gps.reset_input_buffer()
        while time.monotonic() - start < cfg["duration_s"]:
            raw = gps.readline(cfg["max_sentence_bytes"])
            now = time.monotonic()
            if raw:
                status.feed(raw, now)
            if now >= next_report:
                print(json.dumps(status.summary(now, cfg["stale_s"])), flush=True)
                next_report = now + cfg["report_interval_s"]
    result = status.summary(time.monotonic(), cfg["stale_s"])
    print(json.dumps(dict(result, port=port, duration_s=round(time.monotonic()-start, 2))), flush=True)
    if not result["valid_sentences"]:
        return 1
    return 0 if result["current_fix"] else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except (OSError, ValueError, yaml.YAMLError, serial.SerialException) as exc:
        print(f"GPS check failed: {exc}")
        raise SystemExit(1)
