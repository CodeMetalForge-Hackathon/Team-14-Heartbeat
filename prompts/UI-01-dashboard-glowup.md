# UI-01: make the dashboard look like a real command post

Read `AGENTS.md` first, then all of `dashboard/` (`index.html`, `app.js`, `style.css`).

## Why
Judges watch the dashboard on a projector or laptop for three minutes. Today it's correct but plain: tables, a level trace and an event log. It should make the story obvious at a glance:
1. **Listen:** something is heard.
2. **Ask:** we knock a changing pattern.
3. **Answer:** it copies the pattern.
4. **CONFIRMED:** a person, not noise.

It should also look alive, and stay honest.

**Run this on the laptop, not the Pi.** It only touches `dashboard/`.

## Files you own
- `dashboard/` only: `index.html`, `app.js`, `style.css`, and new files inside `dashboard/` if needed.
- **Don't touch** Python, the profiles, the WebSocket/API protocol or the event shapes.
- Everything the map needs is already sent:
  - `state.patrol.route` and `state.patrol.results`
  - `patrol`, `sound`, `prompt_start`, `prompt_end`, `window_open` and `round_result` events

## Hard rules
- **Works offline.** No CDN, no Google Fonts, no map tiles, no npm or build step. Use plain HTML, CSS and JS, inline SVG icons, and system fonts. The server already serves everything inside `dashboard/`.
- **No fake data.**
  - GPS has no fix and isn't wired in, so **no geographic map and no GPS pin**.
  - The map is a schematic of the planned route, labelled **"Route schematic · from planned moves, not GPS"**.
  - A single microphone has no direction, so **never draw a sound at a bearing or position** it wasn't heard from. Sound pulses come from the sentinel's or rover's own marker.
- **Keep every existing element `id` and behavior** that `app.js` uses, plus all controls:
  - Challenge, All quiet → Challenge, Interview, Reassure, Reset
  - Approve dig, with its dialog
  - patrol Pause / Resume / Abort, and E-STOP
- **Keep the safety and honesty text visible:**
  - the CLAP-offline banner
  - the arrival-unverified warning
  - "Estimate assumes independent background knocks"
  - "An unclear answer stays unclear"
  - the black-box status
- **E-STOP stays one tap away at all times while a patrol is active,** including on a phone. Make it sticky.
- **Cheap animations.** The dashboard can be open on the Pi itself, which also runs CLAP.
  - Animate CSS `transform` and `opacity` only.
  - Use one shared `requestAnimationFrame` loop, at most 30 fps, paused when `document.hidden` is true.
  - No blur filters on large areas.
  - Honor `prefers-reduced-motion`: no pulsing, only instant state changes.
- **Readable on a projector and a phone.**
  - Big status text with high contrast.
  - Works from 360 px wide up to 1920 px.
  - No horizontal scroll.

## Changes, in priority order
Commit after part 1, so a partial result still ships.

### Part 1: the look and the story (must have)
1. **A command-post layout.**
   - **At 1200 px and wider:** a two-column grid.
     - Left: the status hero, then the map (part 2).
     - Right: the live trace, call-and-response, answers and rounds.
     - The event log goes below both, collapsed to the last 8 entries, with a "Show all" button.
   - **On phones:** one column, in the order status → trace → call-and-response → controls → the rest.
2. **The status hero.** Make the current `status-card` the centerpiece:
   - `NO_SIGNS`: calm, with a slow breathing glow.
   - `POSSIBLE`: amber, with a faster pulse.
   - `CONFIRMED`: green, with a one-time burst animation when it's first reached, then a steady heartbeat pulse on the brand mark.
   - Animate state changes: a quick crossfade or slide of the title.
   - Keep `phase`, `odds` and `approval-badge` inside the hero.
3. **A call-and-response strip.** This is new, and it's the clearest explanation of the idea. For each challenge round, show a row:
   - **"We knocked":** N blue dots, from `prompt_start` actions / `round_result.expected`.
   - **"Heard":** M dots that pop in as accepted knocks (`sound`, `is_knock`, not `gated`) arrive in the open answer window.
   - **The verdict:** a ✓ or ✗ chip from `round_result.matched`, plus that round's odds.
   - The rows stack up, so the changing pattern is visible: 3, then 5, then 2.
   - Keep the existing rounds table, but it can become a small "details" table under the strip.
4. **The trace as a heartbeat monitor.**
   - Restyle the `trace` canvas like an ECG: a glowing level line, a subtle grid, and a faint noise-floor band.
   - Knock markers flash briefly when they arrive.
   - Prompt spans and answer windows stay visible, as now.
   - Keep the tooltip and the legend.
5. **Polish.**
   - Consistent spacing; panels with a subtle border and a gradient.
   - Make the event log readable:
     - icons per event type
     - errors in amber or red
     - answers, approvals and CONFIRMED highlighted
   - The connection pill pulses while connecting.
   - Restyle the CLAP banner as a clear amber strip at the top.
   - Make buttons large enough to tap.
6. **Presentation mode.**
   - Press `P`, or open `/?present=1`.
   - Hides the event log and the details tables, and enlarges the hero, the call-and-response strip and the map.
   - The controls stay usable.

**Commit:** `UI-01: command-post layout, status hero, call-and-response`

### Part 2: the search map (should have)
7. **A "Search map" panel,** inline SVG, next to the hero.
   - **With a patrol** (`state.patrol.route` present):
     - Dead-reckon the planned route from each gap's `moves`. A `drive` cm goes along the current heading; a `turn` deg changes the heading.
     - Draw the path, and a marker at each gap labelled with its name.
     - **Colour each gap by its result:** pending, LISTENING, CLEAR or NO_SIGNS, POSSIBLE, CONFIRMED, and approved (with a check).
     - Draw a rover marker: at the current gap while stopped; tweened along the segment while `patrol.moving` is true, over roughly the move's expected time.
     - If `arrival_unverified` is true, draw the rover marker hollow with a "?" and keep the existing warning.
     - **For explore and decisions modes,** which have no route: show the rover at the center, with `front_cm` as a short forward ray when it's known, and the steps count.
   - **Without a patrol** (the stationary sentinel): one sentinel node in the center, labelled with `sentinel_name`.
   - **Pulses, always from the sentinel's or rover's own marker:**
     - our prompt: a blue expanding ring
     - an accepted knock: a bright ring
     - a gated or rejected sound: a faint grey ring
     - vocal distress (`audio_observation` with `alert`): an amber ring
   - **CONFIRMED** pins a beacon at that location, with a heartbeat pulse until approval.
   - A grid background and an animated radar sweep are fine, if they're slow and subtle.
   - Show the honesty label from the rules in the panel's corner, along with a scale bar in cm.

**Commit:** `UI-01: search map schematic`

## How to look at it without hardware
- **The stationary flow:**
  ```bash
  uv run --no-sync hb sim --realtime --dashboard
  ```
  Then open `http://localhost:8000`. Run it with and without `--no-survivor`.
- **The patrol flow, with the map:**
  ```bash
  uv run --no-sync hb patrol --dry-run --profile rover-audio-demo --dashboard
  ```
  Then open `http://localhost:8000`.
- Check at 375 px, 768 px and 1440 px wide. Check presentation mode, reduced motion, and a disconnect: stop the process and watch the reconnect.

## Tests
- `uv run --no-sync pytest -q` passes. `tests/test_server.py` serves the dashboard.
- No JS test framework exists; don't add one.
- Instead, report exactly which flows you watched, at which widths, and anything you couldn't check.

## Done when
- Both parts are committed. Or, if time runs out, only part 1, with part 2 listed as not done.
- **Report:**
  - what changed
  - the flows and widths you checked
  - any element `id` you renamed (ideally none)
  - anything left undone

Claude will review it in a browser afterwards.
