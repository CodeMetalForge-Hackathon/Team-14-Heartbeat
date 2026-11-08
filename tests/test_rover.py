"""The rover's serial and safety paths never access an actual device."""

from collections import deque
import threading
import time

import pytest

from heartbeat import rover
from heartbeat.events import EventBus


class Serial:
    def __init__(self, *, motion_reply=True, identity="PONG rover v1"):
        self.motion_reply = motion_reply
        self.identity = identity
        self.input = deque()
        self.writes = []
        self.condition = threading.Condition()
        self.is_open = False
        self.open_settings = None

    def open(self):
        self.open_settings = (self.dtr, self.rts, self.exclusive, self.baudrate)
        self.is_open = True

    def reset_input_buffer(self):
        with self.condition:
            self.input.clear()

    def push(self, line):
        with self.condition:
            self.input.extend((line + "\n").encode())
            self.condition.notify_all()

    def write(self, payload):
        line = payload.decode().strip()
        with self.condition:
            self.writes.append(line)
            self.condition.notify_all()
        words = line.split()
        if words[0] == "PING":
            self.push(self.identity)
        elif words[0] == "STATUS":
            self.push("STATUS batt=11.98 moving=0 enc=-10,20")
        elif words[0] in {"DRIVE", "TURN"}:
            if self.motion_reply:
                self.push("OK " + " ".join(words[:2]))
        elif words[0] != "HB":
            self.push("OK " + line)
        return len(payload)

    def read(self, n):
        with self.condition:
            self.condition.wait_for(lambda: self.input or not self.is_open, timeout=self.timeout)
            return bytes([self.input.popleft()]) if self.input else b""

    def close(self):
        with self.condition:
            self.is_open = False
            self.condition.notify_all()

    def wait_command(self, prefix, timeout=1):
        with self.condition:
            return self.condition.wait_for(
                lambda: any(line.startswith(prefix) for line in self.writes), timeout=timeout)


@pytest.fixture
def make_link(monkeypatch):
    links = []
    claimed = set()

    def claim(port):
        if port in claimed:
            return False
        claimed.add(port)
        return True

    monkeypatch.setattr(rover, "claim_port", claim)
    monkeypatch.setattr(rover, "release_port", claimed.discard)

    def make(port=None, **kwargs):
        serial = Serial() if port is None else port
        cfg = {"connect_timeout_s": 0.2, "command_timeout_s": 0.1,
               "move_timeout_s": 0.15, "heartbeat_s": 0.01, "read_timeout_s": 0.01}
        cfg.update(kwargs.pop("cfg", {}))
        link = rover.RoverLink("/dev/fake-rover", cfg=cfg, serial_factory=lambda: serial, **kwargs)
        links.append(link)
        return link, serial

    yield make
    for link in links:
        link.close()
    assert not claimed


@pytest.mark.parametrize("line,kind", [
    ("READY rover v1", "READY"), (b"PONG rover v1\r\n", "PONG"),
    ("OK DRIVE -12.5", "OK"), ("OK TURN 90", "OK"), ("OK STOP", "OK"),
    ("OK LED 255 0 12", "OK"), ("OK CAL 18.000000 2.500000", "OK"),
    ("STATUS batt=12.34 moving=1 enc=-12.5,6", "STATUS"),
    ("ERR watchdog", "ERR"),
])
def test_parse_all_replies(line, kind):
    assert rover.parse_reply(line)["kind"] == kind


@pytest.mark.parametrize("line", ["", "   ", "PONG knocker v1", "READY stickmic v1", "OK", "OK DRIVE nan",
    "OK TURN inf", "OK STOP 1", "OK LED 256 0 0", "OK LED 1.5 0 0", "OK CAL 18",
    "STATUS batt=nan moving=0 enc=1,2", "STATUS batt=12 moving=2 enc=1,2",
    "STATUS batt=12 moving=0 enc=1,2,3", "STATUS batt=12 moving=0 enc=1,2 extra=1",
    "ERR", "OK STOP\nOK DRIVE 1", "x" * 257])
def test_parser_rejects_malformed_or_wrong_device(line):
    with pytest.raises(ValueError):
        rover.parse_reply(line)


def test_no_constructor_io_and_no_reset_exclusive_open(make_link):
    link, serial = make_link()
    assert not serial.is_open and not serial.writes
    assert link.ping()
    assert serial.open_settings == (False, False, True, 115200)
    assert serial.writes == ["PING", "PING"]


@pytest.mark.parametrize("method,args", [
    ("drive", (101,)), ("drive", (-101,)), ("turn", (181,)), ("turn", (-181,)),
    ("drive", (1, 61)), ("turn", (1, 0)), ("drive", (True,)), ("turn", (float("nan"),)),
    ("drive", (1, float("inf"))), ("led", (256, 0, 0)), ("led", (1.5, 0, 0)),
    ("led", (True, 0, 0)), ("calibrate", (0, 2.5)), ("calibrate", (18, 100001)),
    ("calibrate", (float("inf"), 2.5)),
])
def test_bad_commands_are_refused_before_open(make_link, method, args):
    link, serial = make_link()
    assert getattr(link, method)(*args) is False
    assert not serial.is_open and serial.writes == []


def test_gate_rechecked_and_all_nonmotion_commands_work_while_listening(make_link):
    link, serial = make_link(engine_is_listening=lambda: True)
    assert not link.drive(1) and not link.turn(1)
    assert not serial.writes
    assert link.ping()
    assert link.led(255, 0, 0)
    assert link.calibrate(18, 2.5)
    assert link.status()["enc"] == [-10, 20]
    assert link.stop()
    assert not any(line.startswith(("DRIVE", "TURN")) for line in serial.writes)


def test_timeout_sends_stop_and_releases_connection(make_link):
    link, serial = make_link(Serial(motion_reply=False), cfg={"move_timeout_s": 0.03})
    assert not link.drive(20)
    assert "STOP" in serial.writes
    assert not serial.is_open and not link.moving


def test_heartbeat_only_during_active_move(make_link):
    link, serial = make_link(Serial(motion_reply=False))
    result = []
    worker = threading.Thread(target=lambda: result.append(link.drive(20)))
    worker.start()
    assert serial.wait_command("DRIVE") and serial.wait_command("HB")
    serial.push("OK DRIVE 20")
    worker.join(1)
    assert not worker.is_alive() and result == [True]
    count = serial.writes.count("HB")
    time.sleep(0.03)
    assert serial.writes.count("HB") == count and not link.moving


def test_estop_interrupts_move_and_cancels_queued_move_without_waiting(make_link):
    link, serial = make_link(Serial(motion_reply=False), cfg={"move_timeout_s": 5})
    results = []
    active = threading.Thread(target=lambda: results.append(link.drive(20)))
    queued = threading.Thread(target=lambda: results.append(link.turn(90)))
    active.start()
    assert serial.wait_command("DRIVE")
    queued.start()
    start = time.monotonic()
    assert link.estop()
    assert time.monotonic() - start < 0.3
    active.join(1)
    queued.join(1)
    assert results == [False, False]
    assert not any(line.startswith("TURN") for line in serial.writes)
    assert not link.moving and link.estopped
    assert not link.drive(1)
    link.reset_estop()
    assert not link.estopped


def test_retired_connection_late_completion_cannot_finish_new_move(make_link):
    first = Serial(motion_reply=False)
    link, _ = make_link(first, cfg={"move_timeout_s": 0.04})
    active = threading.Thread(target=lambda: link.drive(20))
    active.start()
    assert first.wait_command("DRIVE")
    assert link.stop()
    active.join(1)
    second = Serial(motion_reply=False)
    link.serial_factory = lambda: second
    first.push("OK DRIVE 20")
    assert not link.drive(20)
    assert "STOP" in second.writes


def test_wrong_identity_never_sends_a_move(make_link):
    link, serial = make_link(Serial(identity="PONG knocker v1"))
    assert not link.drive(20)
    assert serial.writes == ["PING"]
    assert not serial.is_open


def test_excluded_or_claimed_port_never_opens(make_link, monkeypatch):
    link, serial = make_link(exclude_ports={"/dev/fake-rover"})
    assert not link.ping() and not serial.is_open
    link.exclude_ports.clear()
    monkeypatch.setattr(rover, "claim_port", lambda _: False)
    assert not link.ping() and not serial.is_open


def test_events_use_only_injected_stream_time_and_cleanup_stops(make_link):
    bus = EventBus()
    events = []
    bus.subscribe(events.append)
    link, serial = make_link(bus=bus, clock=lambda: 12.25)
    assert link.drive(-100, 60) and link.turn(180, 1)
    link.close()
    assert serial.writes[-1] == "STOP" and not serial.is_open
    assert {event.t for event in events} == {12.25}
    assert {event.data["kind"] for event in events if event.type == "rover"} >= {"command", "reply"}


def test_watchdog_error_stops_instead_of_reporting_success(make_link):
    link, serial = make_link(Serial(motion_reply=False))
    results = []
    worker = threading.Thread(target=lambda: results.append(link.drive(1)))
    worker.start()
    assert serial.wait_command("DRIVE")
    serial.push("ERR watchdog")
    worker.join(1)
    assert results == [False] and "STOP" in serial.writes


def test_fake_moves_in_stream_time_and_estop_is_latched():
    fake = rover.FakeRover()
    assert fake.drive(20)
    assert fake.moving
    fake.advance(fake.duration("drive", 20))
    assert not fake.moving
    assert fake.turn(90)
    assert fake.estop() and not fake.moving
    fake.advance(100)
    assert not fake.moving and not fake.drive(1)
    assert [item["command"] for item in fake.trace] == ["DRIVE", "TURN", "STOP"]
    fake.reset_estop()
    assert fake.drive(1)


@pytest.mark.parametrize("fake", [False, True])
def test_reentrant_event_estop_cannot_restart_move(make_link, fake):
    bus = EventBus()
    link = rover.FakeRover(bus=bus) if fake else make_link(Serial(motion_reply=False), bus=bus)[0]
    stop_results = []
    bus.subscribe(lambda event: stop_results.append(link.estop()) if event.type == "rover"
                  and event.data.get("kind") == "command" and event.data.get("command") == "DRIVE" else None)
    assert not link.drive(1)
    assert stop_results == [True]
    assert link.estopped and not link.moving


def test_blocked_command_subscriber_does_not_block_stop_transmission(make_link):
    bus = EventBus()
    entered, release = threading.Event(), threading.Event()
    def subscriber(event):
        if event.type == "rover" and event.data.get("kind") == "command" and event.data.get("command") == "DRIVE":
            entered.set()
            release.wait(2)
    bus.subscribe(subscriber)
    link, serial = make_link(Serial(motion_reply=False), bus=bus, cfg={"move_timeout_s": 5})
    active = threading.Thread(target=lambda: link.drive(1))
    stopper = threading.Thread(target=link.estop)
    active.start()
    try:
        assert entered.wait(1)
        stopper.start()
        assert serial.wait_command("STOP", timeout=0.3)
    finally:
        release.set()
        active.join(1)
        if stopper.ident is not None:
            stopper.join(1)
    assert not active.is_alive() and not stopper.is_alive()
    assert link.estopped and not link.moving


def test_profile_cannot_relax_firmware_safety_bounds():
    for cfg in ({"max_drive_cm": 101}, {"max_turn_deg": 181}, {"max_speed_pct": 61},
                {"heartbeat_s": 0.6}, {"default_speed_pct": 61}, {"calibration_min": 0.0001}):
        with pytest.raises(ValueError):
            rover.FakeRover(cfg=cfg)


class CliRover:
    def __init__(self, *args, **kwargs):
        self.calls = []

    def drive(self, *args):
        self.calls.append(("drive", *args))
        return True

    def turn(self, *args):
        self.calls.append(("turn", *args))
        return True

    def calibrate(self, *args):
        self.calls.append(("calibrate", *args))
        return True

    def estop(self):
        self.calls.append(("estop",))

    def close(self):
        self.calls.append(("close",))


def test_cli_drive_routes_value_and_speed(monkeypatch):
    from heartbeat.cli import main
    link = CliRover()
    monkeypatch.setattr(rover, "RoverLink", lambda *a, **kw: link)
    assert main(["rover", "--port", "fake", "drive", "20", "--speed", "25"]) == 0
    assert link.calls == [("drive", 20, 25), ("close",)]


def test_cli_calibration_measures_and_updates_both_axes(monkeypatch):
    from heartbeat.cli import main
    link = CliRover()
    answers = iter(["", "40", "", "80"])
    monkeypatch.setattr(rover, "RoverLink", lambda *a, **kw: link)
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    assert main(["rover", "--port", "fake", "calibrate"]) == 0
    assert link.calls == [("calibrate", 18, 2.5), ("drive", 50, 20),
                          ("calibrate", 22.5, 2.5), ("turn", 90, 20),
                          ("calibrate", 22.5, 2.8125), ("close",)]


def test_cli_ctrl_c_estops_before_close(monkeypatch):
    from heartbeat.cli import main
    link = CliRover()
    def interrupt(*_):
        raise KeyboardInterrupt
    link.drive = interrupt
    monkeypatch.setattr(rover, "RoverLink", lambda *a, **kw: link)
    assert main(["rover", "--port", "fake", "drive", "20"]) == 130
    assert link.calls == [("estop",), ("close",)]


class ApiPatrol:
    """Patrol boundary fake; enforce the same thread ownership as production."""

    def __init__(self, pipeline, bus):
        self.pipeline, self.bus = pipeline, bus
        self.commands, self.applied, self.approvals = [], [], []
        self.snapshots = 0
        self.value = {
            "state": "MOVING", "gap": "Gap 1", "moving": True, "done": False,
            "results": [{"gap": "Gap 1", "status": "NO_SIGNS"},
                        {"gap": "Gap 2", "status": "CONFIRMED", "odds": "1 in 10000", "answers": {}}],
        }

    def snapshot(self):
        from copy import deepcopy
        assert threading.current_thread() is self.pipeline.thread
        self.snapshots += 1
        return deepcopy(self.value)

    def command(self, name):
        assert threading.current_thread() is not self.pipeline.thread
        # Represents the direct STOP before processing-thread bookkeeping.
        self.commands.append((name, threading.get_ident()))
        self.pipeline.call_soon(lambda: self.applied.append(name))
        return {"ok": True}

    def approve(self, gap, operator):
        assert threading.current_thread() is self.pipeline.thread
        result = next((item for item in self.value["results"] if item["gap"] == gap), None)
        if result is None or result["status"] != "CONFIRMED":
            return {"ok": False, "error": "approval requires a CONFIRMED gap"}
        result.update(approved=True, approved_by=operator)
        self.approvals.append((gap, operator))
        self.bus.publish("approval", self.pipeline.t, gap=gap, operator=operator, approved=True)
        value = self.snapshot()
        self.bus.publish("patrol", self.pipeline.t, **value)
        return {"ok": True, "patrol": value}


@pytest.fixture
def patrol_api(tmp_path):
    from fastapi.testclient import TestClient
    from heartbeat.server import create_app
    from test_server import FakePipeline, FakeSession
    pipeline, bus = FakePipeline(), EventBus()
    session = FakeSession(pipeline, bus)
    patrol = ApiPatrol(pipeline, bus)
    app = create_app(pipeline, session, bus, patrol=patrol,
                     cfg={"request_timeout_s": 0.1}, dashboard_dir=tmp_path,
                     on_start=pipeline.start, on_stop=pipeline.close)
    with TestClient(app) as client:
        yield client, pipeline, session, patrol, bus


@pytest.mark.parametrize("name,expected", [("stop", "estop"), ("estop", "estop"),
                                         ("pause", "pause"), ("resume", "resume"), ("abort", "abort")])
def test_rover_api_controls_run_before_pipeline_callbacks(patrol_api, name, expected):
    client, pipeline, session, patrol, _ = patrol_api
    pipeline.gate.clear()
    try:
        response = client.post("/api/rover", json={"name": name})
        assert response.status_code == 200
        assert [item[0] for item in patrol.commands] == [expected]
        assert patrol.commands[0][1] != pipeline.thread.ident
        assert not patrol.applied and not session.commands
    finally:
        pipeline.gate.set()
    pipeline.submit(lambda: None)
    assert patrol.applied == [expected]


def test_rover_api_estop_still_runs_after_pipeline_has_finished(patrol_api):
    client, pipeline, _, patrol, _ = patrol_api
    pipeline.finish()
    assert client.post("/api/rover", json={"name": "stop"}).status_code == 200
    assert [item[0] for item in patrol.commands] == ["estop"]
    assert not patrol.applied


@pytest.mark.parametrize("body", [[], {}, {"name": True}, {"name": "drive", "args": {"cm": 1}},
    {"name": "turn"}, {"name": "STOP"}, {"name": "stop", "extra": 1},
    {"name": "stop", "args": []}, {"name": "stop", "args": {"operator": "AB"}},
    {"name": "approve"}, {"name": "approve", "args": {"gap": "Gap 2"}},
    {"name": "approve", "args": {"gap": "Gap 2", "operator": " "}},
    {"name": "approve", "args": {"gap": True, "operator": "AB"}},
    {"name": "approve", "args": {"gap": "Gap 2", "operator": "AB", "t": 0}},
])
def test_rover_api_rejects_invalid_names_and_args_without_dispatch(patrol_api, body):
    client, _, session, patrol, _ = patrol_api
    assert client.post("/api/rover", json=body).status_code == 400
    assert not patrol.commands and not patrol.approvals and not session.commands


@pytest.mark.parametrize("name", ["challenge", "reset", "approve", "cancel", "unknown"])
def test_patrol_rejects_all_generic_dialog_commands(patrol_api, name):
    client, _, session, patrol, _ = patrol_api
    assert client.post("/api/command", json={"name": name}).status_code == 400
    assert not session.commands and not patrol.commands


def test_rover_api_gap_approval_is_recorded_and_reconnect_state_is_scoped(patrol_api):
    client, pipeline, session, patrol, _ = patrol_api
    pipeline.submit(lambda: patrol.value.update(state="DONE", moving=False, done=True))
    response = client.post("/api/rover", json={"name": "approve", "args": {"gap": "Gap 2", "operator": " AB "}})
    assert response.status_code == 200 and response.json()["ok"]
    assert patrol.approvals == [("Gap 2", "AB")]
    events = client.get("/api/events").json()["events"]
    approval = next(item for item in events if item["type"] == "approval")
    assert approval["data"] == {"gap": "Gap 2", "operator": "AB", "approved": True}
    with client.websocket_connect(f"/ws?since={approval['seq'] - 1}") as websocket:
        assert websocket.receive_json()["data"] == approval["data"]
    state = client.get("/api/state").json()
    assert state["patrol"]["done"]
    assert state["patrol"]["results"][1]["approved_by"] == "AB"
    assert not state["approved"] and state["approved_by"] is None
    assert not session.commands


@pytest.mark.parametrize("gap", ["Gap 1", "missing gap"])
def test_rover_api_approval_requires_confirmed_existing_gap(patrol_api, gap):
    client, _, _, patrol, _ = patrol_api
    response = client.post("/api/rover", json={"name": "approve", "args": {"gap": gap, "operator": "AB"}})
    assert response.status_code == 400 and "CONFIRMED" in response.json()["detail"]
    assert not patrol.approvals


def test_rover_api_finished_pipeline_uses_cache_and_refuses_unrecordable_approval(patrol_api):
    client, pipeline, _, patrol, _ = patrol_api
    assert client.get("/api/state").json()["patrol"]["gap"] == "Gap 1"
    pipeline.submit(lambda: patrol.value.update(state="DONE", moving=False, done=True))
    pipeline.finish()
    before = patrol.snapshots
    state = client.get("/api/state").json()
    assert state["patrol"]["done"] and not state["pipeline_running"]
    assert patrol.snapshots == before
    response = client.post("/api/rover", json={"name": "approve", "args": {"gap": "Gap 2", "operator": "AB"}})
    assert response.status_code == 400 and "finished" in response.json()["detail"]
    assert not patrol.approvals


def test_rover_api_timed_out_approval_is_cancelled_before_execution(patrol_api):
    client, pipeline, _, patrol, _ = patrol_api
    pipeline.gate.clear()
    try:
        response = client.post("/api/rover", json={"name": "approve", "args": {"gap": "Gap 2", "operator": "AB"}})
        assert response.status_code == 504
    finally:
        pipeline.gate.set()
    pipeline.submit(lambda: None)
    assert not patrol.approvals


def test_rover_api_rejects_non_json_body(patrol_api):
    client, _, _, patrol, _ = patrol_api
    assert client.post("/api/rover", content="{", headers={"content-type": "application/json"}).status_code == 400
    assert not patrol.commands


def test_rover_api_requires_patrol_session(tmp_path):
    from fastapi.testclient import TestClient
    from test_server import make_test_app
    app, *_ = make_test_app(tmp_path)
    with TestClient(app) as client:
        response = client.post("/api/rover", json={"name": "stop"})
        assert response.status_code == 400 and "patrol" in response.json()["detail"]


def test_startup_retries_lost_ping_before_sending_a_move(make_link):
    class BootingSerial(Serial):
        def write(self, payload):
            if payload == b"PING\n" and self.writes.count("PING") < 2:
                self.writes.append("PING")
                self.push("READY rover v1")
                return len(payload)
            return super().write(payload)

    link, serial = make_link(BootingSerial(), cfg={"connect_retry_s": .02})
    assert link.drive(10)
    assert serial.writes == ["PING", "PING", "PING", "DRIVE 10 30"]
    assert not link.estopped


@pytest.mark.parametrize("ready", [False, True])
def test_startup_deadline_refuses_motion_without_pong(make_link, ready):
    class UnresponsiveSerial(Serial):
        def write(self, payload):
            self.writes.append(payload.decode().strip())
            if ready:
                self.push("READY rover v1")
            return len(payload)

    link, serial = make_link(UnresponsiveSerial(),
                             cfg={"connect_timeout_s": .08, "connect_retry_s": .02})
    started = time.monotonic()
    assert not link.drive(10)
    assert time.monotonic() - started < 1
    assert len(serial.writes) >= 2 and set(serial.writes) == {"PING"}
    assert not serial.is_open


def test_startup_retries_do_not_retry_a_failed_motion(make_link):
    link, serial = make_link(Serial(motion_reply=False), cfg={"connect_retry_s": .02})
    assert not link.drive(10)
    assert serial.writes.count("DRIVE 10 30") == 1
    assert "STOP" in serial.writes
