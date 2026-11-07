#!/usr/bin/env python3
"""Measure false alarms on a background-only WAV, using no audio hardware.

``uv run python tools/eval_background.py venue.wav --classifier auto`` writes
reports/background_venue.md. The WAV must contain no deliberate human answers;
all knock classifications are treated as false alarms, regardless of label.
"""

from __future__ import annotations

import argparse
from dataclasses import replace
from importlib.util import find_spec
import math
from pathlib import Path
import sys

import numpy as np

from heartbeat.audio import SAMPLE_RATE, WavSource
from heartbeat.classify import make_classifier
from heartbeat.config import load_profile
from heartbeat.onset import OnsetDetector


def _dialog_availability() -> str | None:
    if find_spec("heartbeat.dialog") is None:
        return "P03 DialogEngine is not available; simulated false confirmations were not measured."
    return None


def simulate_dialog_events(detections, duration_s: float, cfg, *, emit=None) -> dict:
    """Replay classified events against the actual pure engine on sample time.

    Clip lookahead is irrelevant to event-time replay: detection timestamps,
    prompt completion times, window deadlines, and gates retain their exact
    stream times. FakePrompter supplies durations but injects no answers. No
    worker thread, hardware backend, wall clock, or sleep is used here.
    """
    from heartbeat.dialog import DialogEngine
    from heartbeat.prompter import FakePrompter

    duration_s = float(duration_s)
    if not math.isfinite(duration_s) or duration_s <= 0:
        raise ValueError("Dialog evaluation duration must be finite and positive")
    interval = float(cfg.classifier.evaluation["challenge_interval_s"])
    if not math.isfinite(interval) or interval <= 0:
        raise ValueError("evaluation.challenge_interval_s must be finite and positive")
    seed = cfg.audio.synth["scene"]["seed"]
    fake = FakePrompter(cfg=cfg.prompter, no_survivor=True, seed=seed)
    events = iter(detections)
    last_detection_t = -math.inf

    def next_event():
        nonlocal last_detection_t
        detection = next(events, None)
        if detection is not None:
            t = float(detection.t)
            if not math.isfinite(t) or not 0 <= t <= duration_s or t < last_detection_t:
                raise ValueError("Detections must be ordered finite times within the recording")
            last_detection_t = t
        return detection

    current = next_event()
    previous_t = -math.inf
    status = "NO_SIGNS"
    confirmations = 0
    window = None
    cooldown_end = None
    prompt = None
    gates = []
    attempts = 0
    started = 0
    rearmed = 0
    prompt_count = 0
    last_action_t = 0.0

    def publish(kind, t, data):
        nonlocal status, confirmations, window, cooldown_end, last_action_t
        if kind == "state":
            new_status = data["status"]
            if new_status == "CONFIRMED" and status != "CONFIRMED":
                confirmations += 1
            status = new_status
            state = data["state"]
            if state in {"PROMPTING", "INTERVIEW", "CONFIRMED"}:
                last_action_t = float(t)
            if state == "NO_RESPONSE":
                cooldown_end = float(t) + cfg.dialog["cooldown_s"]
            elif state == "LISTENING":
                cooldown_end = None
        elif kind == "window_open":
            window = dict(data, last=None)
        elif kind in {"round_result", "answer"}:
            window = None
        if emit is not None:
            emit(kind, float(t), dict(data))

    engine = DialogEngine(cfg, publish, rng_seed=seed)

    def dispatch(actions, t):
        nonlocal prompt, prompt_count
        if not actions:
            return
        if prompt is not None:
            raise ValueError("Dialog emitted overlapping virtual prompt batches")
        start = min(float(t), last_action_t)
        if start >= duration_s:
            return
        end, replies = fake.schedule_batch(actions, start)
        if replies:
            raise ValueError("Background FakePrompter unexpectedly generated survivor answers")
        if not math.isfinite(end) or end < start:
            raise ValueError("FakePrompter returned an invalid completion time")
        gate = [start, math.inf]
        gates.append(gate)
        prompt = (float(end), gate)
        prompt_count += 1
        engine.gate_until(math.inf)
        if emit is not None:
            emit("prompt_start", start, {"actions": [dict(vars(action)) for action in actions]})

    tick_index = 0
    challenge_index = 0
    hop_s = cfg.audio.block / SAMPLE_RATE
    while True:
        tick_t = tick_index * hop_s
        challenge_t = challenge_index * interval
        window_end = math.inf
        if window is not None:
            window_end = float(window["end"])
            if window["last"] is not None:
                early = cfg.dialog["early_close_s"] if window["kind"] == "round" else cfg.dialog["question_early_close_s"]
                window_end = min(window_end, window["last"] + early)
        t = min(
            float(current.t) if current is not None else math.inf,
            tick_t, challenge_t if challenge_t < duration_s else math.inf,
            prompt[0] if prompt is not None else math.inf,
            window_end, cooldown_end if cooldown_end is not None else math.inf,
            duration_s,
        )
        if t < previous_t:
            raise ValueError("Detection timestamps or virtual dialog events moved backwards")
        previous_t = t
        # Complete prompts before same-time sounds; the inclusive tail gate
        # still prevents that sound from counting as an answer.
        if prompt is not None and prompt[0] == t:
            end, gate = prompt
            prompt = None
            gate[1] = end + cfg.prompter["gate_tail_s"]
            engine.gate_until(gate[1])
            if emit is not None:
                emit("prompt_end", end, {})
            dispatch(engine.on_prompts_finished(end), end)
        if challenge_t == t and challenge_t < duration_s:
            attempts += 1
            # Confirmation is terminal until reset. Re-arm at the next
            # scheduled opportunity once no prompt is in flight.
            if engine.snapshot()["status"] == "CONFIRMED" and prompt is None:
                engine.command("reset", t)
                window = None
                cooldown_end = None
                rearmed += 1
            actions = engine.command("challenge", t)
            started += bool(actions)
            dispatch(actions, t)
            challenge_index += 1
        while current is not None and float(current.t) == t:
            gated = bool(current.gated) or any(start <= t <= end for start, end in gates)
            detection = replace(current, gated=gated)
            dispatch(engine.on_sound(detection), t)
            if detection.is_knock and not gated and window is not None and window["start"] <= t <= window["end"]:
                window["last"] = max(t, window["last"] if window["last"] is not None else t)
            current = next_event()
        # Sounds at an inclusive response deadline precede the closing tick.
        dispatch(engine.tick(t), t)
        if tick_t == t:
            tick_index += 1
        if t >= duration_s:
            break
    return {
        "false_confirmations": confirmations,
        "false_confirmations_per_hour": confirmations / (duration_s / 3600),
        "challenge_attempts": attempts, "challenges_started": started,
        "challenges_rejected": attempts - started, "rearmed_after_confirmation": rearmed,
        "prompt_batches": prompt_count, "final_status": engine.snapshot()["status"],
        "challenge_interval_s": interval, "dialog_skip_reason": None,
    }


def evaluate_background(wav_path: str | Path, cfg, classifier=None, *, synthetic: bool = False) -> dict:
    """Stream a recording through P01 detection and classification, including EOF."""
    wav_path = Path(wav_path).resolve()
    classifier = make_classifier(cfg) if classifier is None else classifier
    detector = OnsetDetector(cfg)
    source = WavSource(wav_path, block=cfg.audio.block)
    samples = 0
    onsets = 0
    false_knocks = 0
    active = set()
    dialog_events = []
    skipped = _dialog_availability()

    def classify(onset):
        nonlocal onsets, false_knocks
        detection = classifier.classify(onset)
        onsets += 1
        false_knocks += int(detection.is_knock)
        active.add(str(detection.classifier))
        if skipped is None:
            # Store event metadata, not a growing collection of audio clips.
            dialog_events.append(replace(detection, clip=np.empty(0, dtype=np.float32)))

    try:
        for block in source:
            samples += len(block.x)
            for onset in detector.process(block):
                classify(onset)
        for onset in detector.flush():
            classify(onset)
    finally:
        source.close()
    duration = samples / SAMPLE_RATE
    if not duration:
        raise ValueError("Background recording is empty; per-hour rates are undefined")
    hours = duration / 3600
    result = {
        "wav": str(wav_path), "duration_s": duration,
        "synthetic": bool(synthetic),
        "requested_classifier": cfg.classifier.name, "classifiers": sorted(active),
        "onsets": onsets, "onsets_per_hour": onsets / hours,
        "false_knocks": false_knocks, "false_knocks_per_hour": false_knocks / hours,
        "false_confirmations": None, "false_confirmations_per_hour": None,
        "dialog_skip_reason": skipped,
    }
    if skipped is None:
        result.update(simulate_dialog_events(dialog_events, duration, cfg))
    return result


def write_report(result: dict, output_dir: str | Path) -> Path:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = Path(result["wav"]).stem
    path = output_dir / f"background_{stem}.md"
    active = ", ".join(result["classifiers"]) or "no onsets; classifier not exercised"
    duration = result["duration_s"]
    provenance = (
        "**Synthetic fixture only. These results do not measure venue or hardware performance.**"
        if result.get("synthetic", False)
        else "Background recording; provenance and absence of human answers are supplied by the operator."
    )
    report = (
        f"# Background evaluation: {stem}\n\n"
        f"{provenance}\n\n"
        f"Input: `{result['wav']}`. Duration: {duration:.6f} s "
        f"({duration / 3600:.6f} h). Requested classifier: `{result['requested_classifier']}`. "
        f"Active classifier(s): {active}.\n\n"
        "| Measure | Count | Rate per hour |\n"
        "|---|---:|---:|\n"
        f"| Onsets | {result['onsets']} | {result['onsets_per_hour']:.6f} |\n"
        f"| Knock-classified false alarms | {result['false_knocks']} | {result['false_knocks_per_hour']:.6f} |\n"
    )
    if result["false_confirmations"] is None:
        report += "| Simulated false confirmations | not measured | not measured |\n\n"
        report += f"Skipped: {result['dialog_skip_reason']}\n\n"
    else:
        report += (
            f"| Simulated false confirmations | {result['false_confirmations']} | "
            f"{result['false_confirmations_per_hour']:.6f} |\n\n"
        )
        report += (
            f"The actual P03 DialogEngine was challenged every {result['challenge_interval_s']:g} s, "
            f"starting at t=0: {result['challenge_attempts']} attempts, {result['challenges_started']} "
            f"started, {result['challenges_rejected']} rejected while busy. "
            f"FakePrompter(no_survivor=True) scheduled {result['prompt_batches']} prompt batches "
            "with their actual virtual durations and injected no answers. Prompt completion, "
            "inclusive gate tails, audio onsets, and response deadlines were replayed in stream-time "
            "order. Each distinct transition into CONFIRMED counts once; "
            f"{result['rearmed_after_confirmation']} confirmed trials were reset before a later scheduled "
            f"challenge. Final status: {result['final_status']}.\n\n"
        )
    report += (
        "The input is assumed to be background only, with no deliberate human responses. "
        "All knock classifications are therefore counted as false alarms. Audio was "
        "streamed through WavSource and OnsetDetector, including the terminal flush, "
        "using sample time and no microphone, speaker, serial port, or real-time sleeps. "
        "Per-hour rates are extrapolated from this recording's duration; a zero count "
        "does not establish a zero long-term error rate.\n"
    )
    path.write_text(report, encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("wav", type=Path)
    parser.add_argument("--classifier", help="override the profile's classifier registry name")
    parser.add_argument("--profile", default="rubble")
    parser.add_argument("--output", type=Path, default=Path("reports"))
    parser.add_argument("--synthetic", action="store_true", help="mark the report as synthetic-only, not measured venue performance")
    args = parser.parse_args(argv)
    try:
        cfg = load_profile(args.profile)
        if args.classifier:
            cfg = replace(cfg, classifier=replace(cfg.classifier, name=args.classifier))
        result = evaluate_background(args.wav, cfg, synthetic=args.synthetic)
        path = write_report(result, args.output)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"Duration: {result['duration_s']:.3f} s; onsets: {result['onsets']} ({result['onsets_per_hour']:.3f}/h)")
    print(f"Knock false alarms: {result['false_knocks']} ({result['false_knocks_per_hour']:.3f}/h)")
    if result["dialog_skip_reason"]:
        print(f"Skipped simulated false confirmations: {result['dialog_skip_reason']}")
    else:
        print(f"False confirmations: {result['false_confirmations']} ({result['false_confirmations_per_hour']:.3f}/h)")
    print(f"Report: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
