"""Select standard audio or the serial M5StickC microphone."""

from __future__ import annotations

from .audio import MicSource
from .stickmic import StickMicSource, find_stick_ports


def open_input(spec, cfg, bus=None, *, debug=False):
    """Select an input; automatic stick discovery probes idle serial ports.

    The returned source opens its audio stream on iteration. ``cfg`` may be an
    entire profile or just its audio section.
    """
    audio = getattr(cfg, "audio", cfg)
    if spec == "stick":
        ports = find_stick_ports(bus=bus, **({"debug": True} if debug else {}))
        if not ports:
            raise ValueError(
                "No M5StickC microphone found. Connect a stick running stickmic v1 "
                "or use --device stick:/dev/cu.<port>."
            )
        port = ports[0]
    elif isinstance(spec, str) and spec.startswith("stick:"):
        port = spec[len("stick:") :].strip()
        if not port:
            raise ValueError("A serial port must follow 'stick:'")
    else:
        return MicSource(device=spec, block=audio.block, cfg=audio, bus=bus)
    return StickMicSource(
        port, baud=audio.stick_baud, gain_db=audio.stick_gain_db, bus=bus,
        **({"debug": True} if debug else {}),
    )
