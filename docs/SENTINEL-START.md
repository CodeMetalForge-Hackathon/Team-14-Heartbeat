# Sentinel: start here (Sat 13:00)

**The sentinel is the product.** The rover is a way to carry it to places we can't reach. Build the sentinel first, so that if the rover slips, we still have a complete demo.

## Done by 19:00
- [ ] The contact mic on the "slab" hears knocks: `hb listen` shows them.
- [ ] The servo knocker answers `PING` and knocks on command.
- [ ] `hb run` completes a full conversation with a teammate knocking from under the table:
  - challenge, then 2 matched rounds
  - **CONFIRMED** with the odds
  - interview: people / can move / hurt
- [ ] Dropping a book, stomping and talking never cause a CONFIRMED.
- [ ] The dashboard runs live on a phone, "Approve dig" gets logged, and `hb verify` passes.
- [ ] We have measured numbers:
  - knocks detected out of 50
  - false knocks per hour of venue noise
  - false confirmations (target 0)
  - average time to confirm
- [ ] A **backup demo video** is recorded. Mentors said a video is acceptable.

## Who does what right now
| Person | Next 90 minutes |
|---|---|
| **Codex driver** (you) | Set up the repo (below), then run **P01** in Codex session A and **P02** in session B. Paste each report to Claude. |
| **Hardware** | Build the contact mic, then the servo knocker. Get a speaker. |
| **Data** | Start recording now with ffmpeg (below). Switch to `hb record` once P01 lands. |
| **Rover team** | Finish assembly using the notes at the bottom. Take a photo of the Grove kit contents and send it to the Codex driver. |

## 1. Repo
1. The repo is `https://github.com/CodeMetalForge-Hackathon/Team-14`. It already contains this guide, `AGENTS.md` and `prompts/`. Get your copy:
   ```bash
   git clone https://github.com/CodeMetalForge-Hackathon/Team-14.git
   ```
2. Run `codex` in the repo root, and follow the "How to run one" section in `prompts/README.md`.
3. Before you push, run `git pull --rebase`. Two Codex lanes and several teammates share `main`.
4. The first time something uses the mic, macOS asks to allow microphone access for your terminal. Click Allow.

## 2. Contact mic (Hardware, about 30 minutes)
A contact mic picks up vibration through the solid material, not sound in the air. That's exactly how knocks travel through rubble, and it ignores much of the room noise.

**What you need**
- A **piezo disc**: a bare buzzer element, a Grove piezo vibration sensor, or a guitar pickup.
- A **USB audio adapter with a separate pink mic jack**. A single combo headset jack is awkward to wire.
- A 3.5 mm mono or stereo plug, or an old aux cable you can cut.

**Wiring**
- Piezo red wire to the plug **tip**.
- Piezo black wire to the plug **sleeve** (ground). Leave the ring unconnected.
- The adapter's mic bias voltage won't hurt the piezo.

**Mounting.** Coupling is everything.
- Press the disc flat onto the slab with Blu-Tack or putty, or tape it down hard.
- Put a scrap of foam over it to block airborne sound.
- Never let the disc dangle.

**Test**
1. Find the adapter's audio index (see the ffmpeg commands below).
2. Knock on the slab 1 m away from the disc. The level should jump far above the room noise.
3. If very hard knocks next to the disc clip, lower the input volume: System Settings → Sound → Input.

**Fallbacks**
- A USB or lapel mic taped face-down on the slab. It works, but it hears more of the room.
- The laptop mic, for software development only.

**The Pi 5 has no audio input jack**, so the USB adapter is required there. If we can't get one, tell Claude: there is an ESP32 fallback, where the piezo goes into the ESP32's ADC and the samples stream over serial.

## 3. Servo knocker (Hardware, about 45 minutes)
- **Servo:** a Grove servo, or an SG90/MG90S from the shared library. Bring a spare.
- **Hammer:** zip-tie a popsicle stick to the horn, with an M4–M6 bolt or nut on the end as the head.
- **Mounting:** hot-glue or tape the servo to a block so the hammer rests 1–2 cm above the slab, and place it **at least 30 cm from the contact mic**.
- **Wiring:**
  - Grove servo: plug into port **D5** on the Arduino's Grove shield.
  - Bare SG90: brown to GND, red to **5V**, orange to **D5**.
- **Flash and test:** follow `firmware/knocker/README.md` once Codex finishes P02.
- **Speaker:** plug a small powered speaker into the USB adapter's headphone jack, or use a Bluetooth speaker. Until then, use the laptop speaker.

## 4. Record data (Data person, starting now)
The venue is a free dataset of exactly the noise we must reject.

**Before `hb record` exists**, use ffmpeg on the Mac:
```bash
ffmpeg -f avfoundation -list_devices true -i ""
```
```bash
ffmpeg -f avfoundation -i ":1" -ac 1 -ar 16000 -c:a pcm_s16le -t 60 data/raw/knock__table__knuckles-medium-1m__01.wav
```
In the second command:
- Replace `:1` with the USB adapter's index from the list printed by the first command.
- `-t 60` stops after 60 seconds. Leave it out and press `q` to stop whenever you like.

**Naming:** `label__surface__description__NN.wav`. The label is one of:
- `knock`
- `other`: non-knock sounds
- `bg`: long venue background
- `self`: our own servo or speaker

**Rules:**
- **One label per file.** In a `knock` file, only knock: no talking, coughing or bumping. In an `other` file, never knock.
- Always use the **contact mic on the real demo slab**. Data from the laptop mic doesn't transfer.

| Record | How much | Notes |
|---|---|---|
| Knocks: knuckles, fist, and a rock or metal object | 12+ files × 60 s, about 300 knocks | Mix soft, medium and hard. Knock at 30 cm, 1 m and 2 m+ from the disc. Do singles 1–2 s apart, and groups of 2, 3 and 4. |
| Knocks on other surfaces | 1–2 files each | Plywood, cardboard box, metal cabinet or door frame, concrete floor or wall |
| Other | 15+ files × 60 s | Talking nearby, footsteps, chair scraping, dropped pen/book/bottle/keys, claps, bumping the table, typing, a bag landing on the table, door slams, music from a phone, the rover driving |
| Self | 2–3 files | The servo knocker running patterns, and the speaker playing prompts |
| Background | **1 file, 30–60 min** | Leave it recording during normal venue chaos. This measures false alarms. |

Raw data stays out of git (`data/raw/` is ignored). Share it with the team on a drive.

## 5. Rover team notes (mBot Ranger)
- Build the **Land Raider (tracked) form**. It's the most stable over debris and can carry the payload.
- Test it with the Makeblock app over Bluetooth. **Don't flash custom firmware yet;** Claude will write P07 for the rover bridge.
- **Measure and report back:**
  - Is there room for a Pi 5 plus a USB-C power bank (about 350 g)?
  - Where can a spring-loaded "foot" hold the contact mic against the ground when the rover stops?
  - Which Auriga ports are free?
  - How long do the batteries last while driving?
- Record `other__floor__rover-driving__01.wav` with the contact mic on the floor while it drives. Motor noise is a key negative.

## 6. Ask the organizers or Micro Center
- **Shared library:**
  - USB audio adapter
  - piezo or contact mic
  - small speaker
  - 2× servo
  - mmWave radar: **ask whether it outputs breathing or only presence**
  - 2× LoRa modules
  - ESP32
- **Wish list:**
  - piezo discs (a 10-pack)
  - USB audio adapter with a separate mic jack
  - 3.5 mm plugs or an aux cable
  - mini powered speaker
  - SG90/MG90S servos
  - 470 µF capacitors
  - USB-C PD power bank (27 W+) for the Pi
  - Seeed MR60BHA2 60 GHz breathing radar
- **Build materials:** cardboard boxes and a sheet of plywood for the "rubble."

## Timeline
| Time | Milestone |
|---|---|
| 13:00 | Repo set up. P01 (lane A) and P02 (lane B) running. Data recording starts. |
| 13:45 | P01 done. **P03** (A) and **P05** (B) start. The contact mic is on the slab and `hb listen` shows knocks. |
| 14:45 | P03 done: `hb sim --people 2` confirms. Knocker flashed and knocking. **P04** starts. |
| 15:30 | First real end-to-end run on the laptop: `hb run --knocker <port>` with a teammate under the table. |
| 16:30 | P04 done: dashboard and black box. **P06** starts. P05 trained on real data. |
| 17:30 | Sentinel running on the Pi alone, dashboard on a phone. |
| 18:00–19:00 | Measure the numbers. Record the backup video. Plan the rover (P07) with Claude. |
