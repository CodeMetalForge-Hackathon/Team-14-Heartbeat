import io
import json
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from heartbeat.events import ConsoleSink, Event, EventBus, to_jsonable


def test_numpy_values_are_strict_json_serializable(tmp_path):
    value = {
        "score": np.float32(0.5),
        "count": np.int64(3),
        "gated": np.bool_(False),
        "top": [("knock", np.float64(0.9))],
        "values": np.array([1.0, np.nan, np.inf]),
        "path": tmp_path,
    }
    converted = to_jsonable(value)
    assert converted["values"] == [1.0, None, None]
    assert json.loads(json.dumps(converted, allow_nan=False))["count"] == 3


def test_clip_is_only_a_side_channel():
    bus = EventBus()
    received = []
    bus.subscribe(received.append)
    clip = np.ones(15600, dtype=np.float32)
    event = bus.publish("sound", np.float64(1.25), score=np.float32(0.7), _clip=clip)
    assert received == [event]
    assert event._clip is clip
    assert event.clip is clip
    assert set(event.to_dict()) == {"type", "t", "wall", "data"}
    assert "_clip" not in event.data
    assert "clip" not in event.data
    assert json.loads(json.dumps(event.to_dict(), allow_nan=False))["t"] == 1.25


def test_failing_subscriber_reports_error_without_recursive_failures():
    bus = EventBus()
    received = []
    calls = []

    def fails(event):
        calls.append(event.type)
        raise RuntimeError("broken sink")

    bus.subscribe(fails)
    bus.subscribe(received.append)
    bus.subscribe(fails)
    bus.publish("sound", 2.0, score=np.float32(1))
    assert [event.type for event in received] == ["sound", "error", "error"]
    assert calls == ["sound", "sound"]
    assert received[1].data["event_type"] == "sound"
    assert "broken sink" in received[1].data["message"]
    for event in received:
        json.dumps(event.to_dict(), allow_nan=False)


def test_bus_is_synchronous_and_serializes_concurrent_publications():
    bus = EventBus()
    received = []
    remove = bus.subscribe(received.append)

    def publish(index):
        event = bus.publish("level", index / 10, dbfs=np.float32(-50))
        assert event in received

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(publish, range(100)))
    assert len(received) == 100
    assert len({event.t for event in received}) == 100
    remove()
    bus.publish("session_end", 10)
    assert len(received) == 100


def test_console_sink_prints_sound_and_errors():
    output = io.StringIO()
    sink = ConsoleSink(output)
    sink(Event("sound", 12.48, data={"is_knock": True, "snr_db": 23.1, "peak": 0.41, "classifier": "none"}))
    sink(Event("error", 13, data={"message": "Input unavailable"}))
    assert "12.48s  KNOCK  snr 23.1 dB  peak 0.41  (none)" in output.getvalue()
    assert "Input unavailable" in output.getvalue()
