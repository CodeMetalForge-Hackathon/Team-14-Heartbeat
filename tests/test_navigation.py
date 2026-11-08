"""Navigation protocol and audio interruption with fake serial/motors."""
from dataclasses import replace
from types import SimpleNamespace
import threading

import pytest

from heartbeat.config import load_profile
from heartbeat.dialog import DialogEngine
from heartbeat.events import EventBus
from heartbeat.patrol import Patrol, load_route
from heartbeat.rover import FakeRover, navigation_args, parse_reply, _Pending
from test_patrol import ManualPipeline
from test_rover import Serial, make_link


@pytest.mark.parametrize("line", ["RANGE port=10 cm=100.00 valid=1", "RANGE port=10 cm=-1.00 valid=0",
                                  "NAV TURN 25.00", "NAV OBSTACLE 30.00", "OK EXPLORE 120",
                                  "OK NAVCFG 10 30 45 20 45 8 80 200 15"])
def test_protocol_additions(line):
    assert parse_reply(line)["raw"] == line


@pytest.mark.parametrize("line", ["RANGE port=1 cm=100 valid=1", "RANGE port=10 cm=nan valid=1",
                                  "RANGE port=10 cm=-1 valid=1", "RANGE port=10 cm=100 valid=0",
                                  "NAV FORWARD -1", "NAV FLY 50", "OK NAVCFG 1 30 45 20 45 8 80 200 15"])
def test_bad_navigation_replies(line):
    with pytest.raises(ValueError):
        parse_reply(line)


@pytest.mark.parametrize("change", [{"sensor_port": 1}, {"sensor_port": True}, {"poll_ms": 1000},
                                     {"clear_cm": 30}, {"max_turns": 2.5}, {"step_cm": 100},
                                     {"turn_clearance_cm": 30}, {"turn_deg": 180}])
def test_navigation_limits(change):
    cfg = {**load_profile().rover["navigation"], **change}
    with pytest.raises(ValueError):
        navigation_args(cfg)


def test_range_replies_match_requested_port():
    waiter = _Pending("RANGE", (10,))
    assert waiter.matches(parse_reply("RANGE port=10 cm=100 valid=1"))
    assert not waiter.matches(parse_reply("RANGE port=9 cm=100 valid=1"))


class NavigationSerial(Serial):
    def write(self, payload):
        line = payload.decode().strip()
        if line.startswith("EXPLORE") or line.startswith("RANGE"):
            with self.condition:
                self.writes.append(line)
                self.condition.notify_all()
            if line.startswith("RANGE"):
                self.push("RANGE port=10 cm=100 valid=1")
            return len(payload)
        return super().write(payload)


def test_sensor_check_does_not_move(make_link):
    link, serial = make_link(NavigationSerial())
    assert link.range(10)["cm"] == 100
    assert not link.moving and not any(w.startswith(("DRIVE", "TURN", "EXPLORE")) for w in serial.writes)


def test_explore_preserves_telemetry_and_stop_cancels_without_completion(make_link):
    bus = EventBus(); events = []
    bus.subscribe(events.append)
    link, serial = make_link(NavigationSerial(), bus=bus)
    assert link.configure_navigation(load_profile().rover["navigation"])
    result = []
    worker = threading.Thread(target=lambda: result.append(link.explore(120, 30)))
    worker.start()
    try:
        assert serial.wait_command("EXPLORE")
        observed = threading.Event()
        bus.subscribe(lambda e: observed.set() if e.type == "rover" and
                      e.data.get("reply", {}).get("kind") == "NAV" else None)
        serial.push("NAV OBSTACLE 29.00")
        assert observed.wait(1)
        # NAV is progress only, never an acknowledgement of EXPLORE.
        assert not _Pending("EXPLORE", (120, 30)).matches(parse_reply("NAV TURN 29"))
        assert link.stop()
        worker.join(1)
        assert result == [False] and not link.moving
        assert "STOP" in serial.writes
    finally:
        link.close(); worker.join(1)


@pytest.mark.parametrize("fault", ["range_unknown", "navigation_blocked", "obstacle_too_close"])
def test_navigation_fault_never_completes_move(make_link, fault):
    link, serial = make_link(NavigationSerial())
    result=[]
    worker=threading.Thread(target=lambda: result.append(link.explore(120,30)))
    worker.start()
    try:
        assert serial.wait_command("EXPLORE")
        serial.push("ERR " + fault)
        worker.join(1)
        assert result == [False] and not link.moving and "STOP" in serial.writes
    finally:
        link.close(); worker.join(1)


@pytest.fixture
def exploring():
    profile=load_profile("clap-knocks")
    profile=replace(profile,dialog={**profile.dialog,"auto_challenge":False})
    pipeline=ManualPipeline()
    engine=DialogEngine(profile,lambda *args:None,rng_seed=1)
    def command(name):
        engine.command(name,pipeline.t)
        return {"ok":True}
    session=SimpleNamespace(engine=engine,command_result=command,_active=None)
    rover=FakeRover(cfg=profile.rover,clock=lambda:pipeline.t)
    patrol=Patrol(rover,pipeline,session,SimpleNamespace(),
                  [{"name":"Explore 1","moves":[["explore",120]]}],profile.rover)
    patrol.start();pipeline.step(0)
    yield pipeline,patrol,rover
    patrol.close()


def test_exploration_is_configured_and_bounded(exploring):
    pipeline,patrol,rover=exploring
    assert [e["command"] for e in rover.trace][:2]==["NAVCFG","EXPLORE"]
    pipeline.step(120);pipeline.step(120.1)
    assert patrol.state=="DONE" and not rover.moving


def test_knock_stops_exploration_and_review_resume_starts_new_budget(exploring):
    pipeline,patrol,rover=exploring
    pipeline.step(3)
    pipeline.bus.publish("sound",2,is_knock=True,gated=True,score=.9,classifier="clap")
    assert patrol.state=="STOPPING" and not rover.moving
    pipeline.step(3.1);pipeline.step(5.1)
    assert patrol.state=="VERIFYING"
    pipeline.step(13.2)
    assert patrol.state=="REVIEW" and not rover.moving
    pipeline.step(50)
    assert patrol.state=="REVIEW"
    assert patrol.command("resume")["ok"]
    pipeline.step(50.1)
    assert patrol.state=="MOVING" and rover.moving and patrol.gap_name=="Explore 2"
    assert [e["command"] for e in rover.trace].count("EXPLORE")==2


def test_estop_cannot_resume_exploration(exploring):
    pipeline,patrol,rover=exploring
    assert patrol.command("estop")["ok"]
    pipeline.step(1)
    assert not rover.moving and patrol.done and not patrol.command("resume")["ok"]


@pytest.mark.parametrize("seconds", [0,-1,601,float("nan")])
def test_exploration_route_requires_bounded_positive_duration(seconds):
    with pytest.raises(ValueError):
        load_route([{"name":"Explore","moves":[["explore",seconds]]}])


def test_explore_acknowledgement_completes_only_matching_duration(make_link):
    link, serial = make_link(NavigationSerial())
    result=[]
    worker=threading.Thread(target=lambda: result.append(link.explore(120,30)))
    worker.start()
    try:
        assert serial.wait_command("EXPLORE")
        assert not _Pending("EXPLORE", (120,30)).matches(parse_reply("OK EXPLORE 1"))
        serial.push("OK EXPLORE 120")
        worker.join(1)
        assert result == [True] and not link.moving
    finally:
        link.close(); worker.join(1)
