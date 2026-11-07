# Knocker setup (hardware person)

The goal is a servo "hammer" that knocks on the slab when the laptop tells it to. It takes about 45 minutes. Run every command from the repo folder, `Team-14`.

## Parts
- **Seeeduino.** A Seeeduino V4.2 needs the Grove Base Shield on top; a Seeeduino Lotus has Grove ports built in.
- **Servo:** an SG90 or MG90S from the library. The Grove Creator Kit probably doesn't include one.
- **Wires:** a Grove-to-jumper cable, or 3 jumper wires.
- **A data USB cable.** Many cables are charge-only; if the board isn't detected, swap the cable first.
- **Hammer parts:**
  - a popsicle stick or a short ruler
  - 2 zip ties
  - an M4–M6 bolt or nut, or a large screw
  - hot glue or strong tape
  - a small wood block or a sturdy box
- **Optional:** a 470 µF capacitor.

## 1. Board switch
If the Seeeduino has a **3V3 / 5V switch**, set it to **5V**. The servo needs 5 V.

## 2. Wire the servo (USB unplugged)
Use either option:
- **Option A, Grove port D5:**
  - Grove yellow → servo orange (signal)
  - Grove red → servo red
  - Grove black → servo brown
  - Grove white is unused.
- **Option B, jumper wires to the board pins:**
  - servo orange → **D5**
  - servo red → **5V**
  - servo brown → **GND**

## 3. Install the tools (once)
```bash
brew install arduino-cli
arduino-cli core update-index
arduino-cli core install arduino:avr
arduino-cli lib install Servo
```

## 4. Find the port
Plug in the USB cable, then run:
```bash
arduino-cli board list
```
Look for `/dev/cu.usbserial-…` or `/dev/cu.usbmodem…`, and save it for the later steps:
```bash
export PORT=/dev/cu.usbserial-1420
```
Use your actual port name, and run the `export` again in any new terminal.

If nothing shows up, try in this order:
1. A different cable.
2. A different USB port or adapter.
3. Unplug the board and plug it back in.

## 5. Compile and upload
```bash
arduino-cli compile --fqbn arduino:avr:uno firmware/knocker
arduino-cli upload -p $PORT --fqbn arduino:avr:uno firmware/knocker
```
- Seeeduino V4.2 and Lotus are Uno-compatible, so `arduino:avr:uno` is correct.
- If upload fails with "not in sync", check which board it really is (the name is printed on it) and tell Claude.

## 6. Talk to it
Close the Arduino IDE's Serial Monitor if it's open, then run:
```bash
uv run python tools/knocker_test.py --port $PORT ping
```
- **Expected:** a `READY knocker v1 rest=60 strike=95 hold=70` line, then `PONG knocker v1`.
- **If `READY` doesn't arrive:** run the command again and press the board's RESET button during the 4-second wait.

## 7. First swing (hammer not attached yet)
```bash
uv run python tools/knocker_test.py --port $PORT knock 3 --interval 450
```
- **Expected:** the horn moves 3 times, returns, and then goes **silent**. The reply is `OK K 3`.
- **If the board resets or the USB disconnects when the servo moves:**
  - put the 470 µF capacitor across the servo's 5V and GND (the stripe goes to GND), or
  - power the servo from a separate 5 V supply, with GND shared with the board.

## 8. Build the hammer
1. Zip-tie the popsicle stick to the servo horn. Hot-glue or tape the bolt or nut to its tip.
2. Hot-glue or tape the servo to the block, so that at rest the hammer head sits **1–2 cm above the slab**.
3. Put the knocker on the **same slab** as the contact mic, **at least 30 cm away** from it.

## 9. Calibrate the swing
`cal REST STRIKE HOLD` saves the angles on the board. The defaults are 60 / 95 / 70. Repeat this pair of commands with new numbers until the knock sounds right:
```bash
uv run python tools/knocker_test.py --port $PORT cal 60 95 70
uv run python tools/knocker_test.py --port $PORT knock 1 --interval 450
```
| What you see | Change |
|---|---|
| The hammer doesn't reach the slab | Move STRIKE 5° further from REST (100, then 105…) |
| It swings the wrong way | Mirror it, for example `cal 120 85 70` |
| It hits, then stays pressed, or the servo buzzes against the slab | Move STRIKE 5° back toward REST |
| The knock is weak | Raise HOLD to 90, or start the hammer higher |
| It double-bounces | Lower HOLD to 50–60 |

- **Target:** one crisp "tok" per knock, and silence after the pattern.
- **Check that it saved:** unplug the USB, plug it back in, and run `ping` again. The `READY` line should show your new values.

## 10. Check that the mic hears it
1. The Codex driver runs `uv run hb listen --device $DEV`.
2. You run `knock 3`.
3. Three sounds should appear, and none of them should show `peak 1.00`. If they do, move the knocker further from the mic, or turn the input volume down.
4. Then record the `self` file: section 4 of `docs/RECORDING.md`.

## 11. The full sentinel test (with the Codex driver)
```bash
uv run hb run --device $DEV --knocker $PORT
```
1. Type `c` and press Enter. The sentinel speaks, then the hammer knocks 3 times.
2. A teammate under the table knocks back the same number of times.
3. The sentinel runs another round with a different number, and the teammate copies again.
4. **Expected:** after 2 matched rounds, it reaches **CONFIRMED** and shows the odds.
5. Type `i` and press Enter for the interview (people / can move / hurt).

Paste the console output to Claude.
