# Standalone Raspberry Pi 5 sentinel

Use Raspberry Pi OS **64-bit** on a Pi 5. Heartbeat runs on the CPU with Python **3.11**, supplied by uv if needed. The current microphone is the **M5StickC Plus SE over USB serial**; a USB contact-mic adapter remains an alternative input.

All commands below are for a human to run. Device checks open audio/serial hardware; `--play` plays a voice prompt, and `hb run` can strike the knocker.

## Power and connections

Use the official **27 W USB-C supply**, or a USB-C PD power bank that explicitly supports **5 V / 5 A**. A bank advertised as 27 W or more may only deliver that power at higher voltages. A **5 V / 3 A** bank can boot the Pi, but Raspberry Pi OS warns and limits the shared USB peripheral budget to **600 mA**, versus **1.6 A** with a supported 5 A supply. Account for the stick, knocker and speaker; use a powered USB hub when needed. See [Raspberry Pi power requirements](https://www.raspberrypi.com/documentation/computers/raspberry-pi.html#power-supply).

Connect the stick and the separately built USB knocker with data-capable cables. The **Pi 5 has no analogue audio output jack**: use a USB speaker, a powered speaker on a USB audio adapter, or a paired Bluetooth speaker. USB audio is the simplest option for unattended boot. A Bluetooth device paired in a desktop session may depend on that session's audio server and may be unavailable to the system service before login. Test the actual cold-boot setup. See the [Pi 5 hardware specification](https://www.raspberrypi.com/products/raspberry-pi-5/).

With `prompter.voice: auto` in `profiles/rubble.yaml`, unavailable audio output skips voice with a warning and leaves serial knock prompts usable. Set `prompter.voice: none` for deliberate knock-only operation. Keep a working serial knocker connected for a standalone knock-only demo; manual prompts require an operator.

## SSH and installation

In Raspberry Pi Imager, choose the 64-bit OS and configure your own username, hostname, Wi-Fi and SSH key. Put the phone and Pi on the same Wi-Fi or hotspot. In the examples, replace `sentinel-user` and `heartbeat` with the account and hostname you selected; there is no assumed `pi` account.

From the laptop:

```sh
ssh sentinel-user@heartbeat.local
```

If `.local` does not resolve, use the address in the router/hotspot client list. On a Pi terminal, `hostname -I` prints its addresses and `hostname` prints its hostname. Avahi provides `<hostname>.local` on networks with mDNS support. Some hotspots isolate clients; try a shared router if the phone cannot reach the Pi. See [Raspberry Pi remote access](https://www.raspberrypi.com/documentation/computers/remote-access.html).

Clone as the normal user, into any writable location. The repository is private during the event: authorize an SSH key with GitHub first, or use HTTPS and an interactive credential helper. Keep tokens out of clone URLs, scripts and the service file.

```sh
git clone git@github.com:CodeMetalForge-Hackathon/Team-14.git "$HOME/Team-14"
cd "$HOME/Team-14"
bash scripts/pi_setup.sh
```

If a minimal image lacks `git`, install that prerequisite with `sudo apt-get update` and `sudo apt-get install -y git` before cloning. Setup itself installs `git`, `libportaudio2`, `libsndfile1`, `ffmpeg`, `alsa-utils`, `espeak-ng` and `avahi-daemon`. Run setup **without sudo**; it elevates only apt and group changes. It installs uv only if missing, uses `uv sync --locked --python 3.11 --extra ml`, adds this user to `dialout` and `audio`, then downloads only missing YAMNet artifacts via `tools/fetch_models.py --allow-network`. Running setup authorizes those public model downloads; the helper prints the URLs and verifies CPU inference. The clone includes the tracked synthetic-trained knock head; setup does not train or replace it.

The official [uv installer](https://docs.astral.sh/uv/getting-started/installation/) needs `curl` or `wget`; setup reuses either one already on the OS. A fresh uv install goes in `$HOME/.local/bin` without editing shell profiles, using the official [installer options](https://docs.astral.sh/uv/reference/installer/). Repeating setup keeps existing model files and reuses the locked environment. If a download fails, fix connectivity and rerun setup; it leaves completed artifacts in place.

Log out and SSH back in after setup to activate groups, then return to the clone:

```sh
exit
ssh sentinel-user@heartbeat.local
cd "$HOME/Team-14"
export PATH="$HOME/.local/bin:$PATH"
```

Use `uv run --no-sync --extra ml` after setup. After a pull that changes dependencies, first run `uv sync --locked --python 3.11 --extra ml`. A plain `uv sync` omits optional extras and can remove the ML packages; `--no-sync` runs the prepared environment without changing it. See [uv locking and syncing](https://docs.astral.sh/uv/concepts/projects/sync/).

## Choose devices and check the sentinel

Stop any running sentinel before checking devices; only one process should own a stick stream. With the stick and knocker attached:

```sh
uv run --no-sync --extra ml hb doctor --device stick
uv run --no-sync --extra ml hb devices
```

Doctor checks Python, model availability and the active classifier, disk, dashboard addresses, audio and serial devices, and Pi temperature/throttling when available. It identifies the stick, knocker and rover by `PONG stickmic v1`, `PONG knocker v1` and `PONG rover v1`, not by a `/dev/ttyUSB*` name. The chosen microphone must deliver non-silent audio for two seconds: lightly tap the contact surface during this check. An absent rover is fine for the stationary sentinel.

To select a particular stick port when more than one is attached:

```sh
uv run --no-sync --extra ml hb doctor --device stick:/dev/ttyUSB0
uv run --no-sync --extra ml hb run --device stick:/dev/ttyUSB0 --knocker auto --dashboard --auto
```

Replace that example port with the discovered one; it can instead be `/dev/ttyACM0`. Auto knocker selection looks for its protocol reply and otherwise tries speaker knocks, then manual prompts.

For voice output, use the output index or name from `hb devices`, not the ALSA card number. `aplay -l` lists ALSA playback hardware for troubleshooting. For example, if `hb devices` reports output index `3`:

```sh
uv run --no-sync --extra ml hb doctor --device stick --speaker-device 3 --play
uv run --no-sync --extra ml hb run --device stick --speaker-device 3 --knocker auto --dashboard --auto
```

Without `--play`, doctor checks output availability without playing the prompt. A USB adapter that has only an input is not a speaker. For the alternative USB microphone input, replace `--device stick` with its PortAudio input index or name. If that adapter rejects 16 kHz, capture falls back to its default supported rate and resamples to the 16 kHz processing contract; the stick already streams at 16 kHz.

## Run and measure

```sh
uv run --no-sync --extra ml hb run --device stick --knocker auto --dashboard --auto
```

On the phone, open `http://heartbeat.local:8000` (substitute the actual hostname), or `http://<Pi-IP>:8000` using the address shown by doctor. The default profile binds the dashboard to `0.0.0.0:8000`. Press Ctrl-C to stop a foreground run. The dashboard and black-box recording use the same session; recordings appear in `runs/`.

The benchmark uses synthetic audio and does not open microphone, serial or speaker devices. Run the default 120-second scene on the Pi for the deployability slide:

```sh
uv run --no-sync --extra ml hb bench
uv run --no-sync --extra ml hb bench --seconds 20 --classifier auto --seed 1
```

It prints real-time factor, mean/p95 latency for onset/classifier/dialog, peak RSS and the classifier that actually ran. Keep the reported classifier with the numbers: `auto` prefers the trained head, then YAMNet, then the heuristic, so results depend on installed artifacts. The tracked `models/knock_head.joblib` is synthetic-trained. Replace it with a validated real-recording model when available and rerun the benchmark. Paste back both the doctor checklist and the full default benchmark table.

## Start at boot with systemd

First confirm a foreground run works. `scripts/heartbeat.service` is a template: do not copy it to `/etc` with placeholders intact. From the clone as the normal user, generate a temporary unit with the actual user, current repository directory and uv executable. This also handles spaces in paths:

```sh
unit_file="$(mktemp --suffix=.service)"
uv run --no-sync --extra ml python - <<'PY' > "$unit_file"
import os
import math
from pathlib import Path
import pwd
import re
import shutil
import yaml

if os.getuid() == 0:
    raise SystemExit("Render as the normal sentinel user, without sudo")
repo = str(Path.cwd().resolve())
uv = shutil.which("uv")
if uv is None:
    raise SystemExit("uv is missing from PATH")
uv = str(Path(uv).resolve())
if any(c in repo + uv for c in "\n\r"):
    raise SystemExit("Use paths without newline characters")
restart_s = float(yaml.safe_load(Path("profiles/diagnostics.yaml").read_text())["deploy"]["restart_s"])
if not math.isfinite(restart_s) or restart_s <= 0:
    raise SystemExit("deploy.restart_s must be finite and positive")
# WorkingDirectory is one path value, not a shell command or a quoted argument.
values = {
    "@USER@": pwd.getpwuid(os.getuid()).pw_name,
    "@REPO@": repo.replace("%", "%%"),
    "@UV@": uv.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$"),
    "@RESTART_SEC@": format(restart_s, "g"),
}
template = Path("scripts/heartbeat.service").read_text()
print(re.sub(r"@(?:USER|REPO|UV|RESTART_SEC)@", lambda match: values[match[0]], template), end="")
PY
cat "$unit_file"
```

Review `User`, `WorkingDirectory` and `ExecStart` in the printed unit. If you chose a specific speaker or stick port above, edit those arguments in this temporary file with `nano "$unit_file"`. A system service has no interactive desktop audio session; prefer an ALSA-visible USB output and validate voice playback after boot. The default unit uses `--device stick`, `--knocker auto`, `--dashboard` and `--auto`; it preserves the default `auto` classifier and the installed ML extra.

Install and start the reviewed unit:

```sh
systemd-analyze verify "$unit_file"
sudo install -m 0644 "$unit_file" /etc/systemd/system/heartbeat.service
rm -- "$unit_file"
sudo systemctl daemon-reload
sudo systemctl enable --now heartbeat.service
sudo systemctl status heartbeat.service --no-pager
sudo journalctl -u heartbeat.service -f
```

The service runs as your account in the clone, has `audio`/`dialout` access, logs to the journal and restarts on failure. Its restart delay is rendered from `deploy.restart_s` in `profiles/diagnostics.yaml`; render and reinstall after changing that value. It uses the prepared virtual environment with `--no-sync`, so it does not install packages at boot. See the [systemd service reference](https://www.freedesktop.org/software/systemd/man/systemd.service.html) and [execution settings](https://www.freedesktop.org/software/systemd/man/systemd.exec.html).

After a reboot, check the phone dashboard and journal again without logging into a desktop. Ctrl-C exits journal follow only; it does not stop the sentinel. Stop it before manual probes or updates, then start it again:

```sh
sudo systemctl stop heartbeat.service
# Run doctor or update/sync while stopped.
sudo systemctl start heartbeat.service
```

To stop the sentinel and disable future boot startup:

```sh
sudo systemctl disable --now heartbeat.service
```

If the clone or uv executable moves, render and reinstall the unit again. For a service that repeatedly fails, inspect `sudo journalctl -u heartbeat.service -b`; after fixing the cause, use `sudo systemctl reset-failed heartbeat.service` and `sudo systemctl start heartbeat.service`.

## Bring recordings back

Stop the session before copying so the WAV headers and event chain are finished. From the laptop, substitute the remote repository path (it need not be `~/Team-14`):

```sh
mkdir -p ./pi-runs
rsync -av --protect-args sentinel-user@heartbeat.local:/home/sentinel-user/Team-14/runs/ ./pi-runs/
```

`rsync` must be installed on both computers; it is not added by setup. If absent on the Pi, install it there with `sudo apt-get install -y rsync`. Verify and summarize a copied session from the laptop clone:

```sh
session_dir="./pi-runs/REPLACE-WITH-SESSION-DIRECTORY"
uv run --no-sync --extra ml hb verify "$session_dir"
uv run --no-sync --extra ml hb report "$session_dir"
```

## Troubleshooting

| Symptom | Check or fix |
| --- | --- |
| Serial permission denied | `id -nG` must include `dialout`; log out and reconnect after setup. Restart the system service after group changes. |
| Stick or knocker not identified | Check USB data cables, firmware and the protocol reply in doctor. Stop competing sentinel/serial-monitor processes. Use `stick:/dev/...` to choose the stick explicitly. |
| Silent stick capture | Tap during the two-second check; inspect contact/microphone wiring and firmware. Fix the input before interpreting a lack of responses. |
| USB adapter rejects 16 kHz | Use its actual input index/name. The audio source retries at the adapter's default rate and resamples automatically. |
| No voice | Pi 5 has no output jack. Run `aplay -l`, then `hb devices`; choose `--speaker-device` from Heartbeat's output list and test with doctor `--play`. Check `prompter.voice` and speaker power/volume. |
| Voice works over desktop login only | The boot service may not have access to that user's Bluetooth/PipeWire/PulseAudio session. Use direct USB audio for the demo, or deliberately choose `prompter.voice: none` with the serial knocker. |
| Classifier unexpectedly says heuristic | Check the doctor model checklist and journal. Rerun setup for missing YAMNet files; use `uv sync --locked --python 3.11 --extra ml` for missing ML packages. |
| USB disconnects or throttling | Use the correct 5 V supply/cable and cooling; a 3 A supply reduces USB current. Inspect doctor temperature/throttling and reduce peripheral power draw. |
| Phone cannot reach dashboard | Confirm same LAN, actual IP, port 8000 and running service. Try IP instead of mDNS; disable hotspot client isolation if available. |

`NO_SIGNS` means no signs were detected in this session. No response does not establish that nobody is there.
