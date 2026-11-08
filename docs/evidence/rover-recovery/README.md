# Preserved Auriga recovery sketch

`rover.ino.hex` is an exact copy of the compiled sketch preserved on the old Pi
at `/home/teamrover/heartbeat-rover-build/rover.ino.hex`. It is included at the
operator's request to move recovery to a new Pi using GitHub alone.

- Size: 73,535 bytes, Intel HEX (no bootloader image).
- SHA-256: `adaf180028278066cc287107827acfa2d05c5956c02cb00b747ecf83a4808575`.
- Firmware source: `firmware/rover/rover.ino` at `101c7ca`, unchanged at `1919e4b`.
- Source SHA-256: `26dae9f3223c89eaae0cc5c8693609b9109c1897d338633a2161e1767f9ab6d3`.
- Target: `arduino:avr:mega:cpu=atmega2560`, AVR core 1.8.8.
- Build used old Pi's Makeblock library under LifeScout and
  `build.extra_flags=-DLIFESCOUT_AURIGA` to avoid conflicting pin-map definitions.
- Recorded build usage: 26,140 bytes flash, 2,224 bytes static RAM.

This artifact compiled successfully but was **not successfully uploaded or
validated on the physical rover**. It expects `CAPS guarded=1 cruise=1`.
Never infer motor safety or working STOP from this file's existence.
Follow [the new Pi handoff](../../NEW-PI-HANDOFF.md) and
[PI-04](../../../prompts/PI-04-rover-recovery.md) before flashing.
The old full build and library remain on the old Pi; they are not copied into
Git. This HEX permits recovery without rebuilding those dependencies.
