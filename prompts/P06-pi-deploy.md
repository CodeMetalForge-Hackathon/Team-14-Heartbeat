# P06: run the sentinel standalone on the Raspberry Pi 5

Read `AGENTS.md` first. **Lane A, after P04 is committed.**

## Goal
The sentinel boots on a Pi 5 with no laptop. It needs only:
- the contact mic on a USB audio adapter
- the knocker on USB serial
- a speaker

A phone on the same Wi-Fi or hotspot opens the dashboard. We also need numbers for the deployability slide.

## Files you own
- `scripts/pi_setup.sh`, `scripts/heartbeat.service`
- `docs/PI.md`
- The `hb doctor` and `hb bench` commands: additions to `cli.py`, plus a new `heartbeat/doctor.py`
- `tests/test_doctor.py`

## Build
- **`scripts/pi_setup.sh`**
  - Runs on the Pi as the normal user and is idempotent.
  - Install with apt: `git`, `libportaudio2`, `libsndfile1`, `ffmpeg`, `alsa-utils`, `espeak-ng`, `avahi-daemon`.
  - Install uv if it's missing, then run `uv sync --extra ml`.
  - Add the user to the `dialout` and `audio` groups.
  - Run `tools/fetch_models.py` if the models are missing.
  - Print the next steps.
- **`hb doctor`**
  - Print a checklist of ✅ and ❌ with a fix hint for each. It checks:
    - Python version
    - audio input devices, with USB ones highlighted
    - that the chosen device opens and delivers non-silent audio for 2 s
    - serial ports, finding the knocker by `PING` → `PONG`
    - speaker output (plays `assets/voice/reassure.wav` if `--play`)
    - model files and the active classifier
    - free disk space
    - CPU temperature and throttling (`vcgencmd`, if present)
    - the dashboard URL as both IP and `hostname.local`
  - It must also work on macOS, skipping Pi-only checks.
- **`hb bench`**
  - Run the full pipeline on a 120 s synthetic scene faster than real time.
  - Print a markdown table:
    - real-time factor
    - per-stage latency (onset, classifier, dialog) as mean and p95
    - peak RSS memory
    - which classifier ran
  - These numbers go on our slide.
- **`--knocker auto` for `hb run`:** if P03 didn't do it, add it. Find the serial port that answers `PONG`; otherwise use the speaker; otherwise manual.
- **`scripts/heartbeat.service`**
  - A systemd unit that runs `hb run --dashboard --knocker auto --auto` at boot from the repo directory.
  - `Restart=on-failure`, logs to the journal.
  - Document how to enable it, disable it and follow its logs.
- **`docs/PI.md`**
  - SSH into the Pi; find its IP (`hostname -I`, or `<hostname>.local`).
  - Clone the repo and run `pi_setup.sh`.
  - Pick devices with `hb doctor`.
  - Run, then open the dashboard from a phone.
  - **Power:** use the official 27 W USB-C supply or a USB-C PD power bank. A 5V/3A bank works, but the Pi warns and limits its USB current.
  - Pull `runs/` back to the laptop with `rsync` for the after-action review.
  - Troubleshooting:
    - the adapter doesn't support 16 kHz (the resampling path handles it)
    - permission errors on the serial port (the `dialout` group; log out and back in)
    - no sound out (`aplay -l`, choose `--speaker-device`)

## Tests
- `doctor` runs with every hardware probe mocked.
- `bench` on a short scene returns sane numbers.

## Done when
- Tests pass on macOS.
- `uv run hb doctor` and `uv run hb bench` work on the Mac.

Commit `P06: Pi deploy, doctor, bench`. Report the exact commands the human runs on the Pi. They will paste back the `hb doctor` and `hb bench` output.

## Notes from the P04/P07/FIX-01 review (read before starting)
- **The mic is now the M5StickC Plus SE (`--device stick`, from P07).**
  - `hb doctor` must find the stick by its `PONG stickmic v1` reply, and check that it streams 2 s of non-silent 16 kHz audio.
  - It must also list the knocker (`PONG knocker v1`) and the rover (`PONG rover v1`, from P08) by their replies, never by port name.
  - On the Pi these show up as `/dev/ttyUSB*` or `/dev/ttyACM*`.
- **The Pi 5 has no audio output jack.** If no USB or Bluetooth speaker is found, `hb run` on the Pi must still work with knock-only prompts:
  - voice prompts are skipped, with one clear warning and an `error`-severity `warning` event
  - never crash
  - put the setting in the profile (`prompter.voice: auto|none`), not in code
- **The systemd service:**
  - command: `hb run --device stick --knocker auto --dashboard --auto`
  - `--extra ml` installed
  - default classifier `auto`
- **`hb bench`** must report which classifier ran, because the result depends on whether the trained head is present.
