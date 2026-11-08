# Demo hookup: everything on the Pi

**For** the Pi teammate and the rover team. **Time:** about 1 hour the first time.

**The goal:** one Pi 5 runs the whole system.
- The stick listens.
- CLAP classifies.
- The dialog decides.
- The knocker and the Grove speaker talk back.
- The rover drives between gaps.
- Phones watch the dashboard.

Every Pi command runs from `~/Team-14`.

## 0. What connects where
| Part | Connects to | Notes |
|---|---|---|
| M5StickC Plus SE (mic) | Pi USB | USB-C **data** cable |
| Seeeduino knocker | Pi USB | The servo draws its power through this cable |
| mBot Ranger (Auriga) | Pi USB | The rover's own battery runs its motors; the rover must be switched on |
| Grove Speaker | Pi header: yellow → GPIO 12 (pin 32), red → 5V (pin 2), black → GND (pin 6) | Grove-to-female jumper cable; tape the white wire's tip |
| Power | Pi USB-C | See section 1 |

**Plug each USB device into the same Pi port every time,** and put a tape label on the port: "STICK", "KNOCK", "ROVER".

## 1. Power: check this first
- **On the table, the stationary demo:** use the **official 27 W Pi supply** from a wall socket.
- **On the rover:** use a USB-C PD bank that explicitly supports **5 V / 5 A**, or a powered USB hub.
  - A normal 5 V / 3 A bank limits all USB devices together to 600 mA.
  - The servo alone can spike above that.
- **The under-power symptom:** the stick or the knocker disconnects at the moment the servo knocks. You'll see errors in `hb run`, or a lightning-bolt warning in the Pi's log.
  - If you see it, use the wall supply or a powered hub.

## 2. The Grove speaker (once)
1. With the Pi off, do the wiring from the table above.
2. Turn on the audio output on GPIO 12 and 13:
   ```bash
   echo "dtoverlay=audremap-pi5" | sudo tee -a /boot/firmware/config.txt
   ```
3. Restart the Pi:
   ```bash
   sudo reboot
   ```
4. List the sound cards. A **new** card should appear; note its number:
   ```bash
   aplay -l
   ```
5. Play a test sound. Replace `2` with that card number:
   ```bash
   aplay -D plughw:2,0 assets/voice/we_hear_you.wav
   ```
6. Make it the default output, so `hb` uses it, even when it starts at boot. Use the same card number in `N=2`:
   ```bash
   N=2; printf 'pcm.!default { type plug; slave.pcm "hw:%s,0" }\nctl.!default { type hw; card %s }\n' $N $N > ~/.asoundrc
   ```

**Volume:** the small knob on the speaker, plus `alsamixer` (press F6 to pick the card).

**Distorted sound** usually means too much signal, not a broken speaker. The Grove Speaker's amplifier is designed for Arduino tones, so a full 3.3 V signal from the Pi overdrives it. Try these in order:
1. Turn the Grove knob almost to minimum.
2. Turn the Pi's output down to about 40%. In `alsamixer`, press F6 to pick the card, then use the down arrow.
3. If it's still harsh, put a **10 kΩ resistor** in series with the yellow wire, between the Pi's pin 32 and SIG. Ask the hardware table for one; it cuts the level and the high-frequency hiss.

If no new card appears, send Claude the output of `aplay -l`. **The demo still works without the speaker:** the voice is skipped and the knocks continue.

## 3. Update the code
```bash
cd ~/Team-14 && git pull
```
The core project gains no new packages from CLAP, so you don't need to re-run setup.

## 4. Find the devices
With everything plugged in, ask Heartbeat to identify each device:
```bash
uv run --no-sync --extra ml hb doctor --device stick
```
It identifies the stick, knocker and rover by their replies and prints each one's `/dev/ttyUSB*`. Now list the stable names:
```bash
ls -l /dev/serial/by-path/
```
Match each `ttyUSB` from the doctor to its `by-path` name, and save the rover's:
```bash
export ROVER=/dev/serial/by-path/<the rover's entry>
```
The stick and the knocker are found automatically (`--device stick`, `--knocker auto`).

Use `by-path`, not `by-id`. The Auriga and the Seeeduino can carry identical USB chip names, so `by-id` may only show one of them. A `by-path` name stays the same as long as the device stays in the same port.

## 5. CLAP: try it on the Pi first
Running CLAP on the Pi itself means **no Wi-Fi in the loop**, which is the best for the demo and for deployability.

1. **Start the service, terminal 1.** The first start downloads PyTorch and the model, about 1–2 GB, so it needs internet:
   ```bash
   uv run --python 3.12 --script tools/clap_service.py --profile profiles/clap-distress.yaml --allow-download
   ```
   Later starts can drop `--allow-download`.
2. **Check that the Pi keeps up, terminal 2.** First make a 60-second synthetic scene:
   ```bash
   uv run --no-sync hb synth --out /tmp/scene.wav --seconds 60
   ```
   Then replay it in real time:
   ```bash
   uv run --no-sync hb listen --wav /tmp/scene.wav --profile clap-distress --realtime --debug
   ```
3. **Repeat it with real-world load, using the real stick,** because a synthetic file is gentler than real use. Start the listener:
   ```bash
   uv run --no-sync hb listen --device stick --profile clap-distress --debug
   ```
   Then, for 30 seconds each:
   - talk normally right next to the stick
   - knock fast, about 10 knocks in 3 seconds
   - play a prompt through the speaker: `aplay assets/voice/stop_and_listen.wav`

   **Until CLAP-05 lands, any of these can stop the listener on the Pi.** Every detected sound goes to CLAP, including our own prompts, and the Pi handles about 2 per second.
4. **Read the result.**
   - **Passed:** both runs go to the end without "listening stopped", "queue is full" or "maximum audio lag". Use the Pi.
   - **Too slow:** it stops with one of those errors. First try `cpu_threads: 4` in `profiles/clap-distress.yaml`, then restart both terminals. If it still fails, use the Mac (step 4).
   - After CLAP-05 lands, `hb doctor --profile clap-distress` also prints the CLAP timing.
5. **Mac fallback:** put the Mac and the Pi on the **same phone hotspot**. Venue Wi-Fi often blocks one device from reaching another.
   1. On the Mac, at the same git commit as the Pi, start the service so the Pi can reach it:
      ```bash
      uv run --python 3.12 --script tools/clap_service.py --profile profiles/clap-distress.yaml --host 0.0.0.0
      ```
   2. On the Mac, get its address:
      ```bash
      ipconfig getifaddr en0
      ```
   3. On the Pi, make a local profile that points at the Mac. Don't commit it. Replace `<MAC_IP>` with that address:
      ```bash
      sed 's#127.0.0.1:8767#<MAC_IP>:8767#' profiles/clap-distress.yaml > profiles/demo-mac.yaml
      ```
   4. Use `--profile demo-mac` instead of `--profile clap-distress` below.

## 6. The sentinel: the main demo
Keep the CLAP service running in terminal 1. In terminal 2:
```bash
uv run --no-sync --extra ml hb run --device stick --knocker auto --profile clap-distress --dashboard
```
- **Startup must print** `Knocker: /dev/ttyUSB… (PONG knocker v1)`. If it prints "No knocker found", check the knocker's cable and port.
- **The dashboard:** phones on the same network open `http://heartbeat.local:8000`. If that doesn't load, use `http://<Pi IP>:8000`; `hostname -I` prints the Pi's IP.
- **The demo flow:**
  1. Everyone stays quiet.
  2. Press **All quiet → Challenge**.
  3. The "survivor" under the table copies the knocks.
  4. The dashboard reaches **CONFIRMED**, then the interview.

## 7. The rover patrol
**First, flash the latest rover firmware** (`firmware/rover/README.md`). The ultrasonic exploration and guarded moves need it.
- After the flash, **"forward" is the side with the ultrasonic sensor.** That's intentional (`DRIVE_FORWARD_POLARITY`), so expect it to drive the opposite way from before.
- Calibration is kept on the board, so you don't need to redo it.

**Pick one mode for the demo:**
- **Route mode** (default): fixed moves from `profiles/route.yaml`. It's the most predictable. Measure the real gaps on the demo floor and put them in the file.
- **Explore mode:** add `--explore` to the patrol command **and use `--profile clap-knocks`**; the code refuses explore with `clap-distress`. The rover drives forward, stops at obstacles using the ultrasonic sensor, and turns to search.
  - The same running CLAP service works for both profiles.
  - In this profile **one** accepted knock stops the rover (`knock_count: 1`). If track noise keeps stopping it, set `rover.audio_stop.knock_count: 2` in `profiles/clap-knocks.yaml`.
- **Not Decisions mode** (`--decisions`). It needs a Pi camera, which we don't have on the rover, plus a live OpenAI API connection, and it has never been run end to end. Show it as "next step" in the pitch only.

**Steps:**
1. Lift the rover off the table and check the link:
   ```bash
   uv run --no-sync hb rover --port $ROVER ping
   ```
2. Start the patrol in route mode, with the CLAP service still running. For explore mode, add `--explore` and use `--profile clap-knocks`:
   ```bash
   uv run --no-sync --extra ml hb patrol --rover $ROVER --device stick --knocker auto --profile clap-distress --dashboard
   ```
   The rover drives to each gap, stops, settles, listens, and challenges. The motors are always off while it listens.

**The emergency stop** is on the dashboard. Rover plus CLAP hasn't been tested on hardware yet, so rehearse it at least twice.

## 8. If something breaks in front of the judges
Work down this list. Every step has already worked on its own.

| Problem | Do this |
|---|---|
| The CLAP service crashes or is slow | Restart terminal 2 with `--profile live` (the rule-based classifier). The dialog still makes the confirmation decision. |
| The speaker is silent | Keep going: the voice is skipped, and the knocks still prompt the survivor. |
| The rover misbehaves | Hit E-stop on the dashboard and switch to the stationary sentinel only (section 6). |
| The Pi dies | Run the same `hb run` command on the Mac, with the stick and knocker plugged into the Mac. |
| Everything dies | Play the **backup video** of a full CONFIRMED run. Film it tonight. |
