#!/usr/bin/env python3
"""Create offline label review pages, or remove clips selected by a human.

Run ``uv run python tools/label_review.py --index data/clips/index.csv`` and
open reports/review_knock.html (or another label). Mark bad clips and download
their removal list. Apply it with ``--remove-list /path/to/review_remove.json``.
The index remains unchanged so training can report how many clips were removed.
"""

from __future__ import annotations

import argparse
from dataclasses import replace
import html
import json
import math
import os
from pathlib import Path
import shlex
import sys
from urllib.parse import quote

import numpy as np
import soundfile as sf

from heartbeat.audio import SAMPLE_RATE
from heartbeat.classify import make_classifier
from heartbeat.config import load_profile
from heartbeat.onset import CLIP_SAMPLES, PEAK_SAMPLES, PRE_SAMPLES, Onset

try:  # Both ``python tools/label_review.py`` and ``python -m tools.label_review``.
    from tools.segment import LABELS, read_index, resolve_index_path
except ModuleNotFoundError:
    from segment import LABELS, read_index, resolve_index_path


def load_clip(row: dict[str, str], index_path: str | Path) -> Onset:
    """Reconstruct the classifier input, validating the shared audio contract."""
    path = resolve_index_path(row, index_path)
    samples, rate = sf.read(path, dtype="float32")
    if rate != SAMPLE_RATE or samples.ndim != 1 or len(samples) != CLIP_SAMPLES:
        raise ValueError(f"Clip must be {CLIP_SAMPLES} mono samples at {SAMPLE_RATE} Hz: {path}")
    if not np.all(np.isfinite(samples)) or np.any(np.abs(samples) > 1):
        raise ValueError(f"Clip must contain finite samples in [-1, 1]: {path}")
    return Onset(
        t=float(row["t"]),
        peak=float(np.max(np.abs(samples[PRE_SAMPLES : PRE_SAMPLES + PEAK_SAMPLES]))),
        snr_db=float(row["snr_db"]), gated=False, clip=samples,
    )


def waveform_svg(samples: np.ndarray, points: int) -> str:
    """Peak-preserving min/max bins; no plotting dependency or external assets."""
    if not isinstance(points, int) or isinstance(points, bool) or points < 2:
        raise ValueError("evaluation.waveform_points must be an integer of at least 2")
    samples = np.asarray(samples, dtype=float)
    if samples.ndim != 1 or not len(samples) or not np.all(np.isfinite(samples)):
        raise ValueError("Waveform must be a nonempty finite mono vector")
    points = min(points, len(samples))
    edges = np.linspace(0, len(samples), points + 1, dtype=int)
    peak = float(np.max(np.abs(samples))) or 1.0
    # Viewbox units are graphic geometry, not detector or classifier tuning.
    bars = []
    for index, (start, stop) in enumerate(zip(edges[:-1], edges[1:])):
        part = samples[start:stop]
        low, high = float(np.min(part)) / peak, float(np.max(part)) / peak
        bars.append(f"M{index + 0.5:.1f},{50 - 45 * high:.2f}V{50 - 45 * low:.2f}")
    return (
        f'<svg viewBox="0 0 {points} 100" role="img" aria-label="Audio waveform" '
        'preserveAspectRatio="none"><path class="axis" d="M0 50H'
        f'{points}"/><path class="wave" d="{" ".join(bars)}"/></svg>'
    )


def remove_clips(index_path: str | Path, removal_list: str | Path) -> int:
    """Delete only clip keys selected in a review page, validating all first."""
    rows = read_index(index_path)
    indexed = {row["path"]: row for row in rows}
    with Path(removal_list).open(encoding="utf-8") as stream:
        keys = json.load(stream)
    if not isinstance(keys, list) or not all(isinstance(key, str) for key in keys):
        raise ValueError("Removal list must be a JSON array of indexed clip paths")
    keys = sorted(set(keys))
    unknown = set(keys) - indexed.keys()
    if unknown:
        raise ValueError(f"Removal list contains paths outside this index: {sorted(unknown)}")
    paths = [resolve_index_path(indexed[key], index_path) for key in keys]
    removed = 0
    for path in paths:
        if path.is_file():
            path.unlink()
            removed += 1
    return removed


_STYLE = """
body{font:16px system-ui,sans-serif;max-width:1100px;margin:2rem auto;padding:0 1rem;background:#fafafa;color:#202020}
nav a{margin-right:1rem} article{background:white;border:1px solid #ccc;border-radius:6px;padding:1rem;margin:1rem 0}
article:has(input:checked){opacity:.55;border-color:#a22} h2{font-size:1rem;overflow-wrap:anywhere}
svg{display:block;width:100%;height:90px;background:#f1f5f8;margin:.5rem 0}.wave{stroke:#174d75;stroke-width:1;fill:none}.axis{stroke:#b9c6cf;stroke-width:.5}
audio{width:100%}code{overflow-wrap:anywhere}button{font:inherit;padding:.6rem;margin:.5rem 0;cursor:pointer}
.meta{color:#555;font-size:.9rem}.sticky{position:sticky;top:0;background:#fafafa;padding:.5rem 0;border-bottom:1px solid #bbb;z-index:1}
"""

_SCRIPT = """
document.getElementById('download').addEventListener('click', () => {
  const paths = [...document.querySelectorAll('input[data-path]:checked')].map(el => el.dataset.path);
  if (!paths.length) { document.getElementById('selection').textContent = 'Select clips first.'; return; }
  const url = URL.createObjectURL(new Blob([JSON.stringify(paths, null, 2) + '\\n'], {type: 'application/json'}));
  const link = document.createElement('a'); link.href = url; link.download = 'review_remove.json'; link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
});
document.addEventListener('change', () => {
  document.getElementById('selection').textContent = document.querySelectorAll('input[data-path]:checked').length + ' selected';
});
"""


def build_review(index_path: str | Path, output_dir: str | Path, cfg, classifier=None) -> dict:
    """Score extant clips and write one self-contained HTML contact sheet per label."""
    index_path, output_dir = Path(index_path).resolve(), Path(output_dir).resolve()
    rows = read_index(index_path)
    classifier = make_classifier(cfg) if classifier is None else classifier
    points = cfg.classifier.evaluation["waveform_points"]
    by_label = {label: [] for label in LABELS}
    removed = 0
    active = set()
    for row in rows:
        path = resolve_index_path(row, index_path)
        if not path.is_file():
            removed += 1
            continue
        onset = load_clip(row, index_path)
        detection = classifier.classify(onset)
        score = float(detection.score)
        if not math.isfinite(score) or not 0 <= score <= 1:
            raise ValueError(f"Classifier returned an invalid score for {path}")
        active.add(str(detection.classifier))
        by_label[row["label"]].append((score, row, path, waveform_svg(onset.clip, points)))
    output_dir.mkdir(parents=True, exist_ok=True)
    pages = []
    for label, clips in by_label.items():
        clips.sort(key=lambda item: (item[0] if label == "knock" else -item[0], item[1]["path"]))
        order = "Lowest-scoring knocks first" if label == "knock" else "Highest-scoring negatives first"
        page_path = output_dir / f"review_{label}.html"
        cards = []
        for score, row, path, waveform in clips:
            audio_url = quote(os.path.relpath(path, output_dir).replace(os.sep, "/"), safe="/.")
            cards.append(
                '<article><h2>' + html.escape(row["path"]) + '</h2>'
                '<p class="meta">Score: ' + f'{score:.6f}' + ' · Source: '
                + html.escape(row["source_file"]) + ' · Surface: ' + html.escape(row["surface"])
                + ' · t=' + html.escape(row["t"]) + ' s · SNR=' + html.escape(row["snr_db"]) + ' dB</p>'
                + waveform + '<audio controls preload="none" src="' + html.escape(audio_url, quote=True) + '"></audio>'
                '<p><label><input type="checkbox" data-path="' + html.escape(row["path"], quote=True)
                + '"> Mark for removal</label></p><p class="meta">File: <code>'
                + html.escape(str(path)) + '</code></p></article>'
            )
        navigation = " ".join(f'<a href="review_{name}.html">{name}</a>' for name in LABELS)
        content = (
            '<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>Heartbeat review: {label}</title><style>{_STYLE}</style><body>'
            f'<h1>Review {label}: {len(clips)} clips</h1><nav>{navigation}</nav>'
            f'<p>{order}. Active classifiers: {html.escape(", ".join(sorted(active)) or "none (no clips)")}.</p>'
            f'<p>{removed} indexed clips are missing and excluded. Waveforms are normalized per clip.</p>'
            '<p>Listen, mark mislabeled clips, then download a removal list. Apply it with '
            '<code>uv run python tools/label_review.py --index ' + html.escape(shlex.quote(str(index_path)))
            + ' --remove-list /path/to/review_remove.json</code>. You can also delete the displayed WAV files yourself. '
            'Training skips missing indexed clips and reports their count. Re-segmenting restores deleted clips.</p>'
            '<div class="sticky"><button id="download" type="button">Download selected removal list</button> '
            '<span id="selection" aria-live="polite">0 selected</span></div>'
            + ("".join(cards) or '<p>No remaining clips for this label.</p>')
            + f'<script>{_SCRIPT}</script></body></html>'
        )
        page_path.write_text(content, encoding="utf-8")
        pages.append(str(page_path))
    return {"clips": sum(map(len, by_label.values())), "removed": removed, "pages": pages, "classifiers": sorted(active)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--index", type=Path, default=Path("data/clips/index.csv"))
    parser.add_argument("--output", type=Path, default=Path("reports"))
    parser.add_argument("--profile", default="rubble")
    parser.add_argument("--classifier", help="override the profile's classifier registry name")
    parser.add_argument("--remove-list", type=Path, help="explicitly delete clips in a downloaded review JSON list")
    args = parser.parse_args(argv)
    try:
        cfg = load_profile(args.profile)
        if args.classifier:
            cfg = replace(cfg, classifier=replace(cfg.classifier, name=args.classifier))
        if args.remove_list:
            print(f"Removed {remove_clips(args.index, args.remove_list)} selected clips; index preserved.")
        result = build_review(args.index, args.output, cfg)
    except (OSError, ValueError, RuntimeError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"Reviewed {result['clips']} clips; {result['removed']} missing indexed files excluded.")
    for page in result["pages"]:
        print(page)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
