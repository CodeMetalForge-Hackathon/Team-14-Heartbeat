# PI-04: get the rover answering again, or rule it out by 08:00

Read `AGENTS.md`, then `docs/PI-ROVER-CURRENT-ISSUES.md` and `docs/PI-CONNECTED-CHECK.md`.

## Why this order
Our sketch stops the motors 1.5 s after the last command (`WATCHDOG_MS`), from inside `loop()`. A track that keeps running while STOP gets no reply means **our sketch is not running at all**. One of these is happening:
- The board is resetting or browning out.
- The board holds a half-written sketch from the interrupted upload.
- Something else owns the serial port.

While the chip resets or sits in its bootloader, its pins float, and a motor driver with floating inputs can turn a track. More PING or upload retries can't fix that. Check power and port ownership first, then make **one** clean upload.

**Leading suspects, from the existing records:**
1. **A tired battery.**
   - The rover's own STATUS read 7.92 V at rest and **6.77 V** during a 35 % move with the tracks lifted.
   - Another move took it from 7.94 V to 7.08 V.
   - A 6×AA pack should read about 9 V when fresh.
   - Sag like this, which gets worse on the floor or in a stall, can reset the Auriga mid-move.
2. **A second program on the port.**
   - Every run starts with `systemctl --user stop lifescout.service`, so that service exists and competes for this hardware.
   - If it is enabled, it comes back after every reboot.
   - Two programs on one port split the bytes and fight over the baud rate. That garbles replies and makes the bootloader time out.
3. **The interrupted upload.**
   - An upload stopped mid-write leaves a half-written sketch. The bootloader survives that, so a single clean upload repairs it.
4. **USB power on the Pi.**
   - Kernel logs show the rover's CH341 and the GPS dropping off USB.
   - On a supply that isn't 5 A, the Pi 5 limits all USB devices together to 600 mA.

## Rules
- **The operator is at the rover the whole time:** tracks lifted off the table, a hand on the power switch.
- **Ask the operator and wait for a yes** before every step that powers the board or opens its port.
- **Send only PING, STOP, CAPS and STATUS** until step 5 passes. No DRIVE, STEP, CRUISE, PIVOT, TURN or EXPLORE.
- **Every command gets a bounded timeout.**
- **Never interrupt an upload that has started writing.** Let it finish or time out on its own.
- **Talk to the board by its `/dev/ttyUSB*` node** after checking it with the steps below. Don't use the `by-id` name:
  - `arduino-cli` already rejected the alias.
  - A CH340-based knocker (an Uno clone) gets the **same** `usb-1a86_USB_Serial-if00-port0` name, so `by-id` can point at the wrong board.
  - For later runs, use the rover's `/dev/serial/by-path/...` name.
- **Record every step and its result** in `docs/PI-ROVER-CURRENT-ISSUES.md`, under a new "PI-04 recovery" section.
- **Don't change** firmware, profiles or Python code in this prompt.

## Steps
0. **Save the build output; `/tmp` is wiped on reboot.**
   ```bash
   cp -r /tmp/heartbeat-rover-build ~/heartbeat-rover-build
   ```
   If it's already gone, rebuild with the compile command in `docs/PI-CONNECTED-CHECK.md`, using `--build-path ~/heartbeat-rover-build`.

1. **Read-only checks, with the rover unplugged.**
   ```bash
   systemctl --user status lifescout.service --no-pager
   ```
   ```bash
   systemctl --user is-enabled lifescout.service
   ```
   ```bash
   ps aux | grep -iE "lifescout|hb |arduino|avrdude|serial" | grep -v grep
   ```
   ```bash
   vcgencmd get_throttled
   ```
   ```bash
   dmesg -T | grep -iE "under-voltage|over-current|ch341|ttyUSB|disconnect" | tail -40
   ```
   - **If `lifescout.service` is enabled or running:** report what it runs (its `ExecStart`). Ask the operator before running `systemctl --user disable --now lifescout.service`.
   - **If `get_throttled` isn't `0x0`, or dmesg shows under-voltage or over-current:** the Pi goes on the official 27 W wall supply before anything else.

2. **Battery and wiring (the operator does this; you record it).**
   - Fit fresh batteries or a charged pack.
   - Measure the pack with a multimeter: about 9 V or more for 6×AA. Record the value.
   - Check that both motor and encoder plugs are fully seated, especially the **left** one.
   - Check that the ultrasonic sensor's cable is in port 6.
   - Check that no loose jumper wire touches the board.
   - If a Bluetooth or wireless module sits on the Auriga, remove it for this test, to rule it out.

3. **An isolated link test.**
   - Only the rover is plugged into the Pi; unplug the stick, the GPS and the knocker.
   - The Pi runs on the wall supply.
   - The rover's switch is ON, with the tracks lifted.

   First find the rover's node:
   ```bash
   ls -l /dev/serial/by-path/ /dev/serial/by-id/
   ```
   Check that nothing else holds that node. Use the node you just found, which may not be `ttyUSB0`:
   ```bash
   fuser -v /dev/ttyUSB0
   ```
   Then send one ping:
   ```bash
   uv run --no-sync hb rover --port /dev/ttyUSB0 ping
   ```
   - **`PONG rover v1`:** send CAPS, STOP and STATUS. Record the CAPS line and the battery voltage, then go to step 4.
   - **Garbage or no reply:** switch the rover off, wait 5 s, switch it on, and ping **once** more. Still bad: go to step 6.

4. **One clean upload.**
   - Check the node again with `fuser`.
   - Use the `upload` command from `docs/PI-CONNECTED-CHECK.md`, with:
     - `--input-dir ~/heartbeat-rover-build`
     - the checked `/dev/ttyUSB*` node
     - `--verify`
     - an outer `timeout 90`
   - Then send PING, CAPS, STOP and STATUS. **The expected CAPS reply is `CAPS guarded=1 cruise=1`.**
   - **If it times out:** power-cycle the rover once, recheck `fuser`, and make **one** more attempt. If that also fails, go to step 6.

5. **Lifted motion checks.** Each one needs the operator's yes first. Use the same commands and limits as the lifted-track tests in `docs/PI-CONNECTED-CHECK.md`:
   - One `STEP 3 35 1000`. Both tracks turn, then stop on their own. Record the encoder values.
   - The same step, interrupted by a separate STOP after about 450 ms. The STOP gets a reply, and `moving=0`.
   - One `CRUISE 20 35 3000`, then STOP mid-move. Do this only if both checks above passed.

   Record the battery voltage before and after each move. **If the battery drops below 7 V during a move, stop testing** and report that the battery is the limit.

6. **If the Pi can't talk to the board at all.** Write the exact commands for the operator to run on the **Mac**; don't run them yourself.
   - Copy `~/heartbeat-rover-build/rover.ino.hex` to the Mac with `scp`.
   - Flash it with `avrdude -c wiring -p m2560 -P /dev/cu.usbserial-<id> -b 115200 -D -U flash:w:rover.ino.hex:i`. avrdude comes from the Arduino IDE or from Homebrew.
   - If the Mac succeeds, the board is fine, and the problem was on the Pi side: power, the port, or the service. Move the rover back to the Pi and repeat step 3.
   - If the Mac also fails, stop. It's a board or bootloader fault, and it can't be fixed tonight.

## Done when
**One of these is recorded in `docs/PI-ROVER-CURRENT-ISSUES.md`:**
- **Recovered:**
  - PONG and STOP replies are reliable.
  - CAPS returns `guarded=1 cruise=1`.
  - Step 5 passed on the lifted chassis.
  - The battery voltage under load is recorded.
- **Ruled out:** the rover stays out of the live demo, with the evidence for why.

Then:
- **Commit docs only,** with the message `PI-04: rover recovery record`. Never commit `.env.local` or `runs/`.
- **Report:**
  - which suspect it was
  - the CAPS line
  - the battery voltages
  - the rover's `by-path` name

**The team's go/no-go is 08:00.** If the rover isn't recovered by then, the live demo is the stationary sentinel, and the rover is shown through recorded footage.
