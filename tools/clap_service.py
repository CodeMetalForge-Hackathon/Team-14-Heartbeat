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
from heartbeat.audio_monitor import monitor_identity, decode_observation


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
        self.monitor_cfg = settings.get("monitor")
        self.transcriber = None
        if self.monitor_cfg and self.monitor_cfg["enabled"]:
            labels = [p for group in self.monitor_cfg["prompts"].values() for p in group]
            with torch.inference_mode():
                tokens = self.processor.tokenizer(labels, padding=True, return_tensors="pt").to(self.device)
                self.monitor_features = torch.nn.functional.normalize(self.model.get_text_features(**tokens), dim=-1)
            asr = self.monitor_cfg["asr"]
            if asr["enabled"]:
                from transformers import AutoModelForSpeechSeq2Seq, AutoProcessor, pipeline
                options = {"revision": asr["revision"], "local_files_only": not allow_download}
                processor = AutoProcessor.from_pretrained(asr["model"], **options)
                model = AutoModelForSpeechSeq2Seq.from_pretrained(asr["model"], **options).eval()
                self.transcriber = pipeline("automatic-speech-recognition", model=model,
                                            tokenizer=processor.tokenizer,
                                            feature_extractor=processor.feature_extractor, device=self.device)
        # Warm up before declaring readiness so the first real impact does not
        # incur model initialization. No hardware is involved.
        self.predict(np.zeros(CLIP_SAMPLES, dtype="<f4").tobytes())

    def identity(self):
        identity = {"protocol": PROTOCOL, "model": self.settings["model"], "revision": self.settings["revision"],
                    "labels": self.labels, "sample_rate": SAMPLE_RATE, "clip_samples": CLIP_SAMPLES}
        if self.monitor_cfg and self.monitor_cfg["enabled"]:
            identity["monitor"] = monitor_identity(self.monitor_cfg)
        return identity

    def predict(self, body):
        return self._predict(body, monitor=False)

    def observe(self, body):
        return self._predict(body, monitor=True)

    def _predict(self, body, *, monitor):
        from scipy.signal import resample_poly

        expected = monitor_identity(self.monitor_cfg)["window_samples"] * 4 if monitor else BODY_BYTES
        if len(body) != expected:
            raise ValueError(f"Expected exactly {expected} PCM bytes")
        audio = np.frombuffer(body, dtype="<f4")
        if not np.isfinite(audio).all() or np.max(np.abs(audio)) > 1:
            raise ValueError("PCM samples must be finite and in [-1, 1]")
        if not self._lock.acquire(blocking=False):
            raise BusyError("CLAP is busy; no requests are silently queued")
        try:
            started = time.perf_counter()
            original = audio.copy()
            target_rate = self.processor.feature_extractor.sampling_rate
            import math
            divisor = math.gcd(target_rate, SAMPLE_RATE)
            audio = resample_poly(audio, target_rate // divisor, SAMPLE_RATE // divisor).astype(np.float32)
            inputs = self.processor.feature_extractor(audio, sampling_rate=target_rate,
                                                     return_tensors="pt").to(self.device)
            with self._torch.inference_mode():
                features = self._torch.nn.functional.normalize(self.model.get_audio_features(**inputs), dim=-1)
                text_features = self.monitor_features if monitor else self.text_features
                logits = self.model.logit_scale_a.exp() * features @ text_features.T
                scores = logits[0].softmax(dim=-1).cpu().tolist()
            if not np.isfinite(scores).all():
                raise ValueError("CLAP produced non-finite scores")
            result = {**self.identity(), "clip_sha256": sha256(body).hexdigest(), "scores": scores}
            if monitor:
                result["transcript"] = ""
                observation = decode_observation(self.monitor_cfg, result, body)
                # Transcribe only sufficiently loud windows classified as vocals.
                result["transcript"] = self._transcribe(original, observation["label"])
            result["inference_ms"] = (time.perf_counter() - started) * 1000
            return result
        finally:
            self._lock.release()

    def _transcribe(self, audio, label):
        rms = float(np.sqrt(np.mean(audio.astype(float) ** 2)))
        if (self.transcriber and label in {"SCREAM", "SHOUT", "CRYING", "SPEECH"}
                and 20 * np.log10(max(rms, 1e-12)) >= self.monitor_cfg["min_dbfs"]):
            return self.transcriber({"raw": audio, "sampling_rate": SAMPLE_RATE},
                                    generate_kwargs={"do_sample": False})["text"].strip()
        return ""


def create_app(engine):
    from fastapi import FastAPI, HTTPException, Request
    from starlette.concurrency import run_in_threadpool

    app = FastAPI(title="Heartbeat CLAP", docs_url=None, redoc_url=None)

    @app.get("/health")
    def health():
        return engine.identity()

    @app.post("/classify")
    async def classify(request: Request):
        return await handle(request, BODY_BYTES, engine.predict)

    @app.post("/observe")
    async def observe(request: Request):
        cfg = engine.identity().get("monitor")
        if not cfg:
            raise HTTPException(409, "Restart service with a monitor-enabled profile")
        return await handle(request, cfg["window_samples"] * 4, engine.observe)

    async def handle(request, expected_bytes, predict):
        if request.headers.get("content-type") != "application/octet-stream":
            raise HTTPException(415, "Send little-endian float32 PCM as application/octet-stream")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > expected_bytes:
                raise HTTPException(413, "Audio exceeds the fixed onset-clip size")
        if len(body) != expected_bytes:
            raise HTTPException(422, f"Audio must contain exactly {expected_bytes // 4} float32 samples")
        try:
            return await run_in_threadpool(predict, bytes(body))
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
