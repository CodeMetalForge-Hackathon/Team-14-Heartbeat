"""Small command-line tools for detecting and collecting contact-mic audio."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import re
import sys
import time

import numpy as np
import soundfile as sf

from .audio import MicSource, WavSource, list_devices
from .classify import make_classifier
from .config import load_profile
from .events import EventBus
from .onset import OnsetDetector
from .runner import Pipeline
from .synth import scene


def _positive_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be a finite positive number")
    return number


def _device(value: str):
    try:
        return int(value)
    except ValueError:
        return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hb", description="Heartbeat: listen for people answering through debris."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("devices", help="list audio devices and their defaults")
    listen = commands.add_parser("listen", help="print detected sounds")
    source = listen.add_mutually_exclusive_group()
    source.add_argument("--device", type=_device, help="input device index or name")
    source.add_argument("--wav", type=Path, help="process a WAV without a microphone")
    listen.add_argument("--profile", default="rubble")
    listen.add_argument("--realtime", action="store_true", help="pace WAV replay")
    record = commands.add_parser("record", help="record labeled 16 kHz PCM audio")
    record.add_argument("--label", required=True, choices=["knock", "other", "bg", "self"])
    record.add_argument("--surface", required=True)
    record.add_argument("--desc", required=True)
    record.add_argument("--seconds", type=_positive_float, help="otherwise stop with Ctrl-C")
    record.add_argument("--device", type=_device)
    record.add_argument("--profile", default="rubble")
    synth = commands.add_parser("synth", help="write seeded test audio and truth JSON")
    synth.add_argument("--out", type=Path, required=True)
    synth.add_argument("--seconds", type=_positive_float)
    synth.add_argument("--seed", type=int)
    synth.add_argument("--profile", default="rubble")
    return parser


def _print_error(event) -> None:
    if event.type == "error":
        severity = event.data.get("severity", "error")
        print(f"{severity}: {event.data.get('message', event.data)}", file=sys.stderr)


def _bar(dbfs: float, cfg) -> str:
    fraction = (dbfs - cfg.meter_min_dbfs) / (cfg.meter_max_dbfs - cfg.meter_min_dbfs)
    filled = int(round(max(0.0, min(1.0, fraction)) * cfg.meter_width))
    return "[" + "#" * filled + " " * (cfg.meter_width - filled) + "]"


class _ListenConsole:
    def __init__(self, cfg):
        self.cfg = cfg
        self.next_level = 0

    def __call__(self, event) -> None:
        data = event.data
        if event.type == "sound":
            label = "GATED" if data["gated"] else ("KNOCK" if data["is_knock"] else "REJECTED")
            line = (
                f"{event.t:8.2f}s  {label:8s} snr {data['snr_db']:.1f} dB  "
                f"peak {data['peak']:.2f}  ({data['classifier']})"
            )
            if (data["gated"] or not data["is_knock"]) and sys.stdout.isatty():
                line = f"\033[2m{line}\033[0m"
            print(line, flush=True)
        elif event.type == "level" and event.t >= self.next_level * self.cfg.console_level_s:
            print(
                f"{event.t:8.2f}s  {_bar(data['dbfs'], self.cfg)} "
                f"{data['dbfs']:6.1f} dBFS  floor {data['floor_db']:.1f} dBFS",
                flush=True,
            )
            while self.next_level * self.cfg.console_level_s <= event.t:
                self.next_level += 1


def _devices(bus) -> int:
    failed = []
    bus.subscribe(lambda event: failed.append(event) if event.type == "error" else None)
    devices = list_devices(bus=bus)
    if not devices:
        print("No audio devices found.")
        return 1 if failed else 0
    print("  ID  input output  default  name")
    for device in devices:
        defaults = []
        if device.get("is_default_input", device.get("default", False)):
            defaults.append("in")
        if device.get("is_default_output", False):
            defaults.append("out")
        print(
            f"{device['index']:4d}  {device['max_input_channels']:5d} "
            f"{device['max_output_channels']:6d}  {','.join(defaults) or '-':7s}  "
            f"{device['name']}"
        )
    return 0


def _listen(args, profile, bus) -> int:
    cfg = profile.audio
    bus.subscribe(_ListenConsole(cfg))
    source = (
        WavSource(args.wav, realtime=args.realtime, block=cfg.block)
        if args.wav is not None
        else MicSource(device=args.device, block=cfg.block, bus=bus, cfg=cfg)
    )
    pipeline = Pipeline(
        source, OnsetDetector(profile.onset), make_classifier(profile, bus=bus), bus, profile
    )
    failed = []
    bus.subscribe(
        lambda event: failed.append(event)
        if event.type == "error" and event.data.get("severity") != "warning" else None
    )
    pipeline.run()
    return 1 if failed else 0


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "unspecified"


def _reserve_recording(label: str, surface: str, desc: str):
    directory = Path("data/raw")
    directory.mkdir(parents=True, exist_ok=True)
    stem = f"{label}__{_slug(surface)}__{_slug(desc)}"
    number = 1
    while True:
        path = directory / f"{stem}__{number:02d}.wav"
        try:
            return path, path.open("x+b")
        except FileExistsError:
            number += 1


def _countdown(seconds: float) -> None:
    remaining = seconds
    while remaining > 0:
        print(f"Recording starts in {math.ceil(remaining)}…", flush=True)
        delay = min(1.0, remaining)
        time.sleep(delay)
        remaining -= delay


def _record(args, profile, bus) -> int:
    cfg = profile.audio
    _countdown(cfg.record_countdown_s)
    reported_errors = []
    bus.subscribe(
        lambda event: reported_errors.append(event)
        if event.type == "error" and event.data.get("severity") != "warning" else None
    )
    source = MicSource(device=args.device, block=cfg.block, bus=bus, cfg=cfg)
    path, handle = _reserve_recording(args.label, args.surface, args.desc)
    target = None if args.seconds is None else round(args.seconds * cfg.sample_rate)
    frames = 0
    peak = 0.0
    next_meter = 0
    failed = False
    print(f"Recording {path}; press Ctrl-C to stop.", flush=True)
    bus.publish("session_start", 0.0, command="record", label=args.label)
    try:
        with handle:
            with sf.SoundFile(
                handle, mode="w", samplerate=cfg.sample_rate, channels=1,
                format="WAV", subtype="PCM_16",
            ) as output:
                try:
                    for block in source:
                        samples = block.x
                        if target is not None:
                            samples = samples[:max(0, target - frames)]
                        if len(samples):
                            current_peak = float(np.max(np.abs(samples)))
                            peak = max(peak, current_peak)
                            output.write(samples)
                            frames += len(samples)
                            stream_t = frames / cfg.sample_rate
                            if stream_t >= next_meter * cfg.console_level_s:
                                dbfs = 20 * math.log10(current_peak) if current_peak > 0 else -math.inf
                                print(
                                    f"\r{stream_t:7.2f}s  {_bar(dbfs, cfg)} peak {current_peak:.3f}",
                                    end="", flush=True,
                                )
                                while next_meter * cfg.console_level_s <= stream_t:
                                    next_meter += 1
                        if target is not None and frames >= target:
                            break
                except KeyboardInterrupt:
                    pass
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        failed = True
        bus.publish("error", frames / cfg.sample_rate, where="record", message=str(exc))
    finally:
        try:
            source.close()
        except Exception as exc:
            failed = True
            bus.publish("error", frames / cfg.sample_rate, where="source.close", message=str(exc))
        bus.publish("session_end", frames / cfg.sample_rate, command="record")
        print()
    if not frames:
        path.unlink(missing_ok=True)
        print("No audio recorded.")
        return 1
    print(f"Saved {path} ({frames / cfg.sample_rate:.2f}s, peak {peak:.3f}).")
    if peak > cfg.record_clip_peak:
        print("Warning: audio clipped; lower the input gain.")
    elif peak < cfg.record_quiet_peak:
        print("Warning: audio is very quiet; check contact and input gain.")
    return 1 if failed or reported_errors else 0


def _synth(args, profile) -> int:
    defaults = profile.audio.synth["scene"]
    duration = defaults["duration_s"] if args.seconds is None else args.seconds
    seed = defaults["seed"] if args.seed is None else args.seed
    audio, truth = scene(
        duration, bg_dbfs=defaults["bg_dbfs"], seed=seed, cfg=profile.audio.synth
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    sf.write(args.out, audio, profile.audio.sample_rate, subtype="PCM_16")
    truth_path = Path(str(args.out) + ".json")
    truth_path.write_text(json.dumps(truth, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {args.out} and {truth_path} ({len(truth)} events, seed {seed}).")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    bus = EventBus()
    bus.subscribe(_print_error)
    try:
        if args.command == "devices":
            return _devices(bus)
        profile = load_profile(args.profile)
        if args.command == "listen":
            return _listen(args, profile, bus)
        if args.command == "record":
            return _record(args, profile, bus)
        return _synth(args, profile)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        bus.publish("error", 0.0, where=args.command, message=str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
