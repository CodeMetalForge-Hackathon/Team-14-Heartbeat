"""Lazy TFLite YAMNet inference with class-name lookup and CPU timing."""

from __future__ import annotations

import csv
from hashlib import sha256
import importlib
import logging
from pathlib import Path
import sys
from time import perf_counter

import numpy as np

from ..classify import Detection
from ..onset import CLIP_SAMPLES

CLASS_COUNT = 521  # YAMNet's published AudioSet output contract.
_LOG = logging.getLogger(__name__)


def _interpreter_class():
    """Prefer LiteRT, supporting the smaller legacy runtime when installed."""
    errors = []
    for module in ("ai_edge_litert.interpreter", "tflite_runtime.interpreter"):
        try:
            return importlib.import_module(module).Interpreter
        except (ImportError, OSError) as exc:
            errors.append(f"{module}: {exc}")
    raise RuntimeError("No CPU TFLite interpreter available (" + "; ".join(errors) + ")")


def _labels(path: Path) -> list[str]:
    rows = {}
    with path.open(newline="", encoding="utf-8-sig") as stream:
        reader = csv.DictReader(stream)
        if not {"index", "display_name"}.issubset(reader.fieldnames or ()):
            raise ValueError(f"Invalid YAMNet class map: {path}")
        for row in reader:
            index = int(row["index"])
            if index in rows or not row["display_name"].strip():
                raise ValueError("YAMNet class map contains duplicate or empty labels")
            rows[index] = row["display_name"].strip()
    if set(rows) != set(range(CLASS_COUNT)):
        raise ValueError(f"YAMNet class map must contain all {CLASS_COUNT} indices")
    return [rows[index] for index in range(CLASS_COUNT)]


def _shape(detail):
    return tuple(int(size) for size in detail["shape"])


def _read_tensor(interpreter, detail):
    tensor = np.asarray(interpreter.get_tensor(int(detail["index"])))
    if np.issubdtype(tensor.dtype, np.integer):
        scale, zero = detail.get("quantization", (0.0, 0))
        if not scale:
            raise ValueError("Quantized YAMNet output lacks a scale")
        tensor = (tensor.astype(np.float64) - zero) * scale
    return np.asarray(tensor, dtype=np.float64)


class YamnetClassifier:
    name = "yamnet"

    def __init__(self, cfg, *, interpreter=None):
        self.cfg = cfg
        self.model_path = Path(cfg.model_dir) / "yamnet.tflite"
        self.model_sha256 = sha256(self.model_path.read_bytes()).hexdigest() if self.model_path.is_file() else None
        self.labels = _labels(Path(cfg.model_dir) / "yamnet_class_map.csv")
        indices = {name.casefold(): index for index, name in enumerate(self.labels)}
        targets = tuple(cfg.targets)
        missing = [name for name in targets if name.casefold() not in indices]
        if missing:
            _LOG.warning("YAMNet target names absent from class map: %s", ", ".join(missing))
        self.target_indices = tuple(indices[name.casefold()] for name in targets if name.casefold() in indices)
        if not self.target_indices:
            raise ValueError("None of the configured knock targets exists in the YAMNet class map")
        if interpreter is None:
            if not self.model_path.is_file():
                raise FileNotFoundError(self.model_path)
            interpreter = _interpreter_class()(model_path=str(self.model_path), num_threads=int(cfg.num_threads))
        self.interpreter = interpreter
        interpreter.allocate_tensors()
        inputs = interpreter.get_input_details()
        if len(inputs) != 1 or _shape(inputs[0]) not in ((CLIP_SAMPLES,), (1, CLIP_SAMPLES)):
            raise ValueError(f"YAMNet must accept exactly {CLIP_SAMPLES} waveform samples")
        self.input_detail = inputs[0]
        if np.dtype(self.input_detail["dtype"]) != np.dtype(np.float32):
            raise ValueError("The YAMNet float32 model must accept float32 waveform input")
        outputs = interpreter.get_output_details()
        scores = [detail for detail in outputs if _shape(detail) and _shape(detail)[-1] == CLASS_COUNT]
        if len(scores) != 1:
            raise ValueError(f"YAMNet must expose one {CLASS_COUNT}-class score tensor")
        self.score_detail = scores[0]
        if any(size <= 0 for size in _shape(self.score_detail)):
            raise ValueError("YAMNet score tensor shape must be concrete after allocation")
        # The waveform model can also emit a 2-D spectrogram. Embeddings have
        # a wider feature axis than scores or one vector in a rank <= 2 tensor.
        candidates = [
            detail for detail in outputs
            if detail["index"] != self.score_detail["index"] and _shape(detail)
            and len(_shape(detail)) <= 2
            and (_shape(detail)[-1] > CLASS_COUNT or len(_shape(detail)) == 1 or _shape(detail)[0] == 1)
        ]
        named = [detail for detail in candidates if "embedding" in detail.get("name", "").casefold()]
        if len(named) == 1:
            self.embedding_detail = named[0]
        elif len(candidates) <= 1:
            self.embedding_detail = candidates[0] if candidates else None
        else:
            raise ValueError("Ambiguous YAMNet embedding outputs")
        self.feature_spec = {
            "class_scores": CLASS_COUNT,
            "embedding": _shape(self.embedding_detail)[-1] if self.embedding_detail is not None else 0,
        }
        self.inference_ms = 0.0

    def score_scores(self, scores) -> float:
        values = np.asarray(scores, dtype=np.float64)
        if values.shape != (CLASS_COUNT,) or not np.all(np.isfinite(values)):
            raise ValueError("Invalid YAMNet class scores")
        return float(np.clip(np.max(values[list(self.target_indices)]), 0, 1))

    def extract(self, clip) -> tuple[np.ndarray, np.ndarray | None]:
        """Return mean class scores and optional mean embedding per clip."""
        waveform = np.asarray(clip, dtype=np.float32)
        if waveform.shape != (CLIP_SAMPLES,) or not np.all(np.isfinite(waveform)):
            raise ValueError(f"YAMNet requires a finite mono clip of {CLIP_SAMPLES} samples")
        waveform = np.ascontiguousarray(waveform.reshape(_shape(self.input_detail)))
        started = perf_counter()
        self.interpreter.set_tensor(int(self.input_detail["index"]), waveform)
        self.interpreter.invoke()
        raw_scores = _read_tensor(self.interpreter, self.score_detail)
        if not raw_scores.size or raw_scores.shape[-1] != CLASS_COUNT:
            raise ValueError("YAMNet score output shape changed during inference")
        scores = raw_scores.reshape(-1, CLASS_COUNT).mean(axis=0)
        embedding = None
        if self.embedding_detail is not None:
            raw_embedding = _read_tensor(self.interpreter, self.embedding_detail)
            width = self.feature_spec["embedding"]
            if not raw_embedding.size or raw_embedding.shape[-1] != width:
                raise ValueError("YAMNet embedding shape changed during inference")
            embedding = raw_embedding.reshape(-1, width).mean(axis=0)
        self.inference_ms = (perf_counter() - started) * 1000
        if not np.all(np.isfinite(scores)) or (embedding is not None and not np.all(np.isfinite(embedding))):
            raise ValueError("YAMNet produced non-finite output")
        if np.any(scores < 0) or np.any(scores > 1):
            raise ValueError("YAMNet scores must lie in [0, 1]")
        print(f"YAMNet CPU inference: {self.inference_ms:.3f} ms/clip", file=sys.stderr)
        return scores, embedding

    def classify(self, onset) -> Detection:
        scores, _ = self.extract(onset.clip)
        score = self.score_scores(scores)
        top = [[self.labels[int(index)], float(scores[index])] for index in np.argsort(-scores, kind="stable")[:5]]
        return Detection(
            t=float(onset.t), peak=float(onset.peak), snr_db=float(onset.snr_db),
            gated=bool(onset.gated), clip=onset.clip, score=score,
            is_knock=bool(score >= self.cfg.threshold), classifier=self.name, top=top,
        )
