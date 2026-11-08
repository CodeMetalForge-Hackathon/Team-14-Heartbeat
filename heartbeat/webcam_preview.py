"""Browser webcam snapshots for local Decisions preview; no rover interface."""

from __future__ import annotations

import asyncio
import base64
import binascii
from contextlib import asynccontextmanager
import json
import math
import secrets
from time import monotonic

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
import uvicorn

from .camera import CameraFrame
from .decisions import DecisionsClient, DecisionsError


_PAGE = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Heartbeat · Webcam decisions</title>
<style>
:root { color-scheme:dark; font-family:system-ui,sans-serif; background:#101820; color:#eaf0f4; }
body { max-width:1080px; margin:0 auto; padding:28px 20px; }
h1 { margin:8px 0; font-size:30px; } p { color:#bccbd5; line-height:1.55; }
.badge { color:#99e0c2; letter-spacing:.08em; font-size:13px; font-weight:700; }
.grid { display:grid; grid-template-columns:1fr 1fr; gap:20px; margin:20px 0; }
.card { background:#19252f; border:1px solid #314352; border-radius:12px; padding:18px; }
video,img { display:block; width:100%; aspect-ratio:4/3; object-fit:contain; background:#080d12; border-radius:8px; }
img[hidden] { display:none; } h2 { font-size:16px; margin:0 0 12px; }
.controls { display:flex; flex-wrap:wrap; gap:12px; align-items:center; margin:16px 0; }
button,input { font:inherit; border-radius:7px; border:1px solid #586d7d; padding:10px 12px; }
button { background:#a8eccd; color:#10251d; font-weight:650; cursor:pointer; }
button.secondary { background:#243642; color:#eaf0f4; } button:disabled { opacity:.45; cursor:default; }
input[type=number] { width:85px; background:#101820; color:#eaf0f4; }
label { display:flex; align-items:center; gap:8px; } #action { font-size:28px; font-weight:700; }
#status { min-height:1.5em; } .error { color:#ffc4ba; } table { width:100%; border-collapse:collapse; margin-top:12px; }
td { padding:8px 0; border-bottom:1px solid #314352; } td:last-child { text-align:right; }
small { color:#bccbd5; } @media(max-width:700px) { .grid { grid-template-columns:1fr; } }
</style></head><body>
<div class="badge">HEARTBEAT · MOVEMENT DISABLED</div>
<h1>Look. Choose. Review.</h1>
<p>Use your webcam to preview the next action. Capture &amp; decide sends one image to OpenAI.
The rover and microphone are not connected to this page.</p>
<div class="controls">
<label>Example distance <input id="front" type="number" min="2" max="400" step="1"> cm</label>
<label><input id="turns" type="checkbox"> Include turn choices</label>
</div>
<div class="grid">
<section class="card"><h2>Live camera</h2><video id="video" autoplay muted playsinline></video>
<div class="controls"><button id="start">Start camera</button><button id="decide" disabled>Capture &amp; decide</button>
<button id="stop" class="secondary" disabled>Stop camera</button></div>
<p id="status" role="status" aria-live="polite">Start the camera and allow browser access.</p></section>
<section class="card"><h2>Frame sent</h2><img id="frame" alt="The exact camera frame used for the displayed decision" hidden>
<p id="action">No decision yet</p><p id="timing"></p><table><tbody id="scores"></tbody></table>
<small>Distance is an example value. Decisions are recommendations only; no movement is executed.</small></section>
</div>
<script>
const config = __CONFIG__;
const byId = id => document.getElementById(id);
const video = byId('video');
let stream = null, pending = null, busy = false, starting = false, generation = 0;
byId('front').value = config.front_cm;
byId('turns').checked = config.allow_turns;
function setStatus(message, error=false) { byId('status').textContent=message; byId('status').className=error?'error':''; }
function controls() {
  byId('start').disabled = !!stream || starting;
  byId('decide').disabled = !stream || busy || video.readyState < 2;
  byId('stop').disabled = !stream && !starting;
}
function clearResult(message) {
  byId('action').textContent=message; byId('timing').textContent=''; byId('scores').replaceChildren();
}
function stopCamera() {
  if (busy) clearResult('Decision display canceled');
  generation++; pending?.abort(); pending=null; busy=false; starting=false;
  if (stream) stream.getTracks().forEach(track=>track.stop());
  stream=null; video.srcObject=null; controls(); setStatus('Camera stopped. No movement was executed.');
}
byId('start').addEventListener('click', async () => {
  const current=++generation; starting=true; controls(); setStatus('Waiting for camera permission…');
  try {
    if (!navigator.mediaDevices?.getUserMedia) throw new Error('Open this localhost page in Safari or Chrome with camera access enabled.');
    const opened=await navigator.mediaDevices.getUserMedia({video:{width:{ideal:config.width},height:{ideal:config.height}},audio:false});
    if (current!==generation) { opened.getTracks().forEach(track=>track.stop()); return; }
    stream=opened; video.srcObject=opened;
    opened.getVideoTracks().forEach(track=>track.addEventListener('ended',()=>{
      if (stream===opened) { stopCamera(); setStatus('Camera disconnected. Start it again to continue.',true); }
    }));
    await video.play();
    if (current!==generation) return;
    setStatus('Point the camera at the scene, then capture a decision.');
  } catch (error) {
    if (current!==generation) return;
    stopCamera();
    setStatus(error.name==='NotAllowedError'?'Camera access was denied. Allow this browser in macOS Privacy & Security → Camera and in the site permissions.':
      error.name==='NotFoundError'?'No webcam was found. Connect a camera and try again.':
      'Camera unavailable. Close other camera apps and use this localhost page in Safari or Chrome.',true);
  } finally { if (current===generation) { starting=false; controls(); } }
});
video.addEventListener('loadeddata', controls);
byId('stop').addEventListener('click', stopCamera);
window.addEventListener('pagehide', stopCamera);
byId('decide').addEventListener('click', async () => {
  if (!stream || busy || video.readyState < 2) return;
  const front=Number(byId('front').value);
  if (!Number.isFinite(front) || front<2 || front>400) { setStatus('Enter an example distance from 2 to 400 cm.',true); return; }
  const current=generation, request=new AbortController(); pending=request; busy=true; controls();
  const canvas=document.createElement('canvas');
  const scale=Math.min(config.width/video.videoWidth,config.height/video.videoHeight,1);
  canvas.width=Math.max(1,Math.round(video.videoWidth*scale)); canvas.height=Math.max(1,Math.round(video.videoHeight*scale));
  clearResult('Choosing…'); setStatus('Sending one frame. Waiting for a decision…');
  try {
    canvas.getContext('2d').drawImage(video,0,0,canvas.width,canvas.height);
    const image=canvas.toDataURL('image/jpeg',config.jpeg_quality);
    byId('frame').src=image; byId('frame').hidden=false;
    const response=await fetch('/api/decide',{method:'POST',signal:request.signal,
      headers:{'Content-Type':'application/json','X-Preview-Token':config.token},
      body:JSON.stringify({image,front_cm:front,allow_turns:byId('turns').checked})});
    const result=await response.json();
    if (current!==generation) return;
    if (!response.ok) throw new Error(typeof result.detail==='string'?result.detail:'Preview request failed.');
    byId('action').textContent=result.action.replaceAll('_',' ');
    byId('timing').textContent=`API ${result.api_latency_s.toFixed(2)} seconds · example range ${result.example_front_cm} cm · no movement`;
    for (const [action,probability] of Object.entries(result.probabilities)) {
      const row=document.createElement('tr'), label=document.createElement('td'), score=document.createElement('td');
      label.textContent=action.replaceAll('_',' '); score.textContent=`${(probability*100).toFixed(1)}%`;
      row.append(label,score); byId('scores').append(row);
    }
    setStatus('Decision displayed. Capture again for a new observation.');
  } catch (error) {
    if (current===generation && error.name!=='AbortError') { clearResult('No decision'); setStatus(error.message || 'Preview request failed.',true); }
  } finally { if (pending===request) { pending=null; busy=false; controls(); } }
});
</script></body></html>'''


def _positive(value, name, *, integer=False):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value <= 0
            or integer and not isinstance(value, int)):
        raise ValueError(f"webcam_preview.{name} must be a positive {'integer' if integer else 'number'}")
    return value


def run_webcam_preview(settings, navigation, *, front_cm, allow_turns, port=None):
    """Serve on loopback; the user grants camera access in their own browser."""
    cfg = settings["webcam_preview"]
    port = cfg["port"] if port is None else port
    _positive(port, "port", integer=True)
    if port > 65535:
        raise ValueError("Webcam preview port must be between 1 and 65535")
    for name in ("width", "height", "max_request_bytes"):
        _positive(cfg[name], name, integer=True)
    _positive(cfg["shutdown_timeout_s"], "shutdown_timeout_s")
    quality = _positive(cfg["jpeg_quality"], "jpeg_quality")
    if quality > 1:
        raise ValueError("webcam_preview.jpeg_quality must be at most 1")
    client = DecisionsClient(settings)
    token = secrets.token_urlsafe()
    public = {"token": token, "front_cm": front_cm, "allow_turns": allow_turns,
              "width": cfg["width"], "height": cfg["height"], "jpeg_quality": quality}
    page = _PAGE.replace("__CONFIG__", json.dumps(public, allow_nan=False).replace("<", "\\u003c"))
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    origins = {"http://" + host for host in hosts}

    @asynccontextmanager
    async def lifespan(_app):
        try:
            yield
        finally:
            client.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        if request.headers.get("host") not in hosts:
            return JSONResponse({"detail": "Use the localhost preview URL."}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Permissions-Policy"] = "camera=(self), microphone=()"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return page

    @app.post("/api/decide")
    async def decide(request: Request):
        if (request.headers.get("origin") not in origins
                or not secrets.compare_digest(request.headers.get("x-preview-token", ""), token)):
            raise HTTPException(403, "Open the local preview page to capture a decision.")
        if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
            raise HTTPException(415, "Expected a JSON webcam snapshot.")
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > cfg["max_request_bytes"]:
                raise HTTPException(413, "Camera image is too large; reduce the preview resolution.")
            body.extend(chunk)
        try:
            data = json.loads(body)
            if not isinstance(data, dict) or set(data) != {"image", "front_cm", "allow_turns"}:
                raise ValueError
            front, turns, image = data["front_cm"], data["allow_turns"], data["image"]
            if (isinstance(front, bool) or not isinstance(front, (int, float))
                    or not math.isfinite(front) or not 2 <= front <= 400
                    or not isinstance(turns, bool) or not isinstance(image, str)
                    or not image.startswith("data:image/jpeg;base64,")):
                raise ValueError
            jpeg = base64.b64decode(image.split(",", 1)[1], validate=True)
            if (len(jpeg) > settings["max_frame_bytes"] or not jpeg.startswith(b"\xff\xd8")
                    or not jpeg.endswith(b"\xff\xd9")):
                raise ValueError
        except (ValueError, TypeError, UnicodeError, OverflowError, RecursionError, binascii.Error):
            raise HTTPException(400, "Expected a bounded JPEG, example range 2–400 cm, and a turn-choice flag.") from None
        choices = ["HOLD", "STOP_AND_LISTEN"]
        if front >= navigation["clear_cm"]:
            choices.append("FORWARD_SHORT")
        if turns and front >= navigation["turn_clearance_cm"]:
            choices.extend(["TURN_LEFT_SHORT", "TURN_RIGHT_SHORT"])
        telemetry = {"mode": "webcam_preview", "image_source": "browser_webcam_snapshot",
                     "sensor_source": "operator_supplied_example", "front_cm": front,
                     "moving": False, "armed": False, "allow_turns": turns,
                     "clear_cm": navigation["clear_cm"],
                     "turn_clearance_cm": navigation["turn_clearance_cm"], "steps": 0, "history": []}
        # This is receipt time, not exposure time; no motion is authorized here.
        frame = CameraFrame(jpeg, monotonic(), 1)
        try:
            result = await asyncio.to_thread(client.choose, frame, telemetry, choices)
        except DecisionsError as exc:
            raise HTTPException(502, str(exc)) from None
        return {"mode": "webcam_preview", "sensor_source": "operator_supplied_example",
                "example_front_cm": front, "motion_executed": False,
                "action": result.action, "confidence": result.confidence,
                "probabilities": dict(result.probabilities), "api_latency_s": result.latency_s}

    print(f"Webcam Decisions preview: http://127.0.0.1:{port}/", flush=True)
    print("Open in Safari or Chrome, start the camera, then Capture & decide. Ctrl-C closes the server.", flush=True)
    try:
        uvicorn.run(app, host="127.0.0.1", port=port, access_log=False,
                    log_level="warning", timeout_graceful_shutdown=cfg["shutdown_timeout_s"])
    finally:
        client.close()
