"""Serial microphone contracts exercised entirely with byte streams and fakes."""

from dataclasses import replace
import math
import queue
import struct
import threading
from types import SimpleNamespace

import numpy as np
import pytest
import serial
import serial.tools.list_ports

from heartbeat import sources, stickmic
from heartbeat.config import load_profile
from heartbeat.events import EventBus
from heartbeat.prompter import CompositePrompter, SerialKnocker, make_prompter


@pytest.fixture(autouse=True)
def no_hardware(monkeypatch):
    """An accidental default serial open or inventory must fail the test."""
    def forbidden(*args, **kwargs):
        raise AssertionError("tests must not access serial hardware or inventory")

    monkeypatch.setattr(serial, "Serial", forbidden)
    monkeypatch.setattr(serial.tools.list_ports, "comports", forbidden)
    monkeypatch.setattr(stickmic, "_os_port_in_use", lambda port: False)
    monkeypatch.setattr(stickmic, "_PORTS", set())


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now


class SerialBytes:
    """No-port Serial() fake that checks line state at the instant of open."""

    def __init__(self, clock, *, stream=b"", max_read=520,
                 ready=b"READY stickmic v1 rate=16000\n", pong=b"PONG stickmic v1\n"):
        self.clock = clock
        self.stream = stream
        self.max_read = max_read
        self.pending = bytearray(ready)
        self.pong = pong
        self.is_open = False
        self.dtr = self.rts = True
        self.writes = []
        self.opened_at = self.closed_at = None
        self.read_sizes = []
        self.streaming = False

    def open(self):
        assert self.dtr is False and self.rts is False
        assert self.exclusive is True
        assert self.port
        self.opened_at = self.clock.now
        self.is_open = True

    def read(self, size):
        self.read_sizes.append(size)
        if not self.pending:
            self.clock.now += max(self.timeout, 0.0001)
            return b""
        self.clock.now += 0.0001
        size = min(size, self.max_read)
        result = bytes(self.pending[:size])
        del self.pending[:size]
        return result

    def write(self, payload):
        self.writes.append(payload)
        if payload == b"STOP\n":
            self.streaming = False
        elif payload == b"PING\n" and not self.streaming and self.pong is not None:
            self.pending.extend(self.pong)
        elif payload == b"START\n":
            self.streaming = True
            self.pending.extend(self.stream)
        return len(payload)

    def close(self):
        self.is_open = False
        self.closed_at = self.clock.now


def reference_crc(payload):
    """Independent wire-fixture generator, checked against the CCITT vector."""
    value = 0xFFFF
    for byte in payload:
        value ^= byte << 8
        for _ in range(8):
            value = ((value << 1) ^ (0x1021 if value & 0x8000 else 0)) & 0xFFFF
    return value


def frame(seq, samples=None):
    if samples is None:
        samples = np.full(256, 4096, dtype="<i2")
    samples = np.asarray(samples, dtype="<i2")
    payload = struct.pack("<HH", seq, len(samples)) + samples.tobytes()
    return b"\xa5\x5a" + payload + struct.pack("<H", reference_crc(payload))


def source_fixture(stream, **kwargs):
    clock = Clock()
    port_kwargs = {key: kwargs.pop(key) for key in ("max_read", "ready", "pong") if key in kwargs}
    port = SerialBytes(clock, stream=stream, **port_kwargs)
    bus, events = EventBus(), []
    bus.subscribe(events.append)
    source = stickmic.StickMicSource(
        "/dev/fake-stick", bus=bus, serial_factory=lambda: port,
        monotonic=clock.monotonic, **kwargs,
    )
    return source, port, events, clock


def take_blocks(source, count):
    iterator = iter(source)
    try:
        return [next(iterator) for _ in range(count)]
    finally:
        iterator.close()


def assert_times(blocks):
    assert [block.t0 for block in blocks] == [index * 256 / 16000 for index in range(len(blocks))]
    assert all(block.x.shape == (256,) and block.x.dtype == np.float32 for block in blocks)


def test_crc_matches_ccitt_false_known_vector():
    assert reference_crc(b"123456789") == 0x29B1
    assert stickmic.crc16(b"123456789") == 0x29B1
    assert len(frame(0)) == 520


@pytest.mark.parametrize("max_read", [1, 2, 7, 519, 520])
def test_clean_frames_and_partial_reads_have_exact_samples_and_stream_time(max_read):
    samples = np.resize(np.array([-32768, -1, 0, 1, 32767], dtype="<i2"), 256)
    source, port, events, _ = source_fixture(frame(0, samples) + frame(1, -samples), max_read=max_read)
    blocks = take_blocks(source, 2)
    assert_times(blocks)
    np.testing.assert_array_equal(blocks[0].x, samples.astype(np.float32) / 32768)
    np.testing.assert_array_equal(blocks[1].x, (-samples).astype(np.float32) / 32768)
    assert port.baudrate == 921600
    assert port.writes == [b"PING\n", b"START\n", b"STOP\n"]
    assert not port.is_open and not stickmic.port_in_use(source.port)
    assert not events


def test_sequence_gap_inserts_exact_silence_and_warning():
    source, _, events, _ = source_fixture(frame(0) + frame(3))
    blocks = take_blocks(source, 4)
    assert_times(blocks)
    assert np.any(blocks[0].x) and np.any(blocks[3].x)
    assert not blocks[1].x.any() and not blocks[2].x.any()
    warning = next(event for event in events if event.data.get("code") == "stick_frame_gap")
    assert warning.type == "error" and warning.data["severity"] == "warning"
    assert warning.t == 0.016 and warning.data["missing_blocks"] == 2
    assert warning.data["missing_frames"] == 2
    assert source.missing_frames == 2


def test_first_missing_frames_are_also_filled_from_start_sequence_zero():
    source, _, _, _ = source_fixture(frame(2))
    blocks = take_blocks(source, 3)
    assert_times(blocks)
    assert not blocks[0].x.any() and not blocks[1].x.any()
    assert np.any(blocks[2].x)


def test_bad_crc_is_dropped_and_replaced_with_silence():
    bad = bytearray(frame(1))
    bad[-1] ^= 0x80
    source, _, events, _ = source_fixture(frame(0) + bad + frame(2))
    blocks = take_blocks(source, 3)
    assert_times(blocks)
    assert not blocks[1].x.any() and np.any(blocks[2].x)
    assert source.bad_frames == 1 and source.missing_frames == 1
    assert {event.data["code"] for event in events} == {"stick_bad_frame", "stick_frame_gap"}


def test_garbage_and_invalid_count_resynchronize_to_the_next_frame():
    garbage = b"garbage\xa5broken\xa5"
    invalid = frame(91, np.zeros(128, dtype=np.int16))
    source, _, events, _ = source_fixture(frame(0) + garbage + invalid + frame(1), max_read=3)
    blocks = take_blocks(source, 2)
    assert_times(blocks)
    assert all(block.x.any() for block in blocks)
    assert source.garbage_bytes >= len(garbage)
    assert source.bad_frames == 1
    assert any(event.data.get("code") == "stick_bad_frame" for event in events)


def test_sequence_wraparound_preserves_time_without_a_false_gap():
    source, _, events, _ = source_fixture(b"".join(frame(seq) for seq in (65534, 65535, 0, 1)))
    blocks = take_blocks(source, 4)
    assert_times(blocks)
    assert all(block.x.any() for block in blocks)
    assert source.missing_frames == 0 and not events


def test_gap_across_sequence_wrap_is_one_missing_frame():
    source, _, _, _ = source_fixture(frame(65535) + frame(1))
    blocks = take_blocks(source, 3)
    assert_times(blocks)
    assert not blocks[1].x.any() and source.missing_frames == 1


def test_duplicate_frame_is_ignored_instead_of_inserting_a_huge_gap():
    source, _, events, _ = source_fixture(frame(0) + frame(0) + frame(1))
    blocks = take_blocks(source, 2)
    assert_times(blocks)
    assert source.missing_frames == 0 and source.stale_frames == 1
    assert any(event.data.get("code") == "stick_stale_frame" for event in events)


def test_gain_is_applied_and_samples_stay_in_audio_contract_range():
    samples = np.resize(np.array([-32768, -16384, 8192, 16384, 32767], dtype="<i2"), 256)
    source, _, _, _ = source_fixture(frame(0, samples), gain_db=20 * math.log10(2))
    block = take_blocks(source, 1)[0]
    expected = np.clip(samples.astype(np.float32) / 32768 * 2, -1, 1)
    np.testing.assert_allclose(block.x, expected, rtol=0, atol=1e-7)


def test_slow_consumer_does_not_look_like_a_serial_transport_stall():
    source, _, events, clock = source_fixture(frame(0) + frame(1))
    iterator = iter(source)
    try:
        first = next(iterator)
        clock.now += 10
        second = next(iterator)
        assert_times([first, second])
        assert not events
    finally:
        iterator.close()


def test_source_recovers_an_abandoned_binary_stream_without_reset():
    source, port, events, _ = source_fixture(frame(0), ready=b"\xa5\x5a" + b"\x00" * 1400)
    port.streaming = True
    assert len(take_blocks(source, 1)) == 1
    assert port.writes[:3] == [b"STOP\n", b"PING\n", b"START\n"]
    assert not events


@pytest.mark.parametrize("pong", [b"PONG knocker v1\n", b"PONG stickmic v1 extra\n"])
def test_wrong_serial_identity_is_reported_and_never_started(pong):
    source, port, events, _ = source_fixture(frame(0), pong=pong)
    assert list(source) == []
    assert b"START\n" not in port.writes
    assert not port.is_open and not stickmic.port_in_use(source.port)
    assert events[0].data["code"] == "stick_open_failed"


def test_busy_source_is_not_opened_and_reports_an_error():
    source, port, events, _ = source_fixture(frame(0))
    assert stickmic.claim_port(source.port)
    assert list(source) == []
    assert port.opened_at is None
    assert events[0].data["code"] == "stick_open_failed"


def test_send_status_sanitizes_ascii_and_close_sends_stop_once():
    source, port, _, _ = source_fixture(frame(0))
    iterator = iter(source)
    try:
        next(iterator)
        assert source.send_status("CONFIRMED", "two\npeople · " + "x" * 80)
        payload = port.writes[-1].decode("ascii")
        prefix, status, text = payload.rstrip("\n").split(" ", 2)
        assert prefix == "STATUS" and status == "CONFIRMED"
        assert "\n" not in text and len(text) <= 40
        source.close()
        source.close()
        assert port.writes.count(b"STOP\n") == 1
        assert not source.send_status("NO_SIGNS", "closed")
    finally:
        iterator.close()


@pytest.mark.parametrize("spec", [None, 0, 2, "Built-in Microphone", "2", "stick-shaped mic"])
@pytest.mark.parametrize("whole_profile", [False, True])
def test_open_input_preserves_regular_microphone_selection(monkeypatch, spec, whole_profile):
    audio = load_profile().audio
    cfg = SimpleNamespace(audio=audio) if whole_profile else audio
    bus, calls = EventBus(), []
    sentinel = object()

    def fake_mic(**kwargs):
        calls.append(kwargs)
        return sentinel

    monkeypatch.setattr(sources, "MicSource", fake_mic)
    assert sources.open_input(spec, cfg, bus) is sentinel
    assert calls == [{"device": spec, "block": 256, "cfg": audio, "bus": bus}]


@pytest.mark.parametrize("spec, expected", [
    ("stick", "/dev/fake-auto"),
    ("stick:/dev/ttyUSB9", "/dev/ttyUSB9"),
    ("stick: COM7 ", "COM7"),
])
def test_open_input_routes_sticks_and_passes_profile_gain(monkeypatch, spec, expected):
    audio = replace(load_profile().audio, stick_baud=460800, stick_gain_db=6.0)
    bus, scans, calls = EventBus(), [], []
    sentinel = object()

    def find(**kwargs):
        scans.append(kwargs)
        return ["/dev/fake-auto", "/dev/fake-other"]

    def construct(port, **kwargs):
        calls.append((port, kwargs))
        return sentinel

    monkeypatch.setattr(sources, "find_stick_ports", find)
    monkeypatch.setattr(sources, "StickMicSource", construct)
    assert sources.open_input(spec, SimpleNamespace(audio=audio), bus) is sentinel
    assert calls == [(expected, {"baud": 460800, "gain_db": 6.0, "bus": bus})]
    assert scans == ([{"bus": bus}] if spec == "stick" else [])


@pytest.mark.parametrize("spec, message", [("stick", "No M5StickC"), ("stick:", "must follow"),
                                         ("stick:   ", "must follow")])
def test_open_input_reports_missing_sticks_and_empty_port(monkeypatch, spec, message):
    monkeypatch.setattr(sources, "find_stick_ports", lambda **kwargs: [])
    with pytest.raises(ValueError, match=message):
        sources.open_input(spec, load_profile())


def test_find_sticks_skips_busy_ports_rejects_wrong_identity_and_is_fast(monkeypatch):
    clock, opened = Clock(), []
    external = stickmic.canonical_port("/dev/external-busy")
    monkeypatch.setattr(stickmic, "_os_port_in_use", lambda port: port == external)
    assert stickmic.claim_port("/dev/internal-busy")
    replies = {"/dev/knocker": b"PONG knocker v1\n", "/dev/malformed": b"PONG stickmic v1 extra\n",
               "/dev/silent": None, "/dev/stick": b"PONG stickmic v1\n"}

    class DiscoveryPort(SerialBytes):
        def open(self):
            super().open()
            self.pong = replies[self.port]
            opened.append(self)

    ports = ["/dev/internal-busy", "/dev/external-busy", *replies,
             SimpleNamespace(device="/dev/entry-busy", is_open=True)]
    found = stickmic.find_stick_ports(
        serial_factory=lambda: DiscoveryPort(clock, ready=b""),
        port_lister=lambda: ports, monotonic=clock.monotonic,
    )
    assert found == ["/dev/stick"]
    assert {port.port for port in opened} == set(replies)
    assert all(not port.is_open and port.closed_at - port.opened_at <= 0.5 for port in opened)
    assert all(port.writes == [b"STOP\n", b"PING\n"] for port in opened)
    assert all(not stickmic.port_in_use(port) for port in replies)


def test_find_stick_recovers_binary_bytes_before_the_pong():
    clock = Clock()
    port = SerialBytes(clock, ready=b"\xa5\x5a" + b"\x00" * 100)
    port.streaming = True
    assert stickmic.find_stick_ports(
        serial_factory=lambda: port, port_lister=lambda: ["/dev/orphan"],
        monotonic=clock.monotonic,
    ) == ["/dev/orphan"]
    assert not port.is_open and not stickmic.port_in_use("/dev/orphan")


@pytest.mark.parametrize("kind", ["source", "discovery"])
def test_printable_binary_suffix_requires_a_fresh_clean_pong(kind):
    clock = Clock()

    class PrintableSuffixPort(SerialBytes):
        first_ping = True

        def write(self, payload):
            if payload == b"PING\n" and self.first_ping:
                self.first_ping = False
                # The last queued PCM/CRC bytes can all be printable ASCII.
                self.pending.extend(b"abc")
            return super().write(payload)

    port = PrintableSuffixPort(clock, ready=b"", stream=frame(0))
    if kind == "source":
        source = stickmic.StickMicSource("/dev/orphan", serial_factory=lambda: port,
                                        monotonic=clock.monotonic)
        assert len(take_blocks(source, 1)) == 1
    else:
        assert stickmic.find_stick_ports(
            serial_factory=lambda: port, port_lister=lambda: ["/dev/orphan"],
            monotonic=clock.monotonic,
        ) == ["/dev/orphan"]
    assert port.writes[:3] == [b"STOP\n", b"PING\n", b"PING\n"]
    assert not port.is_open


def test_status_sink_formats_each_supported_event_and_ignores_audio_events():
    output = queue.Queue()
    target = SimpleNamespace(send_status=lambda *item: output.put(item))
    sink, bus = stickmic.StickStatusSink(target), EventBus()
    bus.subscribe(sink)
    try:
        bus.publish("state", 0, status="POSSIBLE", answers={}, odds="1 in 1")
        assert output.get(timeout=1) == ("POSSIBLE", "")
        bus.publish("round_result", 1, odds="1 in 2.3M")
        assert output.get(timeout=1) == ("POSSIBLE", "1 in 2.3M")
        bus.publish("answer", 2, question="q_people", value=2)
        assert output.get(timeout=1) == ("POSSIBLE", "1 in 2.3M | 2 people")
        bus.publish("answer", 3, question="q_move", value="yes")
        status, text = output.get(timeout=1)
        assert status == "POSSIBLE" and "2 people" in text and "can move" in text
        bus.publish("state", 4, status="CONFIRMED", answers={"q_people": 2}, odds="x" * 100)
        assert output.get(timeout=1) == ("CONFIRMED", "x" * 40)
        bus.publish("level", 5, dbfs=-12)
        assert output.empty()
    finally:
        sink.close()


def test_status_sink_does_not_block_publishers_when_the_serial_writer_is_blocked():
    started, release, published = threading.Event(), threading.Event(), threading.Event()
    output = queue.Queue()

    def blocked_write(*item):
        output.put((threading.current_thread().name, item))
        started.set()
        release.wait(timeout=2)
        return True

    sink = stickmic.StickStatusSink(SimpleNamespace(send_status=blocked_write))
    bus = EventBus()
    bus.subscribe(sink)
    publisher = None
    try:
        bus.publish("state", 0, status="POSSIBLE", answers={}, odds="1 in 1")
        assert started.wait(timeout=1)
        output.get(timeout=1)

        def publish_many():
            for index in range(100):
                bus.publish("round_result", index, odds=f"1 in {index + 1}")
            bus.publish("state", 101, status="CONFIRMED", answers={"q_people": 2, "q_move": "yes"},
                        odds="1 in 2.3M")
            published.set()

        publisher = threading.Thread(target=publish_many, name="test-publisher", daemon=True)
        publisher.start()
        assert published.wait(timeout=1), "event publication waited for the serial writer"
        release.set()
        name, (status, text) = output.get(timeout=1)
        assert name == "stick-status" and status == "CONFIRMED"
        assert "2 people" in text and "can move" in text and len(text) <= 40
    finally:
        release.set()
        sink.close()
        if publisher is not None:
            publisher.join(timeout=1)


def knocker_factory(clock, replies, opened):
    def construct(port, **kwargs):
        connection = SerialBytes(clock, ready=b"READY knocker v1\n", pong=replies[port])
        # The original injected knocker factory returns an already-open port.
        connection.is_open = True
        connection.port = port
        connection.timeout = kwargs["timeout"]
        opened.append(connection)
        return connection
    return construct


def test_knocker_auto_skips_exclusions_and_busy_ports_and_rejects_stick_pong(monkeypatch):
    clock, opened = Clock(), []
    assert stickmic.claim_port("/dev/internal-busy")
    monkeypatch.setattr(stickmic, "_os_port_in_use",
                        lambda port: port == stickmic.canonical_port("/dev/external-busy"))
    replies = {"/dev/stick": b"PONG stickmic v1\n", "/dev/z-knocker": b"PONG knocker v1\n"}
    prompter = make_prompter(
        "auto", exclude_ports={"/dev/excluded"},
        serial_factory=knocker_factory(clock, replies, opened), monotonic=clock.monotonic,
        port_lister=lambda: ["/dev/internal-busy", "/dev/external-busy", "/dev/excluded", *replies,
                             SimpleNamespace(device="/dev/entry-busy", is_open=True)],
        audio=SimpleNamespace(stop=lambda: None),
    )
    try:
        assert isinstance(prompter, CompositePrompter)
        assert prompter.knocks.port == "/dev/z-knocker"
        assert [port.port for port in opened] == ["/dev/stick", "/dev/z-knocker"]
        assert opened[0].is_open is False
        assert all(port.writes == [b"PING\n"] for port in opened)
        assert stickmic.port_in_use("/dev/z-knocker")
        assert not stickmic.port_in_use("/dev/stick")
    finally:
        prompter.close()
    assert not stickmic.port_in_use("/dev/z-knocker")


@pytest.mark.parametrize("reply", [b"PONG stickmic v1\n", b"PONG knocker v1 extra\n",
                                   b"PONG knocker v10\n"])
def test_knocker_probe_requires_the_exact_knocker_identity(reply):
    clock, opened = Clock(), []
    candidate = SerialKnocker("/dev/wrong", serial_factory=knocker_factory(clock, {"/dev/wrong": reply}, opened),
                              monotonic=clock.monotonic)
    assert candidate.probe() is False
    assert not opened[0].is_open and not stickmic.port_in_use("/dev/wrong")


def test_knocker_default_open_keeps_reset_lines_inactive_and_is_exclusive(monkeypatch):
    clock = Clock()
    connection = SerialBytes(clock, ready=b"READY knocker v1\n", pong=b"PONG knocker v1\n")
    monkeypatch.setattr(serial, "Serial", lambda: connection)
    candidate = SerialKnocker("/dev/no-reset", monotonic=clock.monotonic)
    try:
        assert candidate.probe()
        assert connection.baudrate == 115200
        assert connection.writes == [b"PING\n"]
    finally:
        candidate.close()
    assert not connection.is_open and not stickmic.port_in_use(candidate.port)


@pytest.mark.parametrize("kind", ["source", "knocker"])
def test_unplugged_port_symlink_does_not_leak_the_claim(tmp_path, kind):
    device = tmp_path / "device"
    device.touch()
    alias = tmp_path / "by-id"
    alias.symlink_to(device)
    clock = Clock()
    if kind == "source":
        connection = SerialBytes(clock, stream=frame(0))
        owner = stickmic.StickMicSource(str(alias), serial_factory=lambda: connection,
                                        monotonic=clock.monotonic)
        iterator = iter(owner)
        next(iterator)
    else:
        opened = []
        owner = SerialKnocker(str(alias), serial_factory=knocker_factory(
            clock, {str(alias): b"PONG knocker v1\n"}, opened), monotonic=clock.monotonic)
        assert owner.probe()
    assert stickmic.port_in_use(str(device))
    alias.unlink()
    owner.close()
    if kind == "source":
        iterator.close()
    assert not stickmic.port_in_use(str(device))


def test_mac_tty_alias_is_excluded_from_knocker_scan(monkeypatch):
    monkeypatch.setattr(stickmic.sys, "platform", "darwin")
    opened = []
    candidate = SerialKnocker("/dev/cu.fake", exclude_ports={"/dev/tty.fake"},
                              serial_factory=lambda *args, **kwargs: opened.append(args))
    assert not candidate.probe()
    assert opened == []


def test_devices_lists_fake_sticks_even_when_standard_audio_is_absent(monkeypatch, capsys):
    from heartbeat import cli

    bus = EventBus()
    monkeypatch.setattr(cli, "list_devices", lambda **kwargs: [])
    monkeypatch.setattr(cli, "find_stick_ports", lambda **kwargs: ["/dev/fake-stick"])
    assert cli._devices(bus) == 0
    assert "stick:/dev/fake-stick" in capsys.readouterr().out


def test_run_excludes_the_stick_from_knocker_scan_and_closes_status_sink(monkeypatch, tmp_path):
    from heartbeat import cli, prompter

    bus, calls, closed, observed = EventBus(), {}, [], []
    source = stickmic.StickMicSource("/dev/fake-stick")
    pipeline = SimpleNamespace(source=source, classifier=object(), t=0.0)

    class StatusSink:
        def __init__(self, target):
            assert target is source

        def __call__(self, event):
            observed.append(event.type)

        def close(self):
            closed.append("status")

    class Runtime:
        def __init__(self):
            self.pipeline = pipeline
            self.session = object()
            self.on_close = []

        def run(self):
            bus.publish("state", 0, state="LISTENING", status="NO_SIGNS", odds="1 in 1")

        def close(self):
            for callback in self.on_close:
                callback()
            self.on_close.clear()
            source.close()

    def prepare(profile, source_factory, prompter_factory, **kwargs):
        assert source_factory() is source
        prompter_factory(pipeline)
        return Runtime()

    def make(spec, *args, **kwargs):
        calls.update(kwargs)
        return object()

    monkeypatch.setattr(cli, "open_input", lambda *args: source)
    monkeypatch.setattr(cli, "prepare_runtime", prepare)
    monkeypatch.setattr(cli, "StickStatusSink", StatusSink)
    monkeypatch.setattr(cli, "_run_directory", lambda *args: tmp_path / "fake-run")
    monkeypatch.setattr(cli, "_operator_input", lambda *args: None)
    monkeypatch.setattr(prompter, "make_prompter", make)
    args = SimpleNamespace(auto=False, wav=None, device="stick", knocker="auto", speaker_device=None,
                           dashboard=False, _argv=["hb", "run", "--device", "stick"])
    assert cli._run(args, load_profile(), bus) == 0
    assert calls["exclude_ports"] == {source.port}
    assert observed == ["state"] and closed == ["status"]
    bus.publish("state", 1, state="LISTENING", status="NO_SIGNS", odds="1 in 1")
    assert observed == ["state"]
