#!/usr/bin/env python3
"""Fetch the public YAMNet artifacts only after explicit network approval.

    uv run --extra ml python tools/fetch_models.py --allow-network
    uv run --extra ml python tools/fetch_models.py --verify-only

The large .tflite stays ignored by git; the small CSV can be committed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
import time
import urllib.error
import urllib.request

import numpy as np

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from heartbeat.config import load_profile
from heartbeat.onset import CLIP_SAMPLES


MODEL_URL = "https://storage.googleapis.com/mediapipe-models/audio_classifier/yamnet/float32/1/yamnet.tflite"
CLASS_MAP_URL = "https://raw.githubusercontent.com/tensorflow/models/master/research/audioset/yamnet/yamnet_class_map.csv"


def download(url: str, destination: Path, *, timeout_s: float, max_bytes: int) -> None:
    """Atomically replace one artifact; a failed download never truncates it."""
    if not url.startswith("https://"):
        raise ValueError("artifact downloads require an HTTPS URL")
    temporary = destination.with_suffix(destination.suffix + ".part")
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as response:
            if not response.geturl().startswith("https://"):
                raise ValueError("refusing a download redirected away from HTTPS")
            # Read at most the configured limit, including one overflow byte.
            contents = response.read(max_bytes + 1)
            if not contents or len(contents) > max_bytes:
                raise ValueError("empty artifact or download exceeds configured size limit")
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_bytes(contents)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    print(f"Saved {destination}: {len(contents)} bytes, sha256={hashlib.sha256(contents).hexdigest()}")


def _tensor_detail(detail: dict) -> dict:
    return {
        "name": detail.get("name"),
        "index": int(detail["index"]),
        "shape": np.asarray(detail["shape"]).tolist(),
        "shape_signature": np.asarray(detail.get("shape_signature", detail["shape"])).tolist(),
        "dtype": np.dtype(detail["dtype"]).name,
        "quantization": list(detail.get("quantization", ())),
    }


def verify(cfg) -> dict:
    """Run real CPU inference and print the model's actual tensor contract."""
    from heartbeat.classifiers.yamnet import YamnetClassifier

    classifier = YamnetClassifier(cfg)
    print("Input tensors:")
    print(json.dumps([_tensor_detail(d) for d in classifier.interpreter.get_input_details()], indent=2))
    print("Output tensors:")
    print(json.dumps([_tensor_detail(d) for d in classifier.interpreter.get_output_details()], indent=2))
    started = time.perf_counter()
    scores, embedding = classifier.extract(np.zeros(CLIP_SAMPLES, dtype=np.float32))
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    candidates = [label for label in classifier.labels
                  if any(term in label.casefold() for term in ("knock", "tap", "thump", "bang", "wood", "hammer", "slap"))]
    result = {
        "class_scores": int(scores.size),
        "embedding_exists": embedding is not None,
        "embedding_size": 0 if embedding is None else int(embedding.size),
        "inference_ms": elapsed_ms,
        "target_candidates": candidates,
    }
    print(json.dumps(result, indent=2))
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--profile", default="rubble")
    parser.add_argument("--allow-network", action="store_true", help="confirm a human approved downloading the displayed public URLs")
    parser.add_argument("--verify-only", action="store_true", help="verify existing artifacts without any network request")
    parser.add_argument("--force", action="store_true", help="replace existing artifacts")
    parser.add_argument("--model-url", default=MODEL_URL, help="HTTPS URL for an approved alternative YAMNet TFLite model")
    args = parser.parse_args(argv)
    if args.verify_only and args.force:
        parser.error("--force cannot be combined with --verify-only")
    try:
        cfg = load_profile(args.profile).classifier
        model_dir = Path(cfg.model_dir)
        files = ((args.model_url, model_dir / "yamnet.tflite"),
                 (CLASS_MAP_URL, model_dir / "yamnet_class_map.csv"))
        pending = [(url, path) for url, path in files if args.force or not path.is_file()]
        if not args.verify_only and pending:
            for url, path in pending:
                print(f"Download: {url} -> {path}", flush=True)
            if not args.allow_network:
                print("Network access requires human approval. Review the URLs, then rerun with --allow-network.", file=sys.stderr)
                return 2
            settings = cfg.evaluation
            for url, path in pending:
                download(url, path, timeout_s=float(settings["download_timeout_s"]),
                         max_bytes=int(settings["download_max_bytes"]))
        verify(cfg)
    except (ImportError, OSError, ValueError, RuntimeError, urllib.error.URLError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        print("Install the ml extra for CPU inference. If the primary URL failed, obtain an approved YAMNet TFLite classification model from google/yamnet on Kaggle Models and pass --model-url.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
