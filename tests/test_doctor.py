"""P06 diagnostics use fakes; no test opens a device or contacts a network."""

from copy import deepcopy
from dataclasses import replace
import json
import math
import resource
import socket
import subprocess
import threading
from types import SimpleNamespace

import numpy as np
import pytest
import serial
import serial.tools.list_ports
import yaml

from heartbeat import audio, cli, doctor, stickmic
from heartbeat.audio import AudioBlock
from heartbeat.config import load_profile


@pytest.fixture(autouse=True)
def no_live_probes(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("diagnostic tests must mock hardware, processes and networking")

    monkeypatch.setattr(serial, "Serial", forbidden)
    monkeypatch.setattr(serial.tools.list_ports, "comports", forbidden)
    monkeypatch.setattr(audio, "_sounddevice", forbidden)
    monkeypatch.setattr(doctor, "_sounddevice", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(socket, "gethostname", forbidden)
    monkeypatch.setattr(socket, "gethostbyname", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(resource, "getrusage", lambda who: SimpleNamespace(ru_maxrss=65536.0))
    monkeypatch.setattr(stickmic, "_os_port_in_use", lambda port: False)
    monkeypatch.setattr(stickmic, "_PORTS", set())


@pytest.fixture
def settings():
    return doctor.load_settings()


@pytest.fixture
def profile(tmp_path):
    original = load_profile()
    return replace(
        original,
        classifier=replace(original.classifier, name="heuristic", model_dir=str(tmp_path / "models"),
                           head_path=str(tmp_path / "models" / "missing.joblib")),
    )


@pytest.fixture
def healthy_probes(monkeypatch):
    """Every external observation is supplied here, including platform/version."""
    calls = []
    monkeypatch.setattr(doctor.sys, "version_info", (3, 11, 11, "final", 0))
    monkeypatch.setattr(doctor.sys, "platform", "darwin")
    monkeypatch.setattr(doctor, "probe_audio_devices", lambda: [{
        "index": 4, "name": "USB Contact Mic", "max_input_channels": 1,
        "max_output_channels": 0, "is_default_input": True,
    }])
    monkeypatch.setattr(doctor, "probe_serial_ports", lambda profile, settings: {
        "ports": ["/dev/not-a-stick-name", "/dev/not-a-knocker-name", "/dev/not-a-rover-name"],
        "stick": ["/dev/not-a-stick-name"], "knocker": ["/dev/not-a-knocker-name"],
        "rover": ["/dev/not-a-rover-name"], "errors": [],
    })

    def stream(profile, device, settings):
        calls.append(("audio", device))
        return {"samples": 32000, "rms": 0.1, "peak": 0.2, "duration_s": 2.0, "errors": []}

    def speaker(device, play, profile, settings):
        calls.append(("speaker", device, play))
        return {"available": True, "devices": [{"index": 7, "name": "USB speaker"}],
                "played": play, "detail": "USB speaker output"}

    monkeypatch.setattr(doctor, "probe_audio", stream)
    monkeypatch.setattr(doctor, "probe_speaker", speaker)
    monkeypatch.setattr(doctor, "probe_disk", lambda path: 32 * 1024**3)
    monkeypatch.setattr(doctor, "probe_pi", lambda settings: {
        "available": False, "temp_c": None, "throttled": None, "detail": "Not a Raspberry Pi",
    })
    monkeypatch.setattr(doctor, "probe_network", lambda: {
        "hostname": "heartbeat-pi", "addresses": ["192.0.2.14"],
    })
    return calls


def _check(checks, name):
    matches = [check for check in checks if name.casefold() in check.name.casefold()]
    assert len(matches) == 1, [(check.name, check.detail) for check in checks]
    return matches[0]


class FakeAudio:
    def __init__(self, blocks=(), *, failure=None):
        self.blocks = blocks
        self.failure = failure
        self.closed = threading.Event()

    def __iter__(self):
        yield from self.blocks
        if self.failure is not None:
            raise self.failure

    def close(self):
        self.closed.set()


def _blocks(samples=32000, amplitude=0.1):
    return [AudioBlock(start / 16000, np.full(min(256, samples - start), amplitude, dtype=np.float32))
            for start in range(0, samples, 256)]


def test_doctor_reports_healthy_mocked_devices_and_both_dashboard_urls(profile, settings, healthy_probes):
    checks = doctor.run_doctor(profile, device="stick:/dev/chosen", speaker_device=7,
                               settings=settings)
    text = doctor.format_doctor(checks)
    assert checks and all(check.ok or check.skipped for check in checks)
    assert "✅" in text and "USB" in text
    for fragment in ("stick", "knocker", "rover", "heuristic", "192.0.2.14", "heartbeat-pi.local"):
        assert fragment in text
    assert f"http://192.0.2.14:{profile.server['port']}" in text
    assert f"http://heartbeat-pi.local:{profile.server['port']}" in text
    assert ("audio", "stick:/dev/chosen") in healthy_probes
    assert ("speaker", 7, False) in healthy_probes


def test_doctor_formats_failure_hint_and_skipped_check():
    checks = [doctor.Check("Audio", False, "No samples", "Check the USB cable"),
              doctor.Check("Pi", True, "Not applicable", "", skipped=True)]
    text = doctor.format_doctor(checks)
    assert "❌" in text and "Check the USB cable" in text and "No samples" in text
    assert "Pi" in text and "Not applicable" in text


def test_audio_probe_consumes_two_seconds_on_sample_clock_and_closes(monkeypatch, profile, settings):
    source, calls = FakeAudio(_blocks(32256)), []

    def open_fake(device, cfg, bus):
        calls.append(device)
        return source

    monkeypatch.setattr(doctor, "open_input", open_fake)
    result = doctor.probe_audio(profile, "stick:/dev/chosen", settings)
    assert calls == ["stick:/dev/chosen"]
    assert result["samples"] == 32000
    assert result["duration_s"] == pytest.approx(2.0)
    assert result["rms"] == pytest.approx(0.1)
    assert result["peak"] == pytest.approx(0.1)
    assert result["errors"] == [] and source.closed.is_set()


@pytest.mark.parametrize("samples, amplitude", [(32000, 0.0), (0, 0.1), (1024, 0.1)])
def test_doctor_rejects_silent_missing_or_truncated_audio(
    monkeypatch, profile, settings, healthy_probes, samples, amplitude,
):
    source = FakeAudio(_blocks(samples, amplitude))
    monkeypatch.setattr(doctor, "open_input", lambda *args: source)
    monkeypatch.setattr(doctor, "probe_audio", _REAL_PROBE_AUDIO)
    checks = doctor.run_doctor(profile, settings=settings)
    assert not _check(checks, "stream").ok
    assert source.closed.is_set()


def test_audio_probe_catches_open_failure_and_read_failure(monkeypatch, profile, settings):
    def missing(*args):
        raise OSError("USB unplugged")

    monkeypatch.setattr(doctor, "open_input", missing)
    with pytest.raises(OSError, match="USB unplugged"):
        doctor.probe_audio(profile, "stick:/dev/chosen", settings)
    source = FakeAudio(_blocks(256), failure=OSError("read failed"))
    monkeypatch.setattr(doctor, "open_input", lambda *args: source)
    result = doctor.probe_audio(profile, "stick:/dev/chosen", settings)
    assert "read failed" in " ".join(result["errors"])
    assert source.closed.is_set()


def test_audio_probe_stall_stops_and_closes_source(monkeypatch, profile, settings):
    class StalledAudio(FakeAudio):
        def __iter__(self):
            self.closed.wait(1.0)
            return
            yield

    settings = deepcopy(settings)
    settings["doctor"].update(audio_stall_timeout_s=0.01, audio_timeout_s=0.03,
                              audio_close_timeout_s=0.1)
    source = StalledAudio()
    monkeypatch.setattr(doctor, "open_input", lambda *args: source)
    result = doctor.probe_audio(profile, "stick:/dev/chosen", settings)
    assert result["samples"] == 0 and result["errors"]
    assert source.closed.is_set()


_REAL_PROBE_AUDIO = doctor.probe_audio


@pytest.mark.parametrize("block", [
    AudioBlock(0, np.ones(256, dtype=np.float64)),
    AudioBlock(0, np.ones((256, 1), dtype=np.float32)),
    AudioBlock(0, np.full(256, np.nan, dtype=np.float32)),
    AudioBlock(0, np.full(256, 1.1, dtype=np.float32)),
    AudioBlock(1, np.ones(256, dtype=np.float32)),
])
def test_audio_probe_rejects_blocks_outside_sample_contract(monkeypatch, profile, settings, block):
    source = FakeAudio([block])
    monkeypatch.setattr(doctor, "open_input", lambda *args: source)
    result = doctor.probe_audio(profile, "stick:/dev/chosen", settings)
    assert result["errors"] and source.closed.is_set()


@pytest.mark.parametrize("probe, message", [
    ("probe_audio_devices", "audio inventory unavailable"),
    ("probe_serial_ports", "serial inventory unavailable"),
    ("probe_audio", "stream unavailable"),
    ("probe_speaker", "speaker unavailable"),
    ("probe_classifier", "classifier unavailable"),
    ("probe_disk", "filesystem unavailable"),
    ("probe_pi", "temperature unavailable"),
    ("probe_network", "network inventory unavailable"),
])
def test_doctor_keeps_other_checks_after_probe_failure(
    monkeypatch, profile, settings, healthy_probes, probe, message,
):
    def failed(*args, **kwargs):
        raise OSError(message)

    monkeypatch.setattr(doctor, probe, failed)
    checks = doctor.run_doctor(profile, device=4, play=True, settings=settings)
    assert message in doctor.format_doctor(checks)
    assert not all(check.ok or check.skipped for check in checks)
    if probe != "probe_classifier":
        assert _check(checks, "classifier").ok
    assert any(check.name == "dashboard" for check in checks)


def test_doctor_rejects_wrong_python_and_low_disk_with_fix_hints(
    monkeypatch, profile, settings, healthy_probes,
):
    monkeypatch.setattr(doctor.sys, "version_info", (3, 12, 0, "final", 0))
    monkeypatch.setattr(doctor, "probe_disk", lambda path: 1)
    checks = doctor.run_doctor(profile, settings=settings)
    assert not _check(checks, "python").ok and _check(checks, "python").hint
    assert not _check(checks, "disk").ok and _check(checks, "disk").hint
    assert "❌" in doctor.format_doctor(checks)


def test_doctor_requires_selected_stick_and_knocker_but_rover_is_optional(
    monkeypatch, profile, settings, healthy_probes,
):
    monkeypatch.setattr(doctor, "probe_serial_ports", lambda profile, settings: {
        "ports": [], "stick": [], "knocker": [], "rover": [], "errors": [],
    })
    checks = doctor.run_doctor(profile, device="stick", settings=settings)
    assert not _check(checks, "stick").ok
    assert not _check(checks, "knocker").ok
    assert _check(checks, "rover").skipped


def test_doctor_reports_classifier_that_really_ran_and_optional_models(
    profile, settings, healthy_probes,
):
    profile = replace(profile, classifier=replace(profile.classifier, name="auto"))
    checks = doctor.run_doctor(profile, settings=settings)
    assert _check(checks, "classifier").ok
    assert "heuristic" in _check(checks, "classifier").detail
    assert _check(checks, "models").skipped


def test_classifier_probe_exercises_inference_and_reports_runtime_fallback(monkeypatch, profile):
    from heartbeat.classify import PassThroughClassifier

    class RuntimeFallback(PassThroughClassifier):
        name = "head"

        def classify(self, onset):
            result = super().classify(onset)
            result.classifier = "heuristic"
            return result

    monkeypatch.setattr(doctor, "make_classifier", lambda *args, **kwargs: RuntimeFallback())
    result = doctor.probe_classifier(profile)
    assert result["active"] == "heuristic"


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now


class FakeSerial:
    def __init__(self, clock, reply):
        self.clock = clock
        self.reply = reply
        self.pending = bytearray(b"READY arbitrary v1\n")
        self.writes = []
        self.closed = False

    def write(self, payload):
        self.writes.append(payload)
        self.pending.extend(self.reply)
        return len(payload)

    def read(self, size):
        self.clock.now += 0.001
        result = bytes(self.pending[:size])
        del self.pending[:size]
        return result

    def close(self):
        self.closed = True


@pytest.mark.parametrize("reply, expected", [
    (b"PONG stickmic v1\n", "PONG stickmic v1"),
    (b"PONG knocker v1\n", "PONG knocker v1"),
    (b"PONG rover v1\n", "PONG rover v1"),
    (b"PONG knocker v10\n", None),
    (b"PONG stickmic v1 extra\n", None),
    (b"READY rover v1\n", None),
    (b"", None),
])
def test_serial_probe_accepts_exact_identity_only_and_never_starts_or_moves_device(
    monkeypatch, settings, reply, expected,
):
    clock = FakeClock()
    connection = FakeSerial(clock, reply)
    monkeypatch.setattr(doctor.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(doctor, "new_serial_connection", lambda *args, **kwargs: connection)
    result = doctor._probe_identity("/dev/arbitrary-name", 115200, settings)
    assert result == expected
    assert connection.writes == [b"PING\n"]
    assert connection.closed and not stickmic.port_in_use("/dev/arbitrary-name")


def test_serial_identity_probe_releases_port_if_open_fails(monkeypatch, settings):
    def unavailable(*args, **kwargs):
        raise OSError("unplugged")

    monkeypatch.setattr(doctor, "new_serial_connection", unavailable)
    with pytest.raises(OSError, match="unplugged"):
        doctor._probe_identity("/dev/arbitrary-name", 115200, settings)
    assert not stickmic.port_in_use("/dev/arbitrary-name")


@pytest.mark.parametrize("stick_baud", [500000, 750000, 115200])
def test_serial_inventory_uses_profile_stick_baud_and_control_protocol_baud(
    monkeypatch, profile, settings, stick_baud,
):
    profile = replace(profile, audio=replace(profile.audio, stick_baud=stick_baud))
    replies = {"/dev/a-stick": "PONG stickmic v1", "/dev/b-knocker": "PONG knocker v1",
               "/dev/c-rover": "PONG rover v1"}
    calls = []

    def probe(port, baud, selected_settings, *, stick=False):
        assert selected_settings is settings
        calls.append((port, baud, stick))
        if stick:
            assert baud == stick_baud
            return replies[port] if port == "/dev/a-stick" else None
        assert baud == 115200
        return replies[port]

    monkeypatch.setattr(doctor.list_ports, "comports", lambda: [
        SimpleNamespace(device=port) for port in replies
    ])
    monkeypatch.setattr(doctor, "_probe_identity", probe)
    result = doctor.probe_serial_ports(profile, settings)
    assert result["stick"] == ["/dev/a-stick"]
    assert result["knocker"] == ["/dev/b-knocker"]
    assert result["rover"] == ["/dev/c-rover"]
    assert calls == [("/dev/a-stick", stick_baud, True),
                     ("/dev/b-knocker", stick_baud, True), ("/dev/b-knocker", 115200, False),
                     ("/dev/c-rover", stick_baud, True), ("/dev/c-rover", 115200, False)]


@pytest.mark.parametrize("failure", [False, True])
def test_stick_identity_probe_reuses_runtime_recovery_and_startup_timeout(
    monkeypatch, profile, settings, failure,
):
    clock = FakeClock()
    clock.now = 0.2
    connection = FakeSerial(clock, b"")
    opened, identified = [], []

    def open_connection(port, baud, timeout, **kwargs):
        opened.append((port, baud, timeout, kwargs["write_timeout"]))
        return connection

    def identify(selected, deadline, monotonic):
        assert selected is connection
        assert deadline == pytest.approx(clock.now + stickmic.READY_TIMEOUT_S)
        assert deadline - monotonic() > settings["doctor"]["serial_timeout_s"]
        assert stickmic.port_in_use("/dev/stick")
        identified.append(True)
        if failure:
            raise TimeoutError("fake startup did not finish")

    monkeypatch.setattr(doctor.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(doctor, "new_serial_connection", open_connection)
    monkeypatch.setattr(doctor, "_identify_stick", identify)
    if failure:
        with pytest.raises(TimeoutError, match="startup"):
            doctor._probe_identity("/dev/stick", profile.audio.stick_baud, settings, stick=True)
    else:
        assert doctor._probe_identity("/dev/stick", profile.audio.stick_baud, settings,
                                      stick=True) == "PONG stickmic v1"
    assert identified == [True]
    assert opened == [("/dev/stick", profile.audio.stick_baud,
                       settings["doctor"]["serial_read_poll_s"],
                       settings["doctor"]["serial_write_timeout_s"])]
    assert connection.writes == []  # The reused helper owns STOP/drain/PING, never START.
    assert connection.closed and not stickmic.port_in_use("/dev/stick")


def test_control_identity_probe_keeps_the_open_serial_timeout_fixed(monkeypatch, settings):
    clock = FakeClock()

    class FixedTimeoutSerial(FakeSerial):
        def __setattr__(self, name, value):
            if name == "timeout":
                pytest.fail("read polling must not reconfigure an open serial port")
            super().__setattr__(name, value)

    connection = FixedTimeoutSerial(clock, b"PONG knocker v1\n")
    monkeypatch.setattr(doctor.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(doctor, "new_serial_connection", lambda *args, **kwargs: connection)
    assert doctor._probe_identity("/dev/control", 115200, settings) == "PONG knocker v1"
    assert connection.writes == [b"PING\n"]
    assert connection.closed and not stickmic.port_in_use("/dev/control")


def test_serial_inventory_uses_replies_instead_of_port_names(monkeypatch, profile, settings):
    replies = {"/dev/knocker-looking-name": "PONG stickmic v1",
               "/dev/stick-looking-name": "PONG rover v1",
               "/dev/rover-looking-name": "PONG knocker v1",
               "/dev/PONG-stickmic-v1": None}
    monkeypatch.setattr(doctor.list_ports, "comports", lambda: [
        SimpleNamespace(device=port) for port in replies
    ])
    monkeypatch.setattr(doctor, "_probe_identity", lambda port, baud, settings, **kwargs: replies[port])
    result = doctor.probe_serial_ports(profile, settings)
    assert result["stick"] == ["/dev/knocker-looking-name"]
    assert result["knocker"] == ["/dev/rover-looking-name"]
    assert result["rover"] == ["/dev/stick-looking-name"]


@pytest.mark.parametrize("play", [False, True])
def test_speaker_only_plays_reassurance_when_requested(monkeypatch, profile, settings, tmp_path, play):
    import soundfile as sf

    voice_dir = tmp_path / "voice"
    voice_dir.mkdir()
    sf.write(voice_dir / "reassure.wav", np.full(320, 0.1, dtype=np.float32), 16000)
    profile = replace(profile, prompter={**profile.prompter, "voice_dir": str(voice_dir)})
    calls = []
    backend = SimpleNamespace(
        check_output_settings=lambda **kwargs: calls.append(("check", kwargs)),
        play=lambda *args, **kwargs: calls.append(("play", args, kwargs)),
        stop=lambda: calls.append(("stop",)),
    )
    monkeypatch.setattr(doctor, "_sounddevice", lambda: backend)
    monkeypatch.setattr(doctor, "probe_audio_devices", lambda: [
        {"index": 7, "name": "USB speaker", "max_input_channels": 0, "max_output_channels": 2},
    ])
    result = doctor.probe_speaker(7, play, profile, settings)
    assert result["available"]
    assert result["played"] is play
    plays = [call for call in calls if call[0] == "play"]
    assert len(plays) == int(play)
    if play:
        assert plays[0][2]["device"] == 7
        assert plays[0][2]["samplerate"] == 16000
        assert plays[0][2]["blocking"] is True


def test_speaker_without_output_never_attempts_playback(monkeypatch, profile, settings):
    monkeypatch.setattr(doctor, "probe_audio_devices", lambda: [])
    result = doctor.probe_speaker(None, False, profile, settings)
    assert not result["available"] and not result["played"]


def test_pi_probe_skips_commands_on_mac_even_if_vcgencmd_is_installed(monkeypatch, settings):
    monkeypatch.setattr(doctor.sys, "platform", "darwin")
    monkeypatch.setattr(doctor.shutil, "which", lambda command: "/fake/vcgencmd")
    result = doctor.probe_pi(settings)
    assert not result["available"]


def test_pi_probe_reads_temperature_and_throttle_status_with_bounded_commands(monkeypatch, settings):
    monkeypatch.setattr(doctor.sys, "platform", "linux")
    monkeypatch.setattr(doctor.shutil, "which", lambda command: "/fake/vcgencmd")
    calls = []

    def command(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(stdout={"measure_temp": "temp=52.5'C\n", "get_throttled": "throttled=0x0\n"}[argv[-1]])

    monkeypatch.setattr(subprocess, "run", command)
    result = doctor.probe_pi(settings)
    assert result["available"] and result["temp_c"] == 52.5 and result["throttled"] == 0
    assert {argv[-1] for argv, _ in calls} == {"measure_temp", "get_throttled"}
    assert all(kwargs["timeout"] == settings["doctor"]["command_timeout_s"] for _, kwargs in calls)


@pytest.mark.parametrize("temperature, throttled, expected", [
    (50.0, 0x0, True), (100.0, 0x0, False), (50.0, 0x1, False), (50.0, 0x10000, True),
])
def test_doctor_flags_heat_and_current_throttling(
    monkeypatch, profile, settings, healthy_probes, temperature, throttled, expected,
):
    monkeypatch.setattr(doctor, "probe_pi", lambda settings: {
        "available": True, "temp_c": temperature, "throttled": throttled,
        "detail": f"temp={temperature}; throttled={throttled:#x}",
    })
    checks = doctor.run_doctor(profile, settings=settings)
    assert _check(checks, "pi").ok is expected
    assert not _check(checks, "pi").skipped


def test_disk_probe_returns_mocked_free_bytes(monkeypatch, tmp_path):
    monkeypatch.setattr(doctor.shutil, "disk_usage", lambda path: SimpleNamespace(free=1234567))
    assert doctor.probe_disk(tmp_path / "future-runs") == 1234567


def test_network_probe_only_formats_mocked_local_addresses(monkeypatch):
    monkeypatch.setattr(doctor.sys, "platform", "darwin")
    monkeypatch.setattr(socket, "gethostname", lambda: "heartbeat-pi.local")
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args: [
        (socket.AF_INET, 0, 0, "", (address, 0))
        for address in ("192.0.2.14", "127.0.0.1", "192.0.2.14")
    ])
    assert doctor.probe_network() == {"hostname": "heartbeat-pi", "addresses": ["192.0.2.14"]}


def test_network_probe_uses_pi_interface_when_hostname_does_not_resolve(monkeypatch):
    import fcntl

    class LocalSocket:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def fileno(self):
            return 99

    def unresolved(*args):
        raise socket.gaierror("hostname missing from local resolver")

    def address(fd, request, interface):
        assert fd == 99 and request == 0x8915 and interface.startswith(b"wlan0")
        return bytes(20) + socket.inet_aton("192.0.2.14") + bytes(232)

    monkeypatch.setattr(doctor.sys, "platform", "linux")
    monkeypatch.setattr(socket, "gethostname", lambda: "heartbeat-pi")
    monkeypatch.setattr(socket, "getaddrinfo", unresolved)
    monkeypatch.setattr(socket, "socket", lambda *args: LocalSocket())
    monkeypatch.setattr(socket, "if_nameindex", lambda: [(1, "wlan0")])
    monkeypatch.setattr(fcntl, "ioctl", address)
    assert doctor.probe_network() == {"hostname": "heartbeat-pi", "addresses": ["192.0.2.14"]}


def test_short_benchmark_runs_actual_cpu_pipeline_without_hardware(profile, settings):
    result = doctor.run_bench(profile, seconds=5.0, classifier="heuristic", seed=17, settings=settings)
    assert result["audio_seconds"] == pytest.approx(5.0)
    assert result["classifier"] == "heuristic"
    assert result["detections"] > 0
    assert math.isfinite(result["real_time_factor"]) and result["real_time_factor"] > 0
    assert result["real_time_factor"] == pytest.approx(result["pipeline_wall_s"] / 5.0)
    assert math.isfinite(result["peak_rss_mib"]) and result["peak_rss_mib"] > 0
    for stage in ("onset", "classifier", "dialog"):
        latency = result["stages"][stage]
        assert latency["calls"] > 0
        assert all(math.isfinite(latency[key]) and latency[key] >= 0 for key in ("mean_ms", "p95_ms"))
    json.dumps(result, allow_nan=False)
    table = doctor.format_bench(result)
    assert "|" in table and "heuristic" in table
    assert "p95" in table.lower() and "rss" in table.lower()


def test_benchmark_reports_auto_fallback_that_actually_ran(profile, settings):
    result = doctor.run_bench(profile, seconds=3, classifier="auto", seed=2, settings=settings)
    assert result["requested_classifier"] == "auto"
    assert result["classifier"] == "heuristic"
    assert result["classifiers"] == ["heuristic"]


@pytest.mark.parametrize("seconds", [0, -1, float("nan"), float("inf"), "not-seconds"])
def test_benchmark_rejects_invalid_duration_before_processing(profile, settings, seconds):
    with pytest.raises((ValueError, TypeError)):
        doctor.run_bench(profile, seconds=seconds, settings=settings)


def test_settings_default_scene_and_audio_duration_live_in_yaml(settings):
    assert settings["doctor"]["audio_seconds"] == 2.0
    assert settings["bench"]["seconds"] == 120.0


@pytest.mark.parametrize("data", [[], {"doctor": []}, {"doctor": {"audio_seconds": 0}}])
def test_invalid_settings_are_rejected(tmp_path, data):
    path = tmp_path / "diagnostics.yaml"
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ValueError):
        doctor.load_settings(path)


def test_doctor_cli_dispatch_uses_selected_input_speaker_and_play(
    monkeypatch, profile, settings, healthy_probes, capsys,
):
    monkeypatch.setattr(cli, "load_profile", lambda selected: profile)
    assert cli.main(["doctor", "--device", "stick:/dev/chosen", "--speaker-device", "7", "--play"]) == 0
    assert ("audio", "stick:/dev/chosen") in healthy_probes
    assert ("speaker", 7, True) in healthy_probes
    assert "heartbeat-pi.local" in capsys.readouterr().out


def test_bench_cli_dispatch_never_opens_devices(monkeypatch, capsys):
    result = doctor.run_bench(load_profile(), seconds=3, classifier="heuristic", seed=3)
    calls = []

    def bench(profile, **kwargs):
        calls.append(kwargs)
        return result

    monkeypatch.setattr(doctor, "run_bench", bench)
    assert cli.main(["bench", "--seconds", "3", "--classifier", "heuristic", "--seed", "3"]) == 0
    assert calls and calls[0]["seconds"] == 3
    assert calls[0]["classifier"] == "heuristic" and calls[0]["seed"] == 3
    assert "heuristic" in capsys.readouterr().out


@pytest.mark.parametrize("voice", ["auto", "none"])
def test_voice_setting_accepts_supported_modes(voice):
    from heartbeat.prompter import PrompterConfig

    assert PrompterConfig.from_dict({"voice": voice}).voice == voice


@pytest.mark.parametrize("voice", ["speaker", "always", None, True])
def test_voice_setting_rejects_unsupported_modes(voice):
    from heartbeat.prompter import PrompterConfig

    with pytest.raises(ValueError, match="voice"):
        PrompterConfig.from_dict({"voice": voice})


class FakeKnockerSerial(FakeSerial):
    def __init__(self, clock):
        super().__init__(clock, b"")
        self.pending = bytearray(b"READY knocker v1 rest=60 strike=95 hold=70\n")
        self.is_open = True

    def write(self, payload):
        self.writes.append(payload)
        if payload == b"PING\n":
            self.pending.extend(b"PONG knocker v1\n")
        elif payload.startswith(b"K "):
            self.pending.extend(b"OK K " + payload.split()[1] + b"\n")
        return len(payload)

    def close(self):
        super().close()
        self.is_open = False


def _voice_prompter(profile, *, voice, backend, serial_enabled=True, **options):
    from heartbeat.events import EventBus
    from heartbeat.prompter import make_prompter

    bus, events = EventBus(), []
    bus.subscribe(events.append)
    clock = FakeClock()
    connection = FakeKnockerSerial(clock)
    sleeps = []
    result = make_prompter(
        "auto", bus, cfg={**profile.prompter, "voice": voice}, clock=lambda: 12.5,
        audio=backend, port_lister=lambda: ["/dev/fake-knocker"] if serial_enabled else [],
        serial_factory=lambda *args, **kwargs: connection, sleep=sleeps.append,
        monotonic=clock.monotonic, **options,
    )
    return result, connection, events, sleeps


def _voice_warnings(events):
    warnings = [event for event in events if event.data.get("code") == "voice_disabled"]
    assert len(warnings) == 1
    assert warnings[0].type == "error" and warnings[0].data["severity"] == "warning"
    assert warnings[0].t == 12.5


def test_voice_none_with_serial_preserves_knocks_without_querying_output(profile):
    from heartbeat.dialog import PromptKnocks, PromptVoice
    from heartbeat.prompter import CompositePrompter

    def forbidden(*args, **kwargs):
        pytest.fail("voice none with a serial knocker must not query or play speaker audio")

    backend = SimpleNamespace(check_output_settings=forbidden, play=forbidden, stop=lambda: None)
    prompter, connection, events, sleeps = _voice_prompter(profile, voice="none", backend=backend)
    try:
        assert isinstance(prompter, CompositePrompter)
        prompter.run(PromptVoice("reassure"))
        prompter.run(PromptVoice("q_people"))
        prompter.run(PromptKnocks(2, 0.5))
        assert connection.writes == [b"PING\n", b"K 2 500\n"]
        assert sleeps == []
        _voice_warnings(events)
    finally:
        prompter.close()
    assert connection.closed


def test_voice_auto_without_output_skips_voices_and_keeps_manual_knocks(profile):
    from heartbeat.dialog import PromptKnocks, PromptVoice
    from heartbeat.prompter import ManualPrompter

    def no_output(**kwargs):
        raise OSError("No output device")

    backend = SimpleNamespace(check_output_settings=no_output,
                              play=lambda *args, **kwargs: pytest.fail("no output must never play"),
                              stop=lambda: None)
    prompter, _, events, sleeps = _voice_prompter(profile, voice="auto", backend=backend,
                                                 serial_enabled=False)
    try:
        assert isinstance(prompter, ManualPrompter)
        prompter.run(PromptVoice("reassure"))
        prompter.run(PromptVoice("q_people"))
        assert sleeps == []
        prompter.run(PromptKnocks(2, 0.5))
        assert len(sleeps) == 1 and sleeps[0] > 0
        operators = [event.data for event in events if event.type == "operator"]
        assert len(operators) == 1 and operators[0]["action"] == "knocks" and operators[0]["n"] == 2
        _voice_warnings(events)
    finally:
        prompter.close()


@pytest.mark.parametrize("playback_fails", [False, True])
def test_voice_auto_uses_available_output_and_preserves_serial_after_playback_failure(
    profile, tmp_path, playback_fails,
):
    import soundfile as sf
    from heartbeat.dialog import PromptKnocks, PromptVoice

    sf.write(tmp_path / "reassure.wav", np.full(320, 0.1, dtype=np.float32), 16000)
    profile = replace(profile, prompter={**profile.prompter, "voice_dir": str(tmp_path)})
    plays, queries = [], []

    def play(*args, **kwargs):
        plays.append(kwargs)
        if playback_fails:
            raise OSError("speaker disconnected")

    backend = SimpleNamespace(check_output_settings=lambda **kwargs: queries.append(kwargs),
                              play=play, stop=lambda: None)
    prompter, connection, events, sleeps = _voice_prompter(profile, voice="auto", backend=backend)
    try:
        prompter.run(PromptVoice("reassure"))
        prompter.run(PromptVoice("reassure"))
        prompter.run(PromptKnocks(3, 0.5))
        assert queries and len(plays) == (1 if playback_fails else 2)
        assert all(call["blocking"] is True and call["samplerate"] == 16000 for call in plays)
        assert connection.writes == [b"PING\n", b"K 3 500\n"]
        assert sleeps == [] and not any(event.type == "operator" for event in events)
        if playback_fails:
            _voice_warnings(events)
        else:
            assert not any(event.type == "error" for event in events)
    finally:
        prompter.close()
