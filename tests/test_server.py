import asyncio
from concurrent.futures import Future
from copy import deepcopy
import json
import queue
import threading

from fastapi.testclient import TestClient
import numpy as np
import pytest

from heartbeat.config import load_profile
from heartbeat.events import Event, EventBus
from heartbeat.server import EventBridge, ServerConfig, create_app, run_server


class FakePipeline:
    def __init__(self):
        self.profile = load_profile("rubble")
        self.t = 12.5
        self.on_end = []
        self._jobs = queue.Queue()
        self._finished = threading.Event()
        self.gate = threading.Event()
        self.gate.set()
        self.thread = None

    @property
    def finished(self):
        return self._finished.is_set()

    @property
    def running(self):
        return self.thread is not None and self.thread.is_alive()

    def call_soon(self, fn):
        self._jobs.put(fn)

    def start(self):
        def worker():
            while True:
                fn = self._jobs.get()
                if fn is None:
                    return
                self.gate.wait()
                fn()
                if self.finished:
                    return

        self.thread = threading.Thread(target=worker, daemon=True)
        self.thread.start()

    def submit(self, fn):
        result = Future()

        def execute():
            try:
                result.set_result(fn())
            except Exception as exc:
                result.set_exception(exc)

        self.call_soon(execute)
        return result.result(timeout=2)

    def finish(self):
        def end():
            for hook in tuple(self.on_end):
                hook(self.t)
            self._finished.set()

        self.submit(end)
        self.thread.join(timeout=2)

    def close(self):
        self.gate.set()
        if self.running:
            self.finish()


class FakeEngine:
    def __init__(self, pipeline):
        self.pipeline = pipeline
        self.snapshots = 0
        self.value = {
            "state": "LISTENING", "status": "NO_SIGNS", "round": 0, "rounds": [],
            "combined_p": 1.0, "odds": "1 in 1", "answers": {}, "approved": False,
            "lambda_per_min": 1.0, "profile": "rubble",
        }

    def snapshot(self):
        assert threading.current_thread() is self.pipeline.thread
        self.snapshots += 1
        return deepcopy(self.value)


class FakeSession:
    def __init__(self, pipeline, bus):
        self.pipeline, self.bus = pipeline, bus
        self.engine = FakeEngine(pipeline)
        self.commands = []

    def command_result(self, name, **args):
        assert threading.current_thread() is self.pipeline.thread
        self.commands.append((name, args, threading.get_ident()))
        actions, error = [], None
        if name == "challenge":
            self.engine.value.update(state="PROMPTING", status="POSSIBLE", round=1)
            actions = [{"action": "PromptKnocks", "n": 3, "interval_s": 0.5}]
        elif name == "all_quiet":
            self.engine.value.update(state="PRE_LISTEN", status="POSSIBLE", round=0)
            self.bus.publish("operator", self.pipeline.t, command=name, **args)
        elif name == "approve" and self.engine.value["state"] == "CONFIRMED":
            self.engine.value["approved"] = True
            self.bus.publish("approval", self.pipeline.t, approved=True, operator=args.get("operator"))
        elif name == "reset":
            self.engine.value.update(state="LISTENING", status="NO_SIGNS", approved=False)
        else:
            error = "approve requires CONFIRMED state" if name == "approve" else "unknown operator command"
            self.bus.publish("error", self.pipeline.t, message=error, command=name)
        state = self.engine.snapshot()
        self.bus.publish("state", self.pipeline.t, **state)
        return {"ok": error is None, "actions": actions, "state": state, **({"error": error} if error else {})}


class FakeBlackBox:
    def status(self):
        return {"events": 42, "clips": 3, "dropped": 0, "failed": False, "integrity_valid": True}


def make_test_app(tmp_path, *, cfg=None):
    pipeline, bus = FakePipeline(), EventBus()
    session = FakeSession(pipeline, bus)
    directory = tmp_path / "dashboard"
    directory.mkdir()
    (directory / "index.html").write_text(
        '<html><head><script id="heartbeat-config" type="application/json">'
        '__HEARTBEAT_UI_CONFIG__</script></head>Heartbeat test dashboard</html>'
    )
    (directory / "app.js").write_text("window.heartbeat = true;")
    app = create_app(
        pipeline, session, bus, cfg=cfg, run_id="test-run", profile=pipeline.profile,
        blackbox=FakeBlackBox(), dashboard_dir=directory,
        on_start=pipeline.start, on_stop=pipeline.close,
    )
    return app, pipeline, session, bus


@pytest.fixture
def api(tmp_path):
    app, pipeline, session, bus = make_test_app(tmp_path)
    with TestClient(app) as client:
        yield client, pipeline, session, bus, app


def test_server_config_rejects_unknown_keys_and_invalid_bounds():
    with pytest.raises(ValueError, match="Unknown server"):
        ServerConfig.from_dict({"typo": True})
    with pytest.raises(ValueError, match="positive integer"):
        ServerConfig.from_dict({"client_queue": 0})
    with pytest.raises(ValueError, match="65535"):
        ServerConfig.from_dict({"port": 65536})
    with pytest.raises(ValueError, match="finite"):
        ServerConfig.from_dict({"request_timeout_s": float("nan")})
    with pytest.raises(ValueError, match="positive"):
        ServerConfig.from_dict({"dashboard": {"reconnect_ms": 0}})
    with pytest.raises(ValueError, match="documented UI"):
        ServerConfig.from_dict({"dashboard": {"typo": 10}})
    for level in (None, True, "loud", "WARNING"):
        with pytest.raises(ValueError, match="log_level"):
            ServerConfig.from_dict({"log_level": level})


def test_server_serves_dashboard_and_local_assets(api):
    client, *_ = api
    assert "Heartbeat test dashboard" in client.get("/").text
    assert client.get("/app.js").text == "window.heartbeat = true;"
    assert client.get("/missing.js").status_code == 404


def test_root_embeds_validated_ui_settings_before_state_request(api):
    client, pipeline, session, _, _ = api
    html = client.get("/").text
    assert "__HEARTBEAT_UI_CONFIG__" not in html
    encoded = html.split('type="application/json">', 1)[1].split("</script>", 1)[0]
    settings = json.loads(encoded)
    assert settings["reconnect_ms"] == pipeline.profile.server["dashboard"]["reconnect_ms"]
    assert settings["events_limit"] == pipeline.profile.server["events_default_limit"]
    assert session.engine.snapshots == 0
    assert client.get("/index.html").text == html


def test_state_shape_and_snapshot_are_from_processing_thread(api):
    client, pipeline, session, _, _ = api
    response = client.get("/api/state")
    assert response.status_code == 200
    state = response.json()
    assert state["state"] == "LISTENING"
    assert state["status"] == "NO_SIGNS"
    assert state["run_id"] == "test-run"
    assert state["profile"] == "rubble"
    assert state["sentinel_name"] == "Heartbeat"
    assert state["uptime"] == pipeline.t
    assert state["pipeline_running"]
    assert state["blackbox"]["events"] == 42
    assert state["blackbox"]["enabled"]
    assert state["ui"]["events_limit"] == pipeline.profile.server["events_default_limit"]
    assert session.engine.snapshots == 1
    json.dumps(state, allow_nan=False)


def test_command_routes_to_processing_thread_and_returns_result(api):
    client, pipeline, session, _, _ = api
    response = client.post("/api/command", json={"name": "challenge", "args": {}})
    assert response.status_code == 200
    assert response.json()["ok"]
    assert response.json()["state"]["state"] == "PROMPTING"
    assert response.json()["actions"][0]["n"] == 3
    assert session.commands == [("challenge", {}, pipeline.thread.ident)]


@pytest.mark.parametrize("operator", [None, "AB"])
def test_all_quiet_routes_operator_and_challenge_without_dropping_args(api, operator):
    client, pipeline, session, _, _ = api
    args = {"operator": operator, "challenge": True}
    response = client.post("/api/command", json={"name": "all_quiet", "args": args})
    assert response.status_code == 200
    assert response.json()["state"]["state"] == "PRE_LISTEN"
    assert response.json()["actions"] == []
    assert session.commands == [("all_quiet", args, pipeline.thread.ident)]
    events = client.get("/api/events").json()["events"]
    assert [event["data"] for event in events if event["type"] == "operator"] == [
        {"command": "all_quiet", **args},
    ]


def test_state_api_preserves_candidate_references_and_coaching_text(api):
    client, pipeline, session, _, _ = api
    data = {"candidate": True, "candidate_times": [1.0, 1.5], "coaching": True,
            "text": "Hearing knocks: asking them to listen."}
    pipeline.submit(lambda: session.engine.value.update(data))
    state = client.get("/api/state").json()
    assert {key: state[key] for key in data} == data


def test_snapshot_cursor_does_not_hide_a_later_state_event(api, monkeypatch):
    from heartbeat import server

    client, pipeline, _, bus, app = api
    encode = server.to_jsonable
    published = False

    def advance_while_serializing(value):
        nonlocal published
        if not published and isinstance(value, dict) and value.get("state") == "LISTENING":
            published = True
            # Reproduce an event arriving after the processing-thread snapshot
            # but before the HTTP response is assembled on the event loop.
            bus.publish("state", pipeline.t, state="PROMPTING", status="POSSIBLE")
            app.state.bridge._drain()
        return encode(value)

    monkeypatch.setattr(server, "to_jsonable", advance_while_serializing)
    state = client.get("/api/state").json()
    assert state["status"] == "NO_SIGNS"
    assert state["state_seq"] < state["latest_seq"]
    events = client.get("/api/events", params={"since": state["state_seq"]}).json()["events"]
    assert events[-1]["data"]["status"] == "POSSIBLE"


def test_invalid_engine_command_returns_400_with_reason(api):
    client, _, session, _, _ = api
    response = client.post("/api/command", json={"name": "approve", "args": {"operator": "AB"}})
    assert response.status_code == 400
    assert response.json()["detail"] == "approve requires CONFIRMED state"
    assert not response.json()["ok"]
    assert session.commands[0][1] == {"operator": "AB"}


@pytest.mark.parametrize("body", [
    [], {"name": 3}, {"name": "reset", "args": []}, {"name": "reset", "extra": 1},
    {"name": "reset", "args": {"name": "approve"}},
    {"name": "reset", "args": {"t": 0}},
    {"name": "reset", "args": {"self": None}},
])
def test_malformed_commands_return_400_without_dispatch(api, body):
    client, _, session, _, _ = api
    assert client.post("/api/command", json=body).status_code == 400
    assert session.commands == []


def test_websocket_receives_json_event_without_clip_array(api):
    client, _, _, bus, _ = api
    with client.websocket_connect("/ws") as websocket:
        bus.publish("sound", 1.25, score=np.float32(0.8), clip=None, _clip=np.ones(15600, dtype=np.float32))
        event = websocket.receive_json()
        assert set(event) == {"seq", "type", "t", "wall", "data"}
        assert event["seq"] == 1
        assert event["type"] == "sound"
        assert event["t"] == 1.25
        assert "_clip" not in event["data"]
        assert event["data"]["clip"] is None
        json.dumps(event, allow_nan=False)
        bus.publish("level", 1.3, dbfs=-50, floor_db=-52)
        assert websocket.receive_json()["seq"] == 2


def test_websocket_reconnect_backfills_then_streams_without_duplicates(api):
    client, _, _, bus, _ = api
    for index in range(3):
        bus.publish("level", index / 10, dbfs=-50, floor_db=-52)
    assert [event["seq"] for event in client.get("/api/events").json()["events"]] == [1, 2, 3]
    with client.websocket_connect("/ws?since=1") as websocket:
        assert websocket.receive_json()["seq"] == 2
        bus.publish("level", 0.3, dbfs=-49, floor_db=-52)
        assert websocket.receive_json()["seq"] == 3
        assert websocket.receive_json()["seq"] == 4


def test_event_history_and_limit_are_bounded(tmp_path):
    app, _, _, bus = make_test_app(tmp_path, cfg={"event_history": 3, "events_default_limit": 2, "events_max_limit": 3})
    with TestClient(app) as client:
        for index in range(6):
            bus.publish("level", index / 10, dbfs=-50, floor_db=-52)
        result = client.get("/api/events?since=0&limit=1000").json()
        assert [event["seq"] for event in result["events"]] == [4, 5, 6]
        assert result["truncated"]
        assert result["oldest_seq"] == 4
        assert result["latest_seq"] == 6
        page = client.get("/api/events?since=4&limit=1").json()
        assert [event["seq"] for event in page["events"]] == [5]
        assert page["has_more"]


def test_finished_pipeline_serves_cached_state_and_rejects_commands(api):
    client, pipeline, session, _, _ = api
    assert client.get("/api/state").status_code == 200
    pipeline.finish()
    before = session.engine.snapshots
    state = client.get("/api/state").json()
    assert state["state"] == "LISTENING"
    assert not state["pipeline_running"]
    assert session.engine.snapshots == before
    response = client.post("/api/command", json={"name": "reset"})
    assert response.status_code == 400
    assert "finished" in response.json()["detail"]
    assert session.commands == []


def test_approval_initials_are_visible_and_reset_removes_them(api):
    client, pipeline, session, _, _ = api
    pipeline.submit(lambda: session.engine.value.update(state="CONFIRMED", status="CONFIRMED"))
    assert client.post("/api/command", json={"name": "approve", "args": {"operator": "AB"}}).status_code == 200
    assert client.get("/api/state").json()["approved_by"] == "AB"
    assert client.post("/api/command", json={"name": "reset"}).status_code == 200
    state = client.get("/api/state").json()
    assert not state["approved"]
    assert state["approved_by"] is None


def test_timed_out_queued_command_is_cancelled_before_execution(tmp_path):
    app, pipeline, session, _ = make_test_app(tmp_path, cfg={"request_timeout_s": 0.05})
    with TestClient(app) as client:
        pipeline.gate.clear()
        try:
            response = client.post("/api/command", json={"name": "reset"})
            assert response.status_code == 504
        finally:
            pipeline.gate.set()
        pipeline.submit(lambda: None)
        assert session.commands == []


def test_slow_client_and_ingress_never_block_bus_or_serialize_on_producer(monkeypatch):
    serializers = []
    original = Event.to_dict

    def serialize(event):
        serializers.append(threading.get_ident())
        return original(event)

    monkeypatch.setattr(Event, "to_dict", serialize)

    async def check():
        bus = EventBus()
        bridge = EventBridge(bus, {"bridge_queue_events": 8, "client_queue": 2, "event_history": 8})
        bridge.start()
        slow, _ = bridge.subscribe()

        def produce():
            for index in range(100):
                bus.publish("level", index / 10, dbfs=-50, floor_db=-52)

        producer = threading.Thread(target=produce, daemon=True)
        producer.start()
        producer.join(timeout=1)
        assert not producer.is_alive()
        assert serializers == []
        await asyncio.sleep(0)
        assert bridge.latest_seq == 100
        assert bridge.dropped_ingress == 92
        assert slow.queue.qsize() == 2
        assert slow.dropped == 6
        assert serializers == [threading.get_ident()] * 8
        bridge.close()

    asyncio.run(check())


@pytest.mark.parametrize("log_level", [None, "debug"])
def test_run_server_uses_main_thread_without_opening_test_socket(tmp_path, monkeypatch, log_level):
    import uvicorn

    app, *_ = make_test_app(tmp_path, cfg={} if log_level is None else {"log_level": log_level})
    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda target, **kwargs: calls.append((target, kwargs)))
    run_server(app, host="127.0.0.1", port=9000)
    assert calls == [(app, {"host": "127.0.0.1", "port": 9000,
                           "access_log": False, "log_level": log_level or "warning"})]
