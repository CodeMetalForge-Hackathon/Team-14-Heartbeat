"""Offline dashboard API with bounded, nonblocking event delivery.

``seq`` here counts live bus events, including every meter event. It is not the
black-box hash-chain sequence, which throttles meter events. The black-box
writer's own counts are exposed separately in ``/api/state``.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Mapping
from concurrent.futures import Future
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass, fields
import inspect
import json
import math
from pathlib import Path
import queue
import threading

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .events import Event, to_jsonable


@dataclass(frozen=True)
class ServerConfig:
    host: str
    port: int
    sentinel_name: str
    runs_dir: str
    event_history: int
    client_queue: int
    bridge_queue_events: int
    request_timeout_s: float
    events_default_limit: int
    events_max_limit: int
    dashboard_sim_duration_s: float
    blackbox: dict
    dashboard: dict

    @classmethod
    def from_dict(cls, value=None):
        if isinstance(value, cls):
            return value
        value = getattr(value, "server", value)
        if value is not None and not isinstance(value, Mapping):
            raise ValueError("server configuration must be a mapping")
        from .config import load_profile

        data = dict(load_profile("rubble").server)
        for name, item in (value or {}).items():
            data[name] = {**data[name], **item} if name in {"blackbox", "dashboard"} and isinstance(item, dict) else item
        expected = {item.name for item in fields(cls)}
        unknown, missing = data.keys() - expected, expected - data.keys()
        if unknown:
            raise ValueError(f"Unknown server keys: {', '.join(sorted(map(str, unknown)))}")
        if missing:
            raise ValueError(f"Missing server keys: {', '.join(sorted(missing))}")
        for name in ("host", "sentinel_name", "runs_dir"):
            if not isinstance(data[name], str) or not data[name].strip():
                raise ValueError(f"server.{name} must be a nonempty string")
        for name in ("port", "event_history", "client_queue", "bridge_queue_events",
                     "events_default_limit", "events_max_limit"):
            if isinstance(data[name], bool) or not isinstance(data[name], int) or data[name] < 1:
                raise ValueError(f"server.{name} must be a positive integer")
        if data["port"] > 65535:
            raise ValueError("server.port must be at most 65535")
        for name in ("request_timeout_s", "dashboard_sim_duration_s"):
            number = data[name]
            if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number) or number <= 0:
                raise ValueError(f"server.{name} must be positive and finite")
        if data["events_default_limit"] > data["events_max_limit"]:
            raise ValueError("server.events_default_limit must not exceed events_max_limit")
        for name in ("blackbox", "dashboard"):
            if not isinstance(data[name], dict):
                raise ValueError(f"server.{name} must be a mapping")
        dashboard = data["dashboard"]
        dashboard_keys = {
            "trace_seconds", "event_log_limit", "reconnect_ms", "state_refresh_ms",
            "trace_min_dbfs", "trace_max_dbfs", "trace_grid_db", "trace_grid_s",
        }
        if set(dashboard) != dashboard_keys:
            raise ValueError("server.dashboard must contain only the documented UI settings")
        for name, number in dashboard.items():
            if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number):
                raise ValueError(f"server.dashboard.{name} must be finite")
            if name not in {"trace_min_dbfs", "trace_max_dbfs"} and number <= 0:
                raise ValueError(f"server.dashboard.{name} must be positive")
            if name in {"event_log_limit", "reconnect_ms", "state_refresh_ms"} and not isinstance(number, int):
                raise ValueError(f"server.dashboard.{name} must be an integer")
        if dashboard["trace_min_dbfs"] >= dashboard["trace_max_dbfs"]:
            raise ValueError("server.dashboard.trace_min_dbfs must be below trace_max_dbfs")
        return cls(**deepcopy(data))


class _StateCache:
    def __init__(self):
        self._lock = threading.Lock()
        self._snapshot = {}
        self._order = -1
        self._approval_order = -1
        self._approved_by = None
        self._patrol_snapshot = {}
        self._patrol_order = -1

    def update(self, snapshot, order):
        with self._lock:
            if order >= self._order:
                self._snapshot = deepcopy(snapshot)
                self._order = order
                if not snapshot.get("approved"):
                    self._approved_by = None
                    self._approval_order = order

    def approval(self, operator, order):
        with self._lock:
            if order >= self._approval_order:
                self._approved_by = operator
                self._approval_order = order

    def get(self):
        with self._lock:
            return deepcopy(self._snapshot), self._approved_by, self._order

    def update_patrol(self, snapshot, order):
        with self._lock:
            if order >= self._patrol_order:
                self._patrol_snapshot = deepcopy(snapshot)
                self._patrol_order = order

    def get_patrol(self):
        with self._lock:
            return deepcopy(self._patrol_snapshot)


@dataclass(eq=False)
class _Client:
    queue: asyncio.Queue
    after: int
    dropped: int = 0


class EventBridge:
    """Only enqueue on bus threads; serialize and broadcast on the ASGI loop."""

    def __init__(self, bus, cfg, *, cache=None):
        self.bus = bus
        self.cfg = ServerConfig.from_dict(cfg)
        self.cache = cache if cache is not None else _StateCache()
        self._incoming = queue.Queue(maxsize=self.cfg.bridge_queue_events)
        self._history = deque(maxlen=self.cfg.event_history)
        self._clients: set[_Client] = set()
        self._lock = threading.Lock()
        self._issued = 0
        self._scheduled = False
        self._loop = None
        self._unsubscribe = None
        self._closed = False
        self.latest_seq = 0
        self.dropped_ingress = 0
        self.dropped_clients = 0

    @property
    def published_seq(self):
        with self._lock:
            return self._issued

    def start(self):
        self._loop = asyncio.get_running_loop()
        self._unsubscribe = self.bus.subscribe(self)

    def __call__(self, event: Event):
        with self._lock:
            if self._closed:
                return
            self._issued += 1
            number = self._issued
            try:
                self._incoming.put_nowait((number, event))
            except queue.Full:
                try:
                    self._incoming.get_nowait()
                    self.dropped_ingress += 1
                except queue.Empty:
                    pass
                self._incoming.put_nowait((number, event))
            if self._scheduled:
                return
            self._scheduled = True
            loop = self._loop
        if loop is not None:
            try:
                loop.call_soon_threadsafe(self._drain)
            except RuntimeError:
                pass  # The event loop has already shut down.

    def _drain(self):
        # A bounded batch yields to HTTP and WebSocket tasks even if a fast
        # replay keeps producing events while this callback is running.
        for _ in range(self.cfg.bridge_queue_events):
            try:
                number, event = self._incoming.get_nowait()
            except queue.Empty:
                break
            payload = {"seq": number, **event.to_dict()}
            self._history.append(payload)
            self.latest_seq = number
            if event.type == "state":
                self.cache.update(payload["data"], number)
            elif event.type == "approval" and "gap" not in payload["data"]:
                self.cache.approval(payload["data"].get("operator"), number)
            elif event.type == "patrol":
                self.cache.update_patrol(payload["data"], number)
            for client in tuple(self._clients):
                if number <= client.after:
                    continue
                client.after = number
                if client.queue.full():
                    client.queue.get_nowait()
                    client.dropped += 1
                    self.dropped_clients += 1
                client.queue.put_nowait(payload)
        with self._lock:
            if self._incoming.empty():
                self._scheduled = False
                return
        self._loop.call_soon(self._drain)

    def history(self, since, limit):
        events = [event for event in self._history if event["seq"] > since][:limit]
        oldest = self._history[0]["seq"] if self._history else self.latest_seq + 1
        previous = since
        gap = since < oldest - 1
        for event in events:
            gap = gap or event["seq"] > previous + 1
            previous = event["seq"]
        return {
            "events": events, "latest_seq": self.latest_seq, "oldest_seq": oldest,
            "truncated": bool(gap), "dropped": self.dropped_ingress,
            "has_more": bool(events and events[-1]["seq"] < self.latest_seq),
        }

    def subscribe(self, since=None):
        # Registration and the history watermark share the same event-loop
        # turn. Events arriving during backfill are queued strictly after it.
        watermark = self.latest_seq
        client = _Client(asyncio.Queue(maxsize=self.cfg.client_queue), max(watermark, since or 0))
        self._clients.add(client)
        backfill = [] if since is None else [event for event in self._history if event["seq"] > since]
        return client, backfill

    def unsubscribe(self, client):
        self._clients.discard(client)

    def close(self):
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        with self._lock:
            self._closed = True
        self._drain()
        for client in tuple(self._clients):
            if client.queue.full():
                client.queue.get_nowait()
            client.queue.put_nowait(None)
        self._clients.clear()


async def _hook(callback):
    if callback is not None:
        result = callback()
        if inspect.isawaitable(result):
            await result


def create_app(pipeline, session, bus, *, cfg=None, run_id=None, profile=None,
               blackbox=None, dashboard_dir=None, on_start=None, on_stop=None, patrol=None):
    """Create an ASGI app; lifecycle hooks keep uvicorn on the main thread.

    ``session.command_result`` and ``session.engine.snapshot`` are invoked only
    by callbacks queued through ``pipeline.call_soon``. The final snapshot is
    captured by an ``on_end`` processing-thread hook for use after audio EOF.
    """
    cfg = ServerConfig.from_dict(cfg if cfg is not None else pipeline.profile.server)
    ui = {**cfg.dashboard, "events_limit": cfg.events_default_limit}
    cache = _StateCache()
    bridge = EventBridge(bus, cfg, cache=cache)
    if run_id is None:
        run_id = getattr(blackbox, "run_id", None)
        if run_id is None:
            directory = getattr(blackbox, "run_dir", None)
            run_id = Path(directory).name if directory is not None else "live"
    if profile is None:
        profile = pipeline.profile.dialog.get("profile", "rubble")
    elif hasattr(profile, "dialog"):
        profile = profile.dialog.get("profile", "rubble")
    profile = str(profile)

    def snapshot():
        if session.engine is None:
            raise RuntimeError("Dialog engine has not started")
        value = session.engine.snapshot()
        if patrol is not None:
            value = {**value, "patrol": patrol.snapshot()}
        order = bridge.published_seq
        cache.update(value, order)
        if patrol is not None:
            cache.update_patrol(value["patrol"], order)
        return value, order

    def final_snapshot(t):
        snapshot()

    pipeline.on_end.append(final_snapshot)

    @asynccontextmanager
    async def lifespan(app):
        bridge.start()
        try:
            await _hook(on_start)
            yield
        finally:
            try:
                await _hook(on_stop)
            finally:
                bridge.close()
                if final_snapshot in pipeline.on_end:
                    pipeline.on_end.remove(final_snapshot)

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.bridge = bridge
    app.state.server_config = cfg

    async def processing_call(callback):
        result = Future()

        def execute():
            if not result.set_running_or_notify_cancel():
                return
            try:
                result.set_result(callback())
            except Exception as exc:
                result.set_exception(exc)

        pipeline.call_soon(execute)
        return await asyncio.wait_for(asyncio.wrap_future(result), cfg.request_timeout_s)

    def blackbox_status():
        if blackbox is None:
            return {"enabled": False, "events": 0, "clips": 0, "dropped": 0, "integrity_valid": False}
        getter = getattr(blackbox, "status", None) or blackbox.snapshot
        return {"enabled": True, **to_jsonable(getter())}

    def decorate(value, state_seq):
        _, approved_by, _ = cache.get()
        if patrol is not None:
            value = {**value, "patrol": cache.get_patrol()}
        return {
            **to_jsonable(value), "run_id": str(run_id), "profile": profile,
            "sentinel_name": cfg.sentinel_name, "uptime": float(pipeline.t),
            "pipeline_running": bool(pipeline.running), "approved_by": approved_by,
            "state_seq": state_seq, "latest_seq": bridge.latest_seq, "blackbox": blackbox_status(),
            "ui": dict(ui),
            "transport": {"dropped_events": bridge.dropped_ingress,
                          "dropped_client_events": bridge.dropped_clients,
                          "clients": len(bridge._clients)},
        }

    @app.get("/api/state")
    async def get_state():
        value = None
        state_seq = 0
        if not pipeline.finished:
            try:
                value, state_seq = await processing_call(snapshot)
            except asyncio.TimeoutError:
                if not pipeline.finished:
                    raise HTTPException(503, "Audio processing did not return a state snapshot")
            except Exception as exc:
                raise HTTPException(503, f"Dialog state unavailable: {exc}") from exc
        if value is None:
            value, _, state_seq = cache.get()
        if not value:
            raise HTTPException(503, "Dialog state is not available yet")
        return decorate(value, state_seq)

    @app.get("/api/events")
    async def get_events(since: int = 0, limit: int | None = None):
        if since < 0 or (limit is not None and limit < 1):
            raise HTTPException(400, "since must be nonnegative and limit must be positive")
        count = cfg.events_default_limit if limit is None else min(limit, cfg.events_max_limit)
        return bridge.history(since, count)

    @app.post("/api/command")
    async def command(request: Request):
        if patrol is not None:
            raise HTTPException(400, "Patrol sessions accept only gap-aware controls through /api/rover")
        try:
            body = await request.json()
        except ValueError as exc:
            raise HTTPException(400, "Command body must be JSON") from exc
        if not isinstance(body, dict) or set(body) - {"name", "args"}:
            raise HTTPException(400, "Command must contain name and optional args")
        name, args = body.get("name"), body.get("args", {})
        if not isinstance(name, str) or not name or not isinstance(args, dict):
            raise HTTPException(400, "Command name must be a string and args must be an object")
        if set(args) & {"self", "name", "t"}:
            raise HTTPException(400, "Command args must not override routing or stream-time fields")
        if pipeline.finished:
            raise HTTPException(400, "Audio session has finished; commands require a running session")

        def dispatch():
            result = session.command_result(name, **args)
            cache.update(result["state"], bridge.published_seq)
            if result.get("ok") and name == "approve":
                cache.approval(args.get("operator"), bridge.published_seq)
            return to_jsonable(result)

        try:
            result = await processing_call(dispatch)
        except asyncio.TimeoutError as exc:
            if pipeline.finished:
                raise HTTPException(400, "Audio session has finished; commands require a running session") from exc
            raise HTTPException(504, "Command timed out; check state before retrying") from exc
        except Exception as exc:
            raise HTTPException(503, f"Command dispatch failed: {exc}") from exc
        if not result.get("ok"):
            return JSONResponse({**result, "detail": result.get("error", "Invalid command")}, status_code=400)
        return result

    @app.post("/api/rover")
    async def rover_command(request: Request):
        if patrol is None:
            raise HTTPException(400, "Rover controls require a patrol session")
        try:
            body = await request.json()
        except ValueError as exc:
            raise HTTPException(400, "Rover command body must be JSON") from exc
        if not isinstance(body, dict) or set(body) - {"name", "args"}:
            raise HTTPException(400, "Rover command must contain name and optional args")
        name, args = body.get("name"), body.get("args", {})
        if not isinstance(name, str) or name not in {"stop", "estop", "pause", "resume", "abort", "approve"}:
            raise HTTPException(400, "Unknown rover command")
        if not isinstance(args, dict):
            raise HTTPException(400, "Rover command args must be an object")
        if name == "approve":
            if set(args) != {"gap", "operator"} or any(
                not isinstance(args[key], str) or not args[key].strip() for key in ("gap", "operator")
            ):
                raise HTTPException(400, "Approval requires a gap name and operator")
            if pipeline.finished:
                raise HTTPException(400, "Audio session has finished; approval requires a running recording")

            def approve():
                if pipeline.finished:
                    return {"ok": False, "error": "Audio session has finished; approval requires a running recording"}
                result = patrol.approve(args["gap"].strip(), args["operator"].strip())
                if result.get("ok"):
                    patrol_state = result["patrol"] if "patrol" in result else patrol.snapshot()
                    cache.update_patrol(patrol_state, bridge.published_seq)
                return result

            operation = processing_call(approve)
        else:
            if args:
                raise HTTPException(400, "Rover control commands do not accept args")
            # STOP runs on its own worker, independently of a busy or finished
            # processing queue. Patrol sends the motor STOP before queuing its
            # bookkeeping callback; never route this through processing_call.
            operation = asyncio.to_thread(patrol.command, "estop" if name == "stop" else name)
        try:
            result = to_jsonable(await asyncio.wait_for(operation, cfg.request_timeout_s))
        except asyncio.TimeoutError as exc:
            if name == "approve" and pipeline.finished:
                raise HTTPException(400, "Audio session has finished; approval requires a running recording") from exc
            raise HTTPException(504, "Rover control timed out; check state before retrying") from exc
        except Exception as exc:
            raise HTTPException(503, f"Rover control failed: {exc}") from exc
        if not result.get("ok"):
            return JSONResponse({**result, "detail": result.get("error", "Rover control refused")}, status_code=400)
        return result

    @app.websocket("/ws")
    async def websocket_events(websocket: WebSocket):
        text = websocket.query_params.get("since")
        try:
            since = int(text) if text is not None else None
            if since is not None and since < 0:
                raise ValueError
        except ValueError:
            await websocket.close(code=1008, reason="since must be a nonnegative sequence")
            return
        client, backfill = bridge.subscribe(since)
        try:
            await websocket.accept()
        except BaseException:
            bridge.unsubscribe(client)
            raise

        async def disconnected():
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    return

        reader = asyncio.create_task(disconnected())
        pending = None
        try:
            for event in backfill:
                if reader.done():
                    return
                await websocket.send_json(event)
            while True:
                pending = asyncio.create_task(client.queue.get())
                done, _ = await asyncio.wait((reader, pending), return_when=asyncio.FIRST_COMPLETED)
                if reader in done:
                    return
                event = pending.result()
                pending = None
                if event is None:
                    return
                await websocket.send_json(event)
        except (WebSocketDisconnect, RuntimeError, asyncio.CancelledError):
            pass
        finally:
            bridge.unsubscribe(client)
            tasks = [reader] + ([pending] if pending is not None else [])
            for task in tasks:
                task.cancel()
            try:
                await asyncio.gather(*tasks, return_exceptions=True)
            except asyncio.CancelledError:
                # ASGI shutdown may cancel this connection before its receive
                # task has consumed the disconnect frame. Both child tasks
                # have already been cancelled above.
                pass

    path = Path(dashboard_dir) if dashboard_dir is not None else Path(__file__).resolve().parent.parent / "dashboard"
    bootstrap = json.dumps(ui, separators=(",", ":")).replace("<", "\\u003c")

    @app.get("/", response_class=HTMLResponse)
    @app.get("/index.html", response_class=HTMLResponse)
    async def dashboard():
        try:
            html = (path / "index.html").read_text(encoding="utf-8")
        except OSError as exc:
            raise HTTPException(404, "Dashboard files are unavailable") from exc
        return HTMLResponse(html.replace("__HEARTBEAT_UI_CONFIG__", bootstrap))

    app.mount("/", StaticFiles(directory=path, html=True, check_dir=False), name="dashboard")
    return app


def run_server(app, *, host=None, port=None):
    """Run uvicorn on the main thread while lifecycle hooks manage audio."""
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError("run_server must run on the main thread")
    import uvicorn

    cfg = app.state.server_config
    uvicorn.run(app, host=cfg.host if host is None else host, port=cfg.port if port is None else port)
