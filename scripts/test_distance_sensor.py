#!/usr/bin/env python3
"""Standalone ultrasonic check. Run manually with the patrol pipeline stopped.

    uv run python scripts/test_distance_sensor.py
    uv run python scripts/test_distance_sensor.py --sensor-port 10 --count 20

Only STOP, connection handshakes and RANGE are sent to the rover. Camera,
microphone, dashboard and AI services are not started. Importing this file
does not open hardware.
"""

from __future__ import annotations

import argparse
import math
from pathlib import Path
import sys
import time

# Allow direct execution from the checkout as well as an installed package.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from heartbeat.config import load_profile
from heartbeat.events import EventBus
from heartbeat.rover import RoverLink


def positive_interval(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("interval must be positive and finite")
    return number


def positive_count(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("count must be a positive integer")
    return number


def main():
    parser = argparse.ArgumentParser(description="Print rover ultrasonic distance until Ctrl+C.")
    parser.add_argument("--port", default="/dev/ttyUSB0", help="rover USB serial device")
    parser.add_argument("--profile", default="pi-explore", help="profile supplying sensor and timing defaults")
    parser.add_argument("--sensor-port", type=int, choices=range(6, 11), help="numbered socket on the rover")
    parser.add_argument("--interval", type=positive_interval, help="seconds between readings")
    parser.add_argument("--count", type=positive_count, help="stop after this many readings; otherwise run until Ctrl+C")
    args = parser.parse_args()

    try:
        cfg = load_profile(args.profile).rover
        navigation = cfg["navigation"]
        sensor_port = args.sensor_port if args.sensor_port is not None else navigation["sensor_port"]
        interval = args.interval if args.interval is not None else navigation["range_retry_ms"] / 1000
        bus = EventBus()

        def report_error(event):
            if event.type == "error":
                print(f"Serial error: {event.data.get('message', 'Rover communication failed')}",
                      file=sys.stderr, flush=True)

        bus.subscribe(report_error)
        with RoverLink(args.port, cfg=cfg, bus=bus) as rover:
            if rover.stop() is not True:
                print("Could not acknowledge STOP. Stop the patrol pipeline and check the USB connection.",
                      file=sys.stderr)
                return 1
            print(f"Motors stopped | USB {args.port} | ultrasonic port {sensor_port}", flush=True)
            print("Move an object toward/away from the sensor. Ctrl+C ends the check.", flush=True)
            samples = valid = 0
            try:
                while args.count is None or samples < args.count:
                    reading = rover.range(sensor_port)
                    if reading is None:
                        print("No RANGE reply from the controller; check the serial connection.",
                              file=sys.stderr)
                        return 1
                    samples += 1
                    if reading["valid"]:
                        valid += 1
                        print(f"{samples:04d}  {reading['cm']:.2f} cm", flush=True)
                    else:
                        print(f"{samples:04d}  NO ECHO (cm={reading['cm']:.2f}, valid=0)", flush=True)
                    if args.count is None or samples < args.count:
                        time.sleep(interval)
            except KeyboardInterrupt:
                print("\nStopped.", flush=True)
            print(f"Valid echoes: {valid}/{samples}", flush=True)
            return 0 if valid else 1
    except KeyboardInterrupt:
        print("\nStopped.")
        return 0
    except (OSError, ValueError, KeyError) as exc:
        print(f"Distance check failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
