"""Small command-line tools for detecting and collecting contact-mic audio."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
from datetime import datetime
import json
import math
from pathlib import Path
import re
import sys
import threading
import time

import numpy as np
import soundfile as sf

from .audio import WavSource, list_devices
from .classify import make_classifier
from .config import load_profile
from .events import EventBus
from .onset import OnsetDetector
from .runner import Pipeline, prepare_runtime, prepare_simulation, run_simulation
from .sources import open_input
from .stickmic import StickMicSource, StickStatusSink, configure_stick, find_stick_ports
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


def _stick_mag(value: str) -> int:
    number = int(value)
    if not 1 <= number <= 255:
        raise argparse.ArgumentTypeError("must be an integer from 1 to 255")
    return number


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hb", description="Heartbeat: listen for people answering through debris."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("devices", help="list audio devices and their defaults")
    stick = commands.add_parser("stick", help="inspect the stick or persist its microphone gain")
    stick.add_argument("--port", help="stick serial port; otherwise discover it")
    stick.add_argument("--profile", default="rubble")
    stick_commands = stick.add_subparsers(dest="stick_command", required=True)
    stick_commands.add_parser("info", help="print identity and MAG when reported by firmware")
    mag = stick_commands.add_parser("mag", help="set persistent on-device microphone magnification")
    mag.add_argument("value", type=_stick_mag, metavar="1..255")
    listen = commands.add_parser("listen", help="print detected sounds")
    source = listen.add_mutually_exclusive_group()
    source.add_argument("--device", type=_device, help="input device index or name")
    source.add_argument("--wav", type=Path, help="process a WAV without a microphone")
    listen.add_argument("--profile", default="rubble")
    listen.add_argument("--realtime", action="store_true", help="pace WAV replay")
    listen.add_argument("--debug", action="store_true", help="print stick serial diagnostics and top classifier descriptions")
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
    run = commands.add_parser("run", help="run the sentinel challenge and interview")
    source = run.add_mutually_exclusive_group()
    source.add_argument("--device", type=_device)
    source.add_argument("--wav", type=Path)
    run.add_argument("--knocker", default="auto", help="auto, manual, speaker, or serial port")
    run.add_argument("--speaker-device", type=_device)
    run.add_argument("--auto", action="store_true", help="automatically challenge and interview")
    run.add_argument("--profile", default="rubble")
    sim = commands.add_parser("sim", help="simulate a survivor through the real audio pipeline")
    sim.add_argument("--people", type=int, default=2)
    sim.add_argument("--can-move", choices=["yes", "no"], default="yes")
    sim.add_argument("--hurt", choices=["yes", "no"], default="yes")
    sim.add_argument("--no-survivor", action="store_true")
    sim.add_argument("--noise", action="store_true")
    sim.add_argument("--realtime", action="store_true")
    sim.add_argument("--seed", type=int, default=1)
    sim.add_argument("--profile", default="rubble")
    for command in (run, sim):
        command.add_argument("--dashboard", action="store_true", help="serve the local command-post dashboard")
        command.add_argument("--host", help="dashboard host; defaults to the profile")
        command.add_argument("--port", type=int, help="dashboard port; defaults to the profile")
        command.add_argument("--run-dir", type=Path, help="recording directory; defaults to a new runs/timestamp")
    verify = commands.add_parser("verify", help="check event-chain and audio-clip integrity")
    verify.add_argument("run_dir", type=Path)
    replay = commands.add_parser("replay", help="compare offline detections with a recorded session")
    replay.add_argument("source", type=Path)
    replay.add_argument("--profile")
    replay.add_argument("--classifier")
    report = commands.add_parser("report", help="write a session after-action report")
    report.add_argument("run_dir", type=Path)
    rover = commands.add_parser("rover", help="control the Ranger link; a human runs motor commands")
    rover.add_argument("--port", required=True, help="rover USB serial port")
    rover.add_argument("--profile", default="rubble")
    rover_commands = rover.add_subparsers(dest="rover_command", required=True)
    for name in ("ping", "stop", "status", "calibrate"):
        rover_commands.add_parser(name)
    for name, unit in (("drive", "cm"), ("turn", "deg")):
        motion = rover_commands.add_parser(name)
        motion.add_argument(unit, type=float)
        motion.add_argument("--speed", type=float, help="motor speed percent; defaults to the profile")
    ranging = rover_commands.add_parser("range", help="read front ultrasonic distance without moving")
    ranging.add_argument("--sensor-port", type=int, choices=range(6, 11), required=True)
    led = rover_commands.add_parser("led")
    for color in ("r", "g", "b"):
        led.add_argument(color, type=int)
    patrol = commands.add_parser("patrol", help="stop, settle, and listen at each route gap")
    patrol.add_argument("--rover", help="Ranger USB serial port; unnecessary for --dry-run")
    patrol.add_argument("--device", type=_device, default="stick")
    patrol.add_argument("--knocker", default="auto")
    patrol.add_argument("--speaker-device", type=_device)
    patrol.add_argument("--route", type=Path, default=Path("profiles/route.yaml"))
    patrol.add_argument("--profile", default="rubble")
    patrol.add_argument("--explore", action="store_true", help="navigate using front ultrasonic sensing instead of a fixed route")
    patrol.add_argument("--sensor-port", type=int, choices=range(6, 11))
    patrol.add_argument("--explore-seconds", type=_positive_float, help="bounded exploration duration per start/resume (max 600)")
    patrol.add_argument("--dry-run", action="store_true", help="fake rover and simulated survivor; no hardware")
    patrol.add_argument("--dashboard", action="store_true")
    patrol.add_argument("--host")
    patrol.add_argument("--port", type=int)
    patrol.add_argument("--run-dir", type=Path)
    patrol.add_argument("--seed", type=int, default=1)
    doctor = commands.add_parser("doctor", help="check sentinel hardware and deployment readiness")
    doctor.add_argument("--device", type=_device, default="stick", help="stick, stick:<port>, or audio device")
    doctor.add_argument("--speaker-device", type=_device)
    doctor.add_argument("--play", action="store_true", help="play the reassurance prompt to test the speaker")
    doctor.add_argument("--profile", default="rubble")
    bench = commands.add_parser("bench", help="measure the CPU pipeline on synthetic audio without hardware")
    bench.add_argument("--seconds", type=_positive_float, help="scene length; defaults to diagnostics.yaml")
    bench.add_argument("--classifier", help="override the profile classifier for this measurement")
    bench.add_argument("--seed", type=int)
    bench.add_argument("--profile", default="rubble")
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
    def __init__(self, cfg, *, debug=False):
        self.cfg = cfg
        self.debug = debug
        self.next_level = 0

    def __call__(self, event) -> None:
        data = event.data
        if event.type == "audio_observation":
            flag = " POSSIBLE DISTRESS — review" if data["alert"] else ""
            print(f"{event.t:8.2f}s  {data['label']} score {data['score']:.2f}{flag}", flush=True)
            if data.get("transcript"):
                print(f"          Heard: {data['transcript']}", flush=True)
            if data.get("matched_phrases"):
                print(f"          HELP_PHRASE: {', '.join(data['matched_phrases'])}", flush=True)
            if self.debug:
                print(f"          Vocal scores: {data['scores']}", flush=True)
            return
        if event.type == "audio_monitor_status":
            print(f"{event.t:8.2f}s  {data['message']} (skipped {data['skipped']})", flush=True)
            return
        if event.type == "sound":
            label = "GATED" if data["gated"] else ("KNOCK" if data["is_knock"] else "REJECTED")
            line = (
                f"{event.t:8.2f}s  {label:8s} snr {data['snr_db']:.1f} dB  "
                f"peak {data['peak']:.2f}  score {data.get('score', 0.0):.2f}  ({data['classifier']})"
            )
            evidence = data.get("evidence")
            if evidence:
                weakest = min(evidence, key=evidence.get)
                parts = "  ".join(f"{k}={v:.2f}" for k, v in evidence.items())
                line += f"\n          rules: {parts}  <- weakest: {weakest}"
            if (data["gated"] or not data["is_knock"]) and sys.stdout.isatty():
                line = f"\033[2m{line}\033[0m"
            print(line, flush=True)
            if self.debug:
                for label, score in (data.get("top") or [])[:3]:
                    print(f"          {score:.3f}  {label}", flush=True)
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
    else:
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
    sticks = find_stick_ports(bus=bus)
    for port in sticks:
        print(f"  stick:{port}  M5StickC Plus SE microphone (16000 Hz mono)")
    if not sticks:
        print("No available stick microphones found.")
    return 1 if failed and not devices and not sticks else 0


def _listen(args, profile, bus) -> int:
    cfg = profile.audio
    bus.subscribe(_ListenConsole(cfg, debug=args.debug))
    # Validate the optional inference service before opening microphone/serial.
    classifier = make_classifier(profile, bus=bus)
    source = (
        WavSource(args.wav, realtime=args.realtime, block=cfg.block)
        if args.wav is not None
        else open_input(args.device, cfg, bus, **({"debug": True} if args.debug else {}))
    )
    pipeline = Pipeline(
        source, OnsetDetector(profile.onset), classifier, bus, profile
    )
    failed = []
    bus.subscribe(
        lambda event: failed.append(event)
        if event.type == "error" and event.data.get("severity") != "warning" else None
    )
    pipeline.run()
    return 1 if failed else 0


def _stick(args, profile, bus) -> int:
    port = args.port
    if port is None:
        ports = find_stick_ports(bus=bus)
        if not ports:
            raise ValueError("No stick microphone found; connect it or use hb stick --port <port>.")
        if len(ports) != 1:
            raise ValueError("Multiple stick microphones found; select one with hb stick --port <port>.")
        port = ports[0]
    for line in configure_stick(port, mag=args.value if args.stick_command == "mag" else None,
                                baud=profile.audio.stick_baud):
        print(line, flush=True)
    return 0


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
    source = open_input(args.device, cfg, bus)
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


class _DialogConsole:
    def __call__(self, event) -> None:
        data = event.data
        text = None
        if event.type == "state":
            text = f"{data.get('state', '?')}  status={data.get('status', '?')}"
        elif event.type == "patrol":
            text = f"ROVER {data['state']} · {data.get('gap') or 'route'}"
            if data["state"] == "REVIEW":
                text += " · holding; review evidence, then r + Enter to resume"
            if data.get("fault"):
                text += f" · {data['fault']}"
        elif event.type == "rover" and data.get("reply", {}).get("kind") == "NAV":
            reply = data["reply"]
            text = ("NAV SENSOR_WAIT · motors stopped; retrying distance sensor"
                    if reply["state"] == "SENSOR_WAIT" else
                    f"NAV {reply['state']} · front {reply['cm']:.1f} cm")
        elif event.type == "patrol_cue":
            text = f"ROVER {data['phase']}: {data['label']} at {data['end']:.2f}s"
        elif event.type == "round_result":
            text = (f"round {data['round']}: expected {data['expected']}, heard {data['heard']}  "
                    f"{'match' if data['matched'] else 'no match'}  {data['odds']}")
        elif event.type == "answer":
            names = {"q_people": "people", "q_move": "can_move", "q_hurt": "hurt"}
            text = f"{names.get(data['question'], data['question'])}={data['value']}"
        elif event.type == "approval":
            text = "operator approved"
        elif event.type == "operator" and data.get("command") == "all_quiet":
            text = f"all quiet — {data.get('operator') or 'console operator'}"
        if text is not None:
            print(f"{event.t:7.2f}s  {text}", flush=True)


def _operator_input(session, pipeline) -> None:
    commands = {"c": "challenge", "i": "interview", "r": "reset", "a": "approve"}
    try:
        for line in sys.stdin:
            parts = line.strip().split(maxsplit=1)
            key = parts[0].lower() if parts else ""
            if key == "q":
                pipeline.call_soon(pipeline.stop)
                return
            if key in commands:
                session.command(commands[key])
            elif key == "w":
                session.command("all_quiet", operator=parts[1] if len(parts) > 1 else "console",
                                challenge=True)
    except (OSError, ValueError):
        # Noninteractive stdin may be unavailable; Ctrl-C still stops the run.
        return


def _run_directory(args, profile) -> Path:
    explicit = getattr(args, "run_dir", None)
    if explicit is not None:
        path = Path(explicit)
    else:
        root = Path(profile.server["runs_dir"])
        root.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        number = 1
        while True:
            path = root / (stamp if number == 1 else f"{stamp}-{number:02d}")
            try:
                path.mkdir()
                break
            except FileExistsError:
                number += 1
    print(f"Black box: {path}", flush=True)
    return path


def _serve(runtime, args, profile) -> None:
    from .server import create_app, run_server

    def close_runtime():
        patrol = getattr(runtime, "patrol", None)
        if patrol is not None and not patrol.done:
            patrol.command("estop")
        runtime.close()

    try:
        app = create_app(
            runtime.pipeline, runtime.session, runtime.bus, cfg=profile.server,
            run_id=runtime.run_dir.name, profile=profile,
            blackbox=runtime.blackbox, on_start=runtime.start,
            on_stop=lambda: asyncio.to_thread(close_runtime),
            patrol=getattr(runtime, "patrol", None),
        )
        run_server(app, host=args.host, port=args.port)
    finally:
        close_runtime()


def _run(args, profile, bus) -> int:
    from .prompter import make_prompter

    if args.auto:
        profile = replace(profile, dialog=dict(profile.dialog, auto_challenge=True, auto_interview=True))
    bus.subscribe(_DialogConsole())
    errors = []
    bus.subscribe(lambda event: errors.append(event)
                  if event.type == "error" and event.data.get("severity") != "warning" else None)
    cfg = profile.audio
    runtime = prepare_runtime(
        profile,
        lambda: (WavSource(args.wav, realtime=True, block=cfg.block) if args.wav is not None
                 else open_input(args.device, cfg, bus)),
        lambda pipeline: make_prompter(
            args.knocker, bus, cfg=profile.prompter, clock=lambda: pipeline.t,
            speaker_device=args.speaker_device,
            exclude_ports={pipeline.source.port} if isinstance(pipeline.source, StickMicSource) else set(),
        ),
        bus=bus, run_dir=_run_directory(args, profile), argv=getattr(args, "_argv", sys.argv),
    )
    source = runtime.pipeline.source
    if type(runtime.pipeline.classifier).__name__ == "PassThroughClassifier":
        print("Warning: passthrough classifier counts every transient; use the challenge results.",
              file=sys.stderr)
    status_sink = StickStatusSink(source) if isinstance(source, StickMicSource) else None
    unsubscribe_status = bus.subscribe(status_sink) if status_sink is not None else None
    if status_sink is not None:
        def close_status():
            unsubscribe_status()
            status_sink.close()
        runtime.on_close.append(close_status)
    print("Keys then Enter: c challenge, w [initials] all quiet → challenge, i interview, "
          "r reset, a approve, q quit.", flush=True)
    threading.Thread(target=_operator_input, args=(runtime.session, runtime.pipeline), daemon=True,
                     name="heartbeat-stdin").start()
    try:
        if args.dashboard:
            _serve(runtime, args, profile)
        else:
            runtime.run()
    finally:
        runtime.close()
    return 1 if errors else 0


def _sim(args, profile, bus) -> int:
    if args.people < 0:
        raise ValueError("people must be nonnegative")
    bus.subscribe(_DialogConsole())
    options = dict(
        people=args.people, can_move=args.can_move, hurt=args.hurt,
        no_survivor=args.no_survivor, noise=args.noise, realtime=args.realtime,
        seed=args.seed, bus=bus, run_dir=_run_directory(args, profile),
        argv=getattr(args, "_argv", sys.argv),
    )
    if args.dashboard:
        runtime = prepare_simulation(
            profile, **options, duration_s=profile.server["dashboard_sim_duration_s"],
            stop_when_finished=False,
        )
        _serve(runtime, args, profile)
        result = runtime.result()
    else:
        result = run_simulation(profile, **options)
    snap = result.snapshot
    answers = snap["answers"]
    print(f"Simulation ended at {result.t:.2f}s: {snap['status']} ({snap['state']}), {snap['odds']}.")
    if any(event.type == "error" and event.data.get("severity") != "warning"
           for event in result.events):
        return 1
    if args.no_survivor:
        complete = result.t >= profile.prompter["sim_no_survivor_duration_s"]
        return 0 if result.confirmed_t is None and complete else 1
    expected_people = args.people if args.people else "no answer"
    expected = {"q_people": expected_people, "q_move": args.can_move, "q_hurt": args.hurt}
    return 0 if result.confirmed_t is not None and all(answers.get(k) == v for k, v in expected.items()) else 1


def _calibrate_rover(link, cfg) -> bool:
    """A human measures both motions; establish a known EEPROM baseline first."""
    straight, turn = cfg["calibration_deg_per_cm"], cfg["calibration_deg_per_turn_deg"]
    distance, angle = cfg["calibration_drive_cm"], cfg["calibration_turn_deg"]
    speed = cfg["calibration_speed_pct"]
    print("Calibration will move the rover. Use an unobstructed level surface; Ctrl-C stops it.")
    input("Press Enter to save the profile baseline and begin the straight measurement: ")
    if not link.calibrate(straight, turn) or not link.drive(distance, speed):
        return False
    measured = _positive_float(input(f"Requested {distance:g} cm. Actual distance in cm: "))
    straight *= distance / measured
    if not link.calibrate(straight, turn):
        return False
    input(f"Press Enter to begin the {angle:g}-degree turn measurement: ")
    if not link.turn(angle, speed):
        return False
    measured = _positive_float(input("Actual turn magnitude in degrees: "))
    turn *= angle / measured
    if not link.calibrate(straight, turn):
        return False
    print(f"Saved CAL {straight:g} {turn:g}. Recheck both distances before patrol.")
    return True


def _rover(args, profile, bus) -> int:
    from .rover import RoverLink

    bus.subscribe(lambda event: print(json.dumps(event.data), flush=True)
                  if event.type == "rover" else None)
    link = RoverLink(args.port, cfg=profile.rover, bus=bus)
    try:
        name = args.rover_command
        if name == "calibrate":
            ok = _calibrate_rover(link, profile.rover)
        elif name == "drive":
            ok = link.drive(args.cm, args.speed)
        elif name == "turn":
            ok = link.turn(args.deg, args.speed)
        elif name == "range":
            reading = link.range(args.sensor_port)
            ok = reading is not None and reading["valid"]
        elif name == "led":
            ok = link.led(args.r, args.g, args.b)
        else:
            ok = getattr(link, name)()
        return 0 if ok else 1
    except KeyboardInterrupt:
        link.estop()
        raise
    finally:
        link.close()


def _patrol_input(patrol) -> None:
    commands = {"p": "pause", "r": "resume", "q": "abort", "e": "estop", " ": "estop"}
    try:
        while not patrol.done:
            line = sys.stdin.readline()
            if not line:
                return
            key = line.rstrip("\r\n").lower()
            if key in commands:
                result = patrol.command(commands[key])
                if not result.get("ok"):
                    print(result.get("error", "Rover command failed"), file=sys.stderr)
    except (OSError, ValueError):
        return


def _patrol(args, profile, bus) -> int:
    from .patrol import Patrol, load_route, prepare_patrol_simulation
    from .prompter import make_prompter
    from .rover import RoverLink

    if args.explore:
        navigation = dict(profile.rover["navigation"])
        if args.sensor_port is not None:
            navigation["sensor_port"] = args.sensor_port
        duration = args.explore_seconds if args.explore_seconds is not None else navigation["duration_s"]
        from .rover import navigation_args
        navigation_args(navigation)
        if not profile.rover["audio_stop"]["enabled"] or profile.rover["audio_stop"]["scheduled_listen"]:
            raise ValueError("Use --profile clap-knocks for exploration with audio stops")
        profile = replace(profile, rover={**profile.rover, "navigation": navigation})
        route = load_route([{"name": "Explore 1", "moves": [["explore", duration]]}])
    else:
        if args.sensor_port is not None or args.explore_seconds is not None:
            raise ValueError("--sensor-port and --explore-seconds require --explore")
        route = load_route(args.route)
    if not args.dry_run and not args.rover:
        raise ValueError("--rover is required unless --dry-run is selected")
    profile = replace(profile, dialog=dict(profile.dialog, auto_challenge=False),
                      rover=dict(profile.rover, stop_on_done=not args.dashboard))
    bus.subscribe(_DialogConsole())
    errors = []
    bus.subscribe(lambda event: errors.append(event) if event.type == "error"
                  and event.data.get("severity") != "warning" else None)
    runtime = controller = rover = None
    try:
        directory = _run_directory(args, profile)
        if args.dry_run:
            if args.explore:
                profile = replace(profile, classifier=replace(profile.classifier, name="none"))
            runtime, controller = prepare_patrol_simulation(
                profile, route, bus=bus, run_dir=directory, argv=args._argv,
                seed=args.seed, realtime=args.dashboard)
        else:
            excluded = {args.knocker} if args.knocker not in {"auto", "manual", "speaker"} else set()
            if isinstance(args.device, str) and args.device.startswith("stick:"):
                excluded.add(args.device[len("stick:"):].strip())
            rover = RoverLink(args.rover, cfg=profile.rover, bus=bus, exclude_ports=excluded)

            def source_factory():
                # Reserve and stop the rover before any microphone/knocker scan.
                if not rover.stop():
                    raise RuntimeError("Could not establish a stopped rover")
                return open_input(args.device, profile.audio, bus)

            def prompter_factory(pipeline):
                rover.clock = lambda: pipeline.t
                ports = {rover.port}
                if isinstance(pipeline.source, StickMicSource):
                    ports.add(pipeline.source.port)
                return make_prompter(args.knocker, bus, cfg=profile.prompter,
                                     clock=lambda: pipeline.t, speaker_device=args.speaker_device,
                                     exclude_ports=ports)

            runtime = prepare_runtime(profile, source_factory, prompter_factory, bus=bus,
                                      run_dir=directory, argv=args._argv, rng_seed=args.seed)
            controller = Patrol(rover, runtime.pipeline, runtime.session, runtime.session.prompter,
                                route, profile.rover)
            runtime.on_close.extend([controller.close, rover.close])
            if isinstance(runtime.pipeline.source, StickMicSource):
                sink = StickStatusSink(runtime.pipeline.source)
                unsubscribe = bus.subscribe(sink)
                runtime.on_close.extend([unsubscribe, sink.close])
            controller.start()
        runtime.patrol = controller
        print("Keys then Enter: p pause, r resume, q abort, e or Space E-STOP. Ctrl-C also stops.")
        threading.Thread(target=_patrol_input, args=(controller,), daemon=True,
                         name="heartbeat-patrol-stdin").start()
        if args.dashboard:
            _serve(runtime, args, profile)
        else:
            runtime.start()
            runtime.pipeline.join()
    finally:
        # STOP precedes source/prompt/recording shutdown, including Ctrl-C.
        if controller is not None and not controller.done:
            controller.command("estop")
        if runtime is not None:
            runtime.close()
        if rover is not None:
            rover.close()
    print("Gap | Status | Odds | Answers")
    for result in controller.results:
        position = " (position unverified)" if result.get("arrival_unverified") else ""
        print(f"{result['gap']}{position} | {result['status']} | {result['odds']} | "
              f"{json.dumps(result['answers'], sort_keys=True)}")
    return 0 if controller.state == "DONE" and not errors else 1


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    args._argv = list(sys.argv if argv is None else ["hb", *argv])
    bus = EventBus()
    bus.subscribe(_print_error)
    try:
        if args.command == "devices":
            return _devices(bus)
        if args.command == "verify":
            from .blackbox import verify_run

            result = verify_run(args.run_dir)
            print(result)
            return 0 if result.ok else 1
        if args.command == "report":
            from .blackbox import report_run

            print(report_run(args.run_dir))
            return 0
        if args.command == "replay":
            from .blackbox import replay_run

            result = replay_run(args.source, profile=args.profile, classifier=args.classifier)
            print(f"matched {result['matched']}, missing {result['missing']}, new {result['new']}")
            return 0
        profile = load_profile(args.profile)
        if args.command == "stick":
            return _stick(args, profile, bus)
        if args.command == "rover":
            return _rover(args, profile, bus)
        if args.command == "patrol":
            return _patrol(args, profile, bus)
        if args.command == "doctor":
            from .doctor import format_doctor, run_doctor

            checks = run_doctor(profile, device=args.device, speaker_device=args.speaker_device, play=args.play)
            print(format_doctor(checks))
            return 0 if all(check.ok or check.skipped for check in checks) else 1
        if args.command == "bench":
            from .doctor import format_bench, run_bench

            result = run_bench(profile, seconds=args.seconds, classifier=args.classifier, seed=args.seed)
            print(format_bench(result))
            return 0 if not result.get("errors") else 1
        if args.command == "listen":
            return _listen(args, profile, bus)
        if args.command == "record":
            return _record(args, profile, bus)
        if args.command == "run":
            return _run(args, profile, bus)
        if args.command == "sim":
            return _sim(args, profile, bus)
        return _synth(args, profile)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        bus.publish("error", 0.0, where=args.command, message=str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
