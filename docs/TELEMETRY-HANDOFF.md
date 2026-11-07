# Telemetry handoff for the dashboard

Checked Sat Nov 7, 15:54–16:00, from the main laptop. Every item is marked one of three ways:
- ✅ **Verified:** observed on this laptop or in this repo.
- 🟡 **Assumed or planned:** expected, or designed, but not seen working.
- ❌ **Unavailable:** does not exist on our team.

Nothing below is a made-up reading.

## Read this first: our rover is not a GNSS/LiDAR robot
The questionnaire assumes a Pi rover with GNSS, a beacon, LiDAR and a generated map. **We have none of those.** Our system has three parts:
- **Sentinel** ✅: the laptop runs `hb` (Python). It hears knocks through an M5StickC Plus SE mic on USB serial and knocks back with a servo on a Seeeduino. Its output is a dialog state (`NO_SIGNS`, `POSSIBLE`, `CONFIRMED`), the odds, and the survivor's answers.
- **Rover** ✅🟡: a Makeblock mBot Ranger (Me Auriga board, tracks). Right now it runs **factory firmware** and is driven from the Makeblock phone app over Bluetooth. Our own rover link (P08: drive, stop, listen, report per gap) is **written as a prompt but not built yet.**
- **Pi 5** ❌ (for now): not set up yet, and not reachable from this laptop.

The demo happens **indoors at our table at BASE-X, 60 State St, Boston**. GNSS indoors would be unreliable even if we had it.

## 1. Connection and software
| Question | Answer | Status |
|---|---|---|
| Laptop ↔ Pi connection | None. `heartbeat.local` and `raspberrypi.local` don't resolve, there's no SSH service on mDNS, and the ARP table shows only the router. | ✅ |
| Pi address and project directory | Not set up. Planned: hostname `heartbeat`, repo cloned to `~/Team-14` (P06). | 🟡 |
| What runs the rover | The Makeblock factory firmware plus the Makeblock app over Bluetooth. No host software. | ✅ (P08 to replace) |
| Existing telemetry interface | **Yes, for the sentinel:** FastAPI on port 8000, with REST plus a WebSocket. No MQTT and no ROS. Serial ports are used only between the laptop and the devices. | ✅ |

## 2. GNSS and rover pose
| Item | Status |
|---|---|
| Latitude, longitude, fix, accuracy, heading, speed, altitude | ❌ No GNSS module. The hackathon library has GPS modules, but we haven't checked one out. |
| Rover pose | 🟡 Planned in P08: **dead reckoning from the track encoders**, in cm and degrees relative to the start position. Published as `rover` events. Drift is unmeasured. |
| Sample fix message | ❌ None exists. |

## 3. Beacon
❌ There is no beacon hardware or protocol on our team: no RSSI, distance, bearing or coordinates.

## 4. Sensors and range
| Sensor | Field of view, range, rate | Status |
|---|---|---|
| Stick mic (SPM1423, 16 kHz mono, USB serial) | Contact mic on the slab; 16 ms audio blocks | ✅ format (software); 🟡 not yet tested on the real stick |
| Servo knocker (Seeeduino) | Actuator, not a sensor | ✅ answers on serial |
| Ranger kit ultrasonic sensor (Makeblock Me Ultrasonic) | Nominal 3–400 cm per the vendor spec | 🟡 assumed to be in the kit; not mounted or read by any software |
| Grove Ultrasonic Ranger (Creator Kit) | Nominal 3–350 cm per the vendor spec | 🟡 in the kit; not wired |
| Cameras, LiDAR, thermal, radar | — | ❌ none checked out |

No software reads obstacles or the area the rover has seen.

## 5. Generated map
❌ Nothing generates a map: no occupancy grid, point cloud, GeoJSON or coordinate transform.

🟡 Planned instead (P08): `profiles/route.yaml` lists the demo "gaps" as moves from the start, for example `Gap 1: drive 40 cm`, then `Gap 2: turn 90°, drive 30 cm`.
- A `gap_result` event per gap carries its status, odds and answers.
- A local **metric** frame (cm from the start, with the rover's start heading as +x) is the honest map frame.

## 6. Demo network
| Item | Finding | Status |
|---|---|---|
| Venue Wi-Fi | Laptop at `10.1.200.149`, router `10.1.200.1` | ✅ |
| Internet | OpenStreetMap tile → HTTP 200; Open-Meteo API → HTTP 200 | ✅ (15:54) |
| Phones ↔ laptop or Pi on venue Wi-Fi | Not tested. The ARP table shows only the router, so the network may block devices from reaching each other. | 🟡 risk |
| Recommendation | Use a **phone hotspot** for the laptop, the Pi and the phones. It has internet through cellular. Test early. | — |

## Data sources available today
| Source | Interface | Format | Units | Rate |
|---|---|---|---|---|
| Sentinel snapshot | `GET http://<host>:8000/api/state` | JSON | see the field list below | on request |
| Live events | `WS ws://<host>:8000/ws?since=<seq>` | one JSON event per message | — | `level` at 10 Hz; everything else when it happens |
| Event history | `GET /api/events?since=<seq>&limit=<n>` | JSON list | — | on request |
| Operator commands | `POST /api/command` with body `{"name": ..., "args": {...}}` | JSON | — | — |
| Black box (offline) | `runs/<id>/events.jsonl`, plus `clips/*.wav` and `raw.wav` | hash-chained JSONL | — | `level` saved at 1 Hz |

Operator commands: `challenge`, `interview`, `ask` (with `q` set to `q_people`, `q_move` or `q_hurt`), `reassure`, `reset`, `approve` (with `operator` set to initials).

**Event fields**
- `t`: seconds since the session started (stream time).
- `wall`: an ISO-8601 UTC timestamp.
- `seq`: an increasing integer. Use it with `since=` to resume.
- `data` depends on the event type:
  - `level`: `dbfs`, `floor_db`, both in dBFS
  - `sound`: `score` and `peak` (0–1), `snr_db` (dB), `is_knock`, `gated`, `classifier`, `top`
  - `round_result`: `expected`, `heard`, `p`, `odds`, `mu` (expected background knocks per window), `lambda_used_per_min`
  - `state`: `state`, `status`, `odds`, `combined_p`, `answers`, `approved`, `noisy`
  - `answer`: `question`, `heard`, `value`

**Status values:**
- `state`: `LISTENING`, `PROMPTING`, `RESPONSE_WINDOW`, `TOO_NOISY`, `CONFIRMED`, `INTERVIEW`, `NO_RESPONSE`
- `status`: `NO_SIGNS`, `POSSIBLE`, `CONFIRMED`

Planned in P08 (not built yet): `rover` (commands, replies, pose, battery), `gap_result`, `patrol_done`.

## Real sample messages
These were produced by the real software in **simulation mode** at 15:55 (`hb sim`). There's no real-hardware session yet. The `hash` and `prev` fields are removed from all but the first message.

```json
{"seq": 1, "t": 0.0, "type": "session_start", "wall": "2026-11-07T20:55:02.020361+00:00", "data": {}, "prev": "0000…0000", "hash": "5581dab4…"}
{"seq": 2, "t": 0.0, "type": "state", "wall": "2026-11-07T20:55:02.022309+00:00", "data": {"state": "LISTENING", "status": "NO_SIGNS", "odds": "1 in 1", "combined_p": 1.0, "round": 0, "rounds": [], "answers": {}, "approved": false, "noisy": false, "mu": 0.1067, "lambda_per_min": 1.0, "lambda_used_per_min": 1.0, "profile": "rubble"}}
{"seq": 3, "t": 0.016, "type": "level", "data": {"dbfs": -53.59, "floor_db": -50.0}}
{"seq": 5, "t": 0.8, "type": "sound", "data": {"classifier": "head", "score": 0.929, "is_knock": true, "gated": false, "peak": 0.399, "snr_db": 36.9, "top": [["knock", 0.929], ["other", 0.071]], "clip": "clips/5.wav", "clip_sha256": "af3a9f2f…"}}
{"seq": 15, "t": 7.264, "type": "prompt_start", "data": {"batch": 1, "actions": [{"action": "PromptVoice", "key": "copy_knocks"}, {"action": "PromptKnocks", "n": 3, "interval_s": 0.6}]}}
{"seq": 25, "t": 13.904, "type": "window_open", "data": {"kind": "round", "round": 1, "expected": 3, "start": 13.904, "end": 20.304}}
{"seq": 34, "t": 18.32, "type": "round_result", "data": {"round": 1, "expected": 3, "heard": 3, "knock_times": [15.232, 15.648, 16.32], "matched": true, "closed_early": true, "starts_promptly": true, "bounce": false, "noisy": false, "p": 0.00302, "combined_p": 0.00302, "odds": "1 in 331", "mu": 0.289, "lambda_used_per_min": 2.71}}
{"seq": 70, "t": 38.152, "type": "answer", "data": {"question": "q_people", "heard": 2, "value": 2}}
```
`/api/state` returns the latest `state` data, plus: `run_id`, `profile`, `sentinel_name`, `uptime`, `pipeline_running`, `approved_by`, `state_seq`, `latest_seq`, `blackbox`, `transport`.

## Commands to get live data with no hardware
Run these from the repo root on the laptop:
```bash
uv run --extra ml hb sim --people 2 --dashboard --realtime
curl -s http://localhost:8000/api/state
```
- `hb sim` streams a full simulated rescue at real speed to `/ws`.
- `curl` returns the current snapshot.
- Use the laptop's IP instead of `localhost` from a phone on the same network.

## Simplest path to a mobile OpenLayers dashboard
1. **No bridge needed for live data.** Have the page open `ws://<laptop-ip>:8000/ws` directly. A WebSocket isn't blocked by cross-origin rules.
2. **REST calls from another origin will be blocked.** The server has no CORS headers. Choose one fix:
   - serve the page from our server (put it in `dashboard/`), or
   - add FastAPI's `CORSMiddleware` (ask Claude/Codex; it's a one-line change), or
   - relay through your Python bridge.
3. **A bridge is only needed for extras** such as weather. A small FastAPI app can subscribe to `/ws`, add Open-Meteo data, and re-serve both.
4. **Map frame:** use an OpenLayers custom projection in **centimeters from the rover's start**, and draw the gaps from `profiles/route.yaml` once P08 lands.
   - A street basemap only makes sense if the team picks an anchor point by hand. Label anything placed that way **"approximate (dead reckoning), not GNSS."**
   - Indoors at the venue, a floor-plan sketch of the table area is more honest than map tiles.

## Missing information and blockers
1. **P08 rover link isn't built.** There is no rover pose, battery or per-gap telemetry until it lands.
2. **The Pi isn't set up (P06).** Until then, everything runs on the laptop.
3. **No GNSS, beacon, LiDAR, camera or map.** If geo-positioning is required, someone must check out a GPS module. It would still be poor indoors.
4. **Network:** client-to-client reachability on venue Wi-Fi is unverified. Plan on a phone hotspot.
5. **REST needs CORS** if the dashboard is served from a different origin.
6. **The stick mic hasn't run on real hardware yet.** Live `sound` and `level` values from the real slab will differ from the simulation.
