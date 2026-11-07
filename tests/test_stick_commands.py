"""Text-mode stick configuration and operator controls, without hardware."""

from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf

from heartbeat import cli, stickmic
from heartbeat.events import Event


class TextStick:
    def __init__(self, *, metadata=b"", reply=None):
        self.now = 0.0
        self.pending = bytearray(b"READY stickmic v1 " + metadata + b"\n")
        self.reply = reply
        self.writes = []
        self.opens = self.closes = 0
        self.is_open = False
        self.dtr = self.rts = True

    def open(self):
        assert self.dtr is False and self.rts is False and self.exclusive is True
        self.opens += 1
        self.is_open = True

    def close(self):
        self.closes += 1
        self.is_open = False

    def read(self, size):
        self.now += 0.001 if self.pending else self.timeout
        result = bytes(self.pending[:size])
        del self.pending[:size]
        return result

    def write(self, payload):
        self.writes.append(payload)
        if payload == b"PING\n":
            self.pending.extend(b"PONG stickmic v1\n")
        elif payload.startswith(b"MAG "):
            self.pending.extend(self.reply if self.reply is not None else b"OK " + payload)
        return len(payload)

    def flush(self):
        pass

    def reset_input_buffer(self):
        self.pending.clear()


@pytest.fixture(autouse=True)
def no_hardware(monkeypatch):
    import serial
    import serial.tools.list_ports

    def forbidden(*args, **kwargs):
        pytest.fail("these tests must not access serial hardware")

    monkeypatch.setattr(serial, "Serial", forbidden)
    monkeypatch.setattr(serial.tools.list_ports, "comports", forbidden)
    monkeypatch.setattr(stickmic, "_os_port_in_use", lambda port: False)
    monkeypatch.setattr(stickmic, "_PORTS", set())


def configure(port, **kwargs):
    return stickmic.configure_stick("/dev/fake-stick", serial_factory=lambda: port,
                                   monotonic=lambda: port.now, **kwargs)


@pytest.mark.parametrize("mag", [1, 16, 255])
def test_mag_uses_text_handshake_checks_ack_and_releases_port(mag):
    port = TextStick()
    assert configure(port, mag=mag) == [f"OK MAG {mag}"]
    assert port.writes == [b"\nSTOP\n", b"PING\n", f"MAG {mag}\n".encode()]
    assert port.opens == port.closes == 1
    assert not stickmic._PORTS and not port.is_open


@pytest.mark.parametrize("metadata, expected", [
    (b"", ["PONG stickmic v1"]),
    (b"MAG=8", ["PONG stickmic v1", "MAG 8"]),
    (b"mag=16", ["PONG stickmic v1", "MAG 16"]),
])
def test_info_only_prints_available_firmware_metadata(metadata, expected):
    port = TextStick(metadata=metadata)
    assert configure(port) == expected
    assert port.writes == [b"\nSTOP\n", b"PING\n"]
    assert port.closes == 1 and not stickmic._PORTS


@pytest.mark.parametrize("reply, error", [(b"ERR mag_save\n", OSError),
                                          (b"OK MAG 7\n", OSError), (b"", TimeoutError)])
def test_mag_failure_is_bounded_and_releases_port(reply, error):
    port = TextStick(reply=reply)
    with pytest.raises(error):
        configure(port, mag=8)
    assert port.now < stickmic.READY_TIMEOUT_S + 1
    assert port.closes == 1 and not stickmic._PORTS


@pytest.mark.parametrize("mag", [0, 256, -1, 1.5, True])
def test_invalid_mag_never_claims_or_opens(mag):
    port = TextStick()
    with pytest.raises(ValueError, match="1 to 255"):
        configure(port, mag=mag)
    assert port.opens == port.closes == 0 and not stickmic._PORTS


def test_busy_mag_never_opens_or_releases_another_owner():
    port = TextStick()
    assert stickmic.claim_port("/dev/fake-stick")
    with pytest.raises(OSError, match="already in use"):
        configure(port, mag=8)
    assert port.opens == port.closes == 0
    assert stickmic.port_in_use("/dev/fake-stick")


def test_mag_serial_open_failure_releases_claim():
    port = TextStick()
    def failed_open():
        raise OSError("disconnected")
    port.open = failed_open
    with pytest.raises(OSError, match="disconnected"):
        configure(port, mag=8)
    assert not stickmic._PORTS


@pytest.mark.parametrize("explicit", [False, True])
def test_stick_mag_cli_parser_and_serial_roundtrip(monkeypatch, capsys, explicit):
    port = TextStick()
    calls = []
    real = stickmic.configure_stick
    def selected(path, **kwargs):
        calls.append(path)
        return real(path, serial_factory=lambda: port, monotonic=lambda: port.now, **kwargs)
    monkeypatch.setattr(cli, "configure_stick", selected)
    monkeypatch.setattr(cli, "find_stick_ports", lambda **kwargs: ["/dev/fake-stick"])
    args = ["stick"] + (["--port", "/dev/fake-stick"] if explicit else []) + ["mag", "8"]
    assert cli.main(args) == 0
    assert capsys.readouterr().out.strip() == "OK MAG 8"
    assert calls == ["/dev/fake-stick"] and port.baudrate == 500000
    assert not stickmic._PORTS


@pytest.mark.parametrize("value", ["0", "256", "-1", "1.5", "nope"])
def test_cli_rejects_invalid_mag_before_hardware(value):
    with pytest.raises(SystemExit):
        cli.main(["stick", "mag", value])


@pytest.mark.parametrize("ports", [[], ["/dev/one", "/dev/two"]])
def test_mag_auto_requires_one_unambiguous_device(monkeypatch, capsys, ports):
    monkeypatch.setattr(cli, "find_stick_ports", lambda **kwargs: ports)
    assert cli.main(["stick", "mag", "8"]) == 1
    assert "error:" in capsys.readouterr().err


def test_info_cli_uses_readonly_text_query(monkeypatch, capsys):
    port = TextStick()
    monkeypatch.setattr(cli, "configure_stick", lambda path, **kwargs: configure(port, **kwargs))
    assert cli.main(["stick", "--port", "/dev/fake-stick", "info"]) == 0
    assert capsys.readouterr().out.strip() == "PONG stickmic v1"
    assert not any(command.startswith((b"MAG", b"START")) for command in port.writes)


def test_all_quiet_key_records_initials_and_q_still_quits(monkeypatch):
    calls, stopped = [], []
    session = SimpleNamespace(command=lambda name, **kwargs: calls.append((name, kwargs)))
    pipeline = SimpleNamespace(call_soon=lambda action: action(), stop=lambda: stopped.append(True))
    monkeypatch.setattr(cli.sys, "stdin", StringIO("w AB\nw\nc\nq\ni\n"))
    cli._operator_input(session, pipeline)
    assert calls == [("all_quiet", {"operator": "AB", "challenge": True}),
                     ("all_quiet", {"operator": "console", "challenge": True}), ("challenge", {})]
    assert stopped == [True]


def test_all_quiet_transcript_includes_operator(capsys):
    cli._DialogConsole()(Event(type="operator", t=12, data={"command": "all_quiet", "operator": "AB"}))
    assert "all quiet — AB" in capsys.readouterr().out


def test_committed_stop_and_listen_voice_is_playable_without_hardware():
    from heartbeat.dialog import PromptVoice
    from heartbeat.prompter import ManualPrompter, VOICE_TEXT

    path = Path(__file__).resolve().parents[1] / "assets/voice/stop_and_listen.wav"
    samples, rate = sf.read(path, dtype="float32")
    assert rate == 16000 and samples.ndim == 1 and samples.size > rate
    assert np.isfinite(samples).all() and np.max(np.abs(samples)) > 0
    assert sf.info(path).subtype == "PCM_16"
    output, waits = StringIO(), []
    prompter = ManualPrompter(stream=output, sleep=waits.append)
    prompter.run(PromptVoice("stop_and_listen"))
    assert VOICE_TEXT["stop_and_listen"] in output.getvalue()
    assert waits == [len(samples) / rate]
