#!/usr/bin/env python3
"""Segment labeled recordings with the same streaming detector as the sentinel.

Run ``uv run python tools/segment.py --raw data/raw --clips data/clips``.
Index paths are relative to index.csv; source_file is relative to the raw root.
Re-running segmentation regenerates the index and clips, including clips that
were removed during review. Review after the final segmentation pass.
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
import math
from pathlib import Path
import sys

import soundfile as sf

from heartbeat.audio import SAMPLE_RATE, WavSource
from heartbeat.config import load_profile
from heartbeat.onset import OnsetDetector


LABELS = ("knock", "other", "bg", "self")
INDEX_FIELDS = ("path", "label", "source_file", "surface", "t", "snr_db")


@dataclass(frozen=True)
class RawRecording:
    path: Path
    relative_path: Path
    label: str
    surface: str


def parse_raw_name(path: str | Path) -> tuple[str, str]:
    """Validate the capture naming contract and return its label and surface."""
    path = Path(path)
    parts = path.stem.split("__")
    if (
        path.suffix.lower() != ".wav"
        or len(parts) != 4
        or parts[0] not in LABELS
        or not all(part.strip() for part in parts)
        or not parts[3].isascii()
        or not parts[3].isdigit()
    ):
        raise ValueError(
            f"Invalid raw filename {path.name!r}; expected "
            "<knock|other|bg|self>__<surface>__<desc>__<NN>.wav"
        )
    return parts[0], parts[1]


def discover_recordings(raw_dir: str | Path) -> list[RawRecording]:
    """Preflight every name and reject aliases before writing any output."""
    raw_dir = Path(raw_dir).resolve()
    if not raw_dir.is_dir():
        raise ValueError(f"Raw directory does not exist: {raw_dir}")
    result = []
    seen_files = set()
    seen_stems = set()
    for path in sorted(raw_dir.rglob("*")):
        if not path.is_file() or path.suffix.lower() != ".wav":
            continue
        label, surface = parse_raw_name(path)
        relative = path.relative_to(raw_dir)
        resolved = path.resolve()
        if not resolved.is_relative_to(raw_dir):
            raise ValueError(f"Raw recording points outside the raw directory: {relative}")
        stat = path.stat()
        identity = (stat.st_dev, stat.st_ino)
        key = (label, relative.with_suffix("").as_posix())
        if identity in seen_files or key in seen_stems:
            raise ValueError(f"Duplicate source recording or output stem: {relative}")
        seen_files.add(identity)
        seen_stems.add(key)
        result.append(RawRecording(path, relative, label, surface))
    return result


def resolve_index_path(row: dict[str, str], index_path: str | Path) -> Path:
    """Resolve one clip key, rejecting paths outside the index directory."""
    root = Path(index_path).resolve().parent
    relative = Path(row["path"])
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"Clip path must be relative to index.csv: {row['path']!r}")
    resolved = (root / relative).resolve()
    if not resolved.is_relative_to(root) or resolved == root:
        raise ValueError(f"Clip path escapes the index directory: {row['path']!r}")
    return resolved


def read_index(index_path: str | Path) -> list[dict[str, str]]:
    """Read an index without dropping deleted clips; callers report exclusions."""
    with Path(index_path).open(newline="", encoding="utf-8") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != list(INDEX_FIELDS):
            raise ValueError(f"Index must have columns {', '.join(INDEX_FIELDS)}")
        result = []
        seen = set()
        seen_events = set()
        sources = {}
        for line, row in enumerate(reader, start=2):
            if None in row or any(value is None for value in row.values()):
                raise ValueError(f"Malformed index row {line}")
            if row["label"] not in LABELS:
                raise ValueError(f"Invalid label at index row {line}: {row['label']!r}")
            if not row["source_file"].strip() or not row["surface"].strip():
                raise ValueError(f"Missing source_file or surface at index row {line}")
            source = Path(row["source_file"])
            if source.is_absolute() or ".." in source.parts or source == Path("."):
                raise ValueError(f"source_file must be relative to the raw root at index row {line}")
            row["source_file"] = source.as_posix()
            source_metadata = (row["label"], row["surface"])
            if sources.setdefault(row["source_file"], source_metadata) != source_metadata:
                raise ValueError(f"One source recording has conflicting labels or surfaces at index row {line}")
            try:
                t, snr = float(row["t"]), float(row["snr_db"])
            except ValueError as exc:
                raise ValueError(f"Invalid timestamp or SNR at index row {line}") from exc
            if not math.isfinite(t) or t < 0 or not math.isfinite(snr):
                raise ValueError(f"Nonfinite timestamp/SNR or negative time at index row {line}")
            event = (row["source_file"], round(t * SAMPLE_RATE))
            if event in seen_events:
                raise ValueError(f"Duplicate source onset at index row {line}")
            seen_events.add(event)
            path = resolve_index_path(row, index_path)
            if path in seen:
                raise ValueError(f"Duplicate clip path at index row {line}: {row['path']!r}")
            seen.add(path)
            result.append(row)
    return result


def segment(raw_dir: str | Path, clips_dir: str | Path, cfg) -> dict:
    """Stream recordings, flush EOF onsets, and atomically replace index.csv."""
    raw_dir, clips_dir = Path(raw_dir).resolve(), Path(clips_dir).resolve()
    if clips_dir.is_relative_to(raw_dir):
        raise ValueError("The clips directory must be outside the raw directory")
    recordings = discover_recordings(raw_dir)
    clips_dir.mkdir(parents=True, exist_ok=True)
    index_path = clips_dir / "index.csv"
    temporary = clips_dir / "index.csv.tmp"
    counts = {label: 0 for label in LABELS}
    seen_paths = set()
    try:
        with temporary.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=INDEX_FIELDS)
            writer.writeheader()
            for recording in recordings:
                detector = OnsetDetector(cfg)
                source = WavSource(recording.path, block=cfg.audio.block)

                def write_onsets(onsets):
                    for onset in onsets:
                        # Six fractional digits preserve the 16 kHz sample lattice.
                        name = f"{recording.path.stem}__{onset.t:.6f}.wav"
                        relative = Path(recording.label) / recording.relative_path.parent / name
                        row = {
                            "path": relative.as_posix(),
                            "label": recording.label,
                            "source_file": recording.relative_path.as_posix(),
                            "surface": recording.surface,
                            "t": f"{onset.t:.6f}",
                            "snr_db": f"{onset.snr_db:.6f}",
                        }
                        output = resolve_index_path(row, index_path)
                        if output in seen_paths:
                            raise ValueError(f"Duplicate generated clip key: {relative}")
                        seen_paths.add(output)
                        output.parent.mkdir(parents=True, exist_ok=True)
                        sf.write(output, onset.clip, SAMPLE_RATE, subtype="FLOAT")
                        writer.writerow(row)
                        counts[recording.label] += 1

                try:
                    for block in source:
                        write_onsets(detector.process(block))
                    write_onsets(detector.flush())
                finally:
                    source.close()
        temporary.replace(index_path)
    finally:
        temporary.unlink(missing_ok=True)
    return {"recordings": len(recordings), "clips": sum(counts.values()), "labels": counts, "index": str(index_path)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--raw", type=Path, default=Path("data/raw"))
    parser.add_argument("--clips", type=Path, default=Path("data/clips"))
    parser.add_argument("--profile", default="rubble")
    args = parser.parse_args(argv)
    try:
        result = segment(args.raw, args.clips, load_profile(args.profile))
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"Segmented {result['recordings']} recordings into {result['clips']} clips: {result['labels']}")
    print(f"Index: {result['index']} (paths relative to its directory)")
    if not result["recordings"]:
        print("No raw WAVs found; no measured or synthetic classifier results were produced.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
