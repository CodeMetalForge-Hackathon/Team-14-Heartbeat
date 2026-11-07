#!/usr/bin/env python3
"""Generate committed voice WAVs using local TTS file output; never play audio."""

from __future__ import annotations

import argparse
from math import gcd
from pathlib import Path
import platform
import shutil
import subprocess
import tempfile

import numpy as np
from scipy.signal import resample_poly
import soundfile as sf

from heartbeat.audio import SAMPLE_RATE
from heartbeat.prompter import VOICE_TEXT
from heartbeat.synth import knock


ROOT = Path(__file__).resolve().parent.parent


def _verify_file(path: Path) -> None:
    data, rate = sf.read(path, dtype="float32")
    if rate != SAMPLE_RATE or data.ndim != 1 or not len(data) or not np.any(data) or not np.all(np.isfinite(data)):
        raise ValueError(f"{path} must be a nonempty, audible 16000 Hz mono WAV")
    if sf.info(path).subtype != "PCM_16":
        raise ValueError(f"{path} must use 16-bit PCM")


def verify(directory: Path, *, only: str | None = None) -> None:
    keys = (*VOICE_TEXT, "knock") if only is None else (only,)
    for key in keys:
        if key not in VOICE_TEXT and key != "knock":
            raise ValueError(f"Unknown voice prompt: {key}")
        _verify_file(directory / f"{key}.wav")


def generate(directory: Path, *, voice: str | None = None, only: str | None = None) -> None:
    if only is not None and only not in VOICE_TEXT and only != "knock":
        raise ValueError(f"Unknown voice prompt: {only}")
    mac = platform.system() == "Darwin"
    required = ("say", "afconvert") if mac else ("espeak-ng",)
    binaries = {name: shutil.which(name) for name in required}
    missing = [name for name, path in binaries.items() if path is None]
    if missing:
        raise RuntimeError(f"Missing local speech tools: {', '.join(missing)}")
    directory.mkdir(parents=True, exist_ok=True)
    # Stage on the destination filesystem so a failed TTS process cannot
    # replace a valid committed asset with a zero-frame output file.
    with tempfile.TemporaryDirectory(prefix=".heartbeat-voice-", dir=directory) as temporary:
        for key, text in VOICE_TEXT.items():
            if only is not None and key != only:
                continue
            source = Path(temporary) / (f"{key}.aiff" if mac else f"{key}.wav")
            target = Path(temporary) / f"{key}-converted.wav"
            if mac:
                command = [binaries["say"], "-o", str(source)]
                if voice:
                    command.extend(["-v", voice])
                subprocess.run([*command, text], check=True, capture_output=True, text=True)
                subprocess.run(
                    [binaries["afconvert"], "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", str(source), str(target)],
                    check=True, capture_output=True, text=True,
                )
            else:
                command = [binaries["espeak-ng"], "-w", str(source)]
                if voice:
                    command.extend(["-v", voice])
                subprocess.run([*command, text], check=True, capture_output=True, text=True)
                data, rate = sf.read(source, dtype="float32", always_2d=True)
                mono = data.mean(axis=1)
                divisor = gcd(rate, SAMPLE_RATE)
                converted = resample_poly(mono, SAMPLE_RATE // divisor, rate // divisor)
                sf.write(target, np.clip(converted, -1, 1), SAMPLE_RATE, subtype="PCM_16")
            _verify_file(target)
            target.replace(directory / f"{key}.wav")
        if only is None or only == "knock":
            target = Path(temporary) / "knock.wav"
            sf.write(target, knock(), SAMPLE_RATE, subtype="PCM_16")
            _verify_file(target)
            target.replace(directory / "knock.wav")
    verify(directory, only=only)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=ROOT / "assets" / "voice")
    parser.add_argument("--voice", help="Optional locally installed TTS voice")
    parser.add_argument("--only", choices=(*VOICE_TEXT, "knock"), help="Generate or verify just this asset")
    parser.add_argument("--check", action="store_true", help="Verify existing files without generating speech")
    args = parser.parse_args(argv)
    try:
        verify(args.out, only=args.only) if args.check else generate(args.out, voice=args.voice, only=args.only)
    except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError) as exc:
        detail = exc.stderr.strip() if isinstance(exc, subprocess.CalledProcessError) and exc.stderr else str(exc)
        parser.exit(1, f"Voice assets failed: {detail}\n")
    count = 1 if args.only is not None else len(VOICE_TEXT) + 1
    print(f"Verified {count} voice asset(s) in {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
