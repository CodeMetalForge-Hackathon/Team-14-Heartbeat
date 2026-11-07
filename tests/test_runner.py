from types import SimpleNamespace
import json
from pathlib import Path
import subprocess
import sys
import threading

import numpy as np

from heartbeat.events import EventBus
from heartbeat.runner import Pipeline


def profile():
    return SimpleNamespace(audio=SimpleNamespace(sample_rate=16000, level_hz=10))


def detection(t=0.0):
    return SimpleNamespace(
        t=t, snr_db=23.1, peak=0.41, gated=False, score=1.0,
        is_knock=True, classifier="none", top=[],
        clip=np.zeros(15600, dtype=np.float32),
    )


class Source:
    def __init__(self, count=10):
        self.blocks = [
            SimpleNamespace(t0=i * 256 / 16000, x=np.zeros(256, dtype=np.float32))
            for i in range(count)
        ]
        self.close_calls = 0

    def __iter__(self):
        yield from self.blocks

    def close(self):
        self.close_calls += 1


class Detector:
    level_db = -40.0
    floor_db = -50.0

    def __init__(self):
        self.calls = 0

    def process(self, block):
        self.calls += 1
        return [detection(block.t0)] if self.calls == 1 else []

    def flush(self):
        return [detection(0.1)]


def make_pipeline(source=None, detector=None):
    bus = EventBus()
    events = []
    bus.subscribe(events.append)
    pipeline = Pipeline(
        source or Source(), detector or Detector(),
        SimpleNamespace(classify=lambda onset: onset), bus, profile(),
    )
    return pipeline, events


def test_worker_stream_time_hooks_and_side_channel():
    pipeline, events = make_pipeline()
    blocks = []
    sounds = []
    threads = []
    pipeline.on_block.append(blocks.append)
    pipeline.on_block.append(lambda t: threads.append(threading.get_ident()))
    pipeline.on_sound.append(sounds.append)
    pipeline.run()

    assert blocks == [(i + 1) * 256 / 16000 for i in range(10)]
    assert pipeline.t == 2560 / 16000
    assert set(threads) != {threading.get_ident()}
    assert len(set(threads)) == 1
    assert len(sounds) == 2  # Includes the final, padded clip from flush().
    assert events[0].type == "session_start"
    assert events[-1].type == "session_end"
    assert events[-1].t == 2560 / 16000
    sound_events = [event for event in events if event.type == "sound"]
    assert len(sound_events) == 2
    assert sound_events[0]._clip is sounds[0].clip
    assert sound_events[0].data["clip"] is None
    for event in events:
        json.dumps(event.to_dict(), allow_nan=False)
    assert pipeline.source.close_calls == 1
    assert not pipeline.running


def test_levels_follow_stream_time_and_hooks_can_fail():
    pipeline, events = make_pipeline(source=Source(100))
    good_hook = []

    def fail(t):
        raise RuntimeError("hook failed")

    pipeline.on_block.extend([fail, good_hook.append])
    pipeline.run()
    levels = [event for event in events if event.type == "level"]
    assert len(levels) == 17
    assert levels[0].t == 256 / 16000
    assert levels[-1].t == 25600 / 16000
    assert len(good_hook) == 100
    assert len([event for event in events if event.type == "error"]) == 100
    assert events[-1].type == "session_end"


def test_stop_from_hook_closes_source_once():
    pipeline, events = make_pipeline(source=Source(100))
    times = []
    pipeline.on_block.extend([times.append, lambda t: pipeline.stop()])
    pipeline.start()
    pipeline.join()
    pipeline.stop()
    assert len(times) == 1
    assert pipeline.source.close_calls == 1
    assert events[-1].type == "session_end"


def test_source_failure_reports_error_and_ends_session():
    class BrokenSource(Source):
        def __iter__(self):
            raise RuntimeError("device disconnected")
            yield

    pipeline, events = make_pipeline(source=BrokenSource())
    pipeline.run()
    errors = [event for event in events if event.type == "error"]
    assert errors[0].data["where"] == "source"
    assert "device disconnected" in errors[0].data["message"]
    assert events[-1].type == "session_end"
    assert pipeline.source.close_calls == 1


def test_detector_and_classifier_failures_are_logged():
    class BrokenDetector(Detector):
        def process(self, block):
            raise RuntimeError("bad detector")

    pipeline, events = make_pipeline(source=Source(1), detector=BrokenDetector())

    def broken_classify(onset):
        raise RuntimeError("bad classifier")

    pipeline.classifier.classify = broken_classify
    pipeline.run()
    errors = [event.data["where"] for event in events if event.type == "error"]
    assert errors == ["detector", "classifier"]
    assert events[-1].type == "session_end"


def test_sigint_waits_for_flush_and_session_end_in_subprocess():
    # Isolate real SIGINT from pytest and hold flush open long enough to expose
    # Python 3.11's interrupted Thread.join() race, without touching hardware.
    code = r'''
import os
import signal
import threading
import time
from types import SimpleNamespace
from heartbeat.runner import Pipeline

class Source:
    def __init__(self):
        self.ready = threading.Event()
        self.released = threading.Event()
    def __iter__(self):
        self.ready.set()
        self.released.wait()
        return
        yield
    def close(self):
        self.released.set()

class Detector:
    def __init__(self):
        self.flushing = threading.Event()
        self.release = threading.Event()
        self.flushed = False
    def flush(self):
        self.flushing.set()
        assert self.release.wait(5), "flush was never released"
        self.flushed = True
        return []

class Bus:
    def __init__(self):
        self.events = []
    def publish(self, kind, t, **data):
        self.events.append(kind)

source, detector, bus = Source(), Detector(), Bus()
profile = SimpleNamespace(audio=SimpleNamespace(sample_rate=16000, level_hz=10))
pipeline = Pipeline(source, detector, None, bus, profile)

def interrupt():
    assert source.ready.wait(5), "source did not start"
    time.sleep(0.05)  # Let run() enter its blocking wait before sending SIGINT.
    os.kill(os.getpid(), signal.SIGINT)
    assert detector.flushing.wait(5), "shutdown never reached flush"
    time.sleep(0.05)
    detector.release.set()

sender = threading.Thread(target=interrupt, daemon=True)
sender.start()
pipeline.run()
assert detector.flushed, "run returned before flush completed"
assert bus.events == ["session_start", "session_end"], bus.events
assert not pipeline.running
print("completed")
'''
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[1],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "completed"
