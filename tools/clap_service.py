# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = ["torch>=2.6,<3", "transformers>=4.45,<5", "numpy", "scipy", "pyyaml", "fastapi", "uvicorn"]
# ///
"""Optional CLAP inference process. No mic, motors, file recording, or dialog.

Mac: uv run --python 3.12 --script tools/clap_service.py --profile profiles/clap.yaml
Jetson: run with the JetPack-compatible torch environment; see docs/CLAP.md.
Model downloads require --allow-download; cached weights work offline.
"""

import argparse
from hashlib import sha256
from pathlib import Path
import sys
import threading
import time
from urllib.parse import urlsplit

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from heartbeat.clap_settings import BODY_BYTES, CLIP_SAMPLES, PROTOCOL, SAMPLE_RATE, validate_clap


class BusyError(RuntimeError):
    pass


class ClapEngine:
    def __init__(self, settings, *, allow_download=False):
        import torch
        from transformers import ClapModel, ClapProcessor

        self.settings = validate_clap(settings)
        self.labels = settings["positive_prompts"] + settings["negative_prompts"]
        self._lock = threading.Lock()
        self._torch = torch
        torch.set_num_threads(settings["cpu_threads"])
        self.device = settings["device"]
        if self.device.startswith("cuda") and not torch.cuda.is_available():
            raise RuntimeError("CUDA is unavailable. Install PyTorch matching your JetPack, or select device: cpu")
        options = {"revision": settings["revision"], "local_files_only": not allow_download}
        self.processor = ClapProcessor.from_pretrained(settings["model"], **options)
        self.model = ClapModel.from_pretrained(settings["model"], **options).to(self.device).eval()
        with torch.inference_mode():
            tokens = self.processor.tokenizer(self.labels, padding=True, return_tensors="pt").to(self.device)
            self.text_features = torch.nn.functional.normalize(self.model.get_text_features(**tokens), dim=-1)
        # Warm up before declaring readiness so the first real impact does not
        # incur model initialization. No hardware is involved.
        self.predict(np.zeros(CLIP_SAMPLES, dtype="<f4").tobytes())

    def identity(self):
        return {"protocol": PROTOCOL, "model": self.settings["model"], "revision": self.settings["revision"],
                "labels": self.labels, "sample_rate": SAMPLE_RATE, "clip_samples": CLIP_SAMPLES}

    def predict(self, body):
        from scipy.signal import resample_poly

        if len(body) != BODY_BYTES:
            raise ValueError(f"Expected exactly {BODY_BYTES} PCM bytes")
        audio = np.frombuffer(body, dtype="<f4")
        if not np.isfinite(audio).all() or np.max(np.abs(audio)) > 1:
            raise ValueError("PCM samples must be finite and in [-1, 1]")
        if not self._lock.acquire(blocking=False):
            raise BusyError("CLAP is busy; no requests are silently queued")
        try:
            started = time.perf_counter()
            target_rate = self.processor.feature_extractor.sampling_rate
            import math
            divisor = math.gcd(target_rate, SAMPLE_RATE)
            audio = resample_poly(audio, target_rate // divisor, SAMPLE_RATE // divisor).astype(np.float32)
            inputs = self.processor.feature_extractor(audio, sampling_rate=target_rate,
                                                     return_tensors="pt").to(self.device)
            with self._torch.inference_mode():
                features = self._torch.nn.functional.normalize(self.model.get_audio_features(**inputs), dim=-1)
                logits = self.model.logit_scale_a.exp() * features @ self.text_features.T
                scores = logits[0].softmax(dim=-1).cpu().tolist()
            if not np.isfinite(scores).all():
                raise ValueError("CLAP produced non-finite scores")
            return {**self.identity(), "clip_sha256": sha256(body).hexdigest(), "scores": scores,
                    "inference_ms": (time.perf_counter() - started) * 1000}
        finally:
            self._lock.release()


def create_app(engine):
    from fastapi import FastAPI, HTTPException, Request
    from starlette.concurrency import run_in_threadpool

    app = FastAPI(title="Heartbeat CLAP", docs_url=None, redoc_url=None)

    @app.get("/health")
    def health():
        return engine.identity()

    @app.post("/classify")
    async def classify(request: Request):
        if request.headers.get("content-type") != "application/octet-stream":
            raise HTTPException(415, "Send little-endian float32 PCM as application/octet-stream")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > BODY_BYTES:
                raise HTTPException(413, "Audio exceeds the fixed onset-clip size")
        if len(body) != BODY_BYTES:
            raise HTTPException(422, "Audio must contain exactly 15600 float32 samples")
        try:
            return await run_in_threadpool(engine.predict, bytes(body))
        except BusyError as exc:
            raise HTTPException(429, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    return app


def main():
    import yaml
    import uvicorn

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, default=Path("profiles/clap.yaml"))
    parser.add_argument("--allow-download", action="store_true")
    parser.add_argument("--host", default="127.0.0.1", help="Bind interface; use 0.0.0.0 only on a trusted LAN")
    args = parser.parse_args()
    settings = validate_clap(yaml.safe_load(args.profile.read_text())["classifier"]["clap"])
    endpoint = urlsplit(settings["endpoint"])
    print(f"Loading and warming CLAP on {settings['device']}; text features are computed once...", flush=True)
    engine = ClapEngine(settings, allow_download=args.allow_download)
    print("CLAP ready. Relative prompt scores are uncalibrated; validate short clips before use.", flush=True)
    uvicorn.run(create_app(engine), host=args.host, port=endpoint.port or 80, workers=1)


if __name__ == "__main__":
    main()
