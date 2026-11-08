"use strict";

(() => {
  const byId = id => document.getElementById(id);
  const text = (id, value) => {
    const node = byId(id);
    const next = String(value ?? "—");
    if (node.textContent !== next) node.textContent = next;
  };
  const colors = getComputedStyle(document.documentElement);
  const color = name => colors.getPropertyValue(`--${name}`).trim();
  const ui = JSON.parse(byId("heartbeat-config").textContent);
  const SVG_NS = "http://www.w3.org/2000/svg";
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
  let state = null, socket = null, cursor = 0, connected = false;
  let stateCursor = 0;
  let busy = false, statePromise = null, reconnectTimer = null, pollTimer = null;
  let roverBusy = false, stopGeneration = 0, approvalGap = null;
  const patrolRows = new Map();
  let connectionGeneration = 0;
  let levels = [], sounds = [], prompts = [], windows = [], activity = [];
  let now = 0, plot = null, paintQueued = false, canvasSize = "";
  const canvas = byId("trace"), context = canvas.getContext("2d");
  // Presentation-only state. None of it feeds back into commands or the dialog.
  let backfilling = false, lastStatus = null, streamState = null, streamRound = 0;
  let liveRound = null;
  const exchangeRows = new Map();
  let mapKey = null, mapGeo = null;
  const rover = {x: 240, y: 150, heading: 0, tween: null, segment: null, node: null};
  let flashUntil = 0, loopHandle = 0, lastFrame = 0;

  function seconds(value) {
    return `${Number(value || 0).toFixed(2)}s`;
  }
  function clock(value) {
    const whole = Math.max(0, Math.floor(Number(value || 0)));
    return `${String(Math.floor(whole / 60)).padStart(2, "0")}:${String(whole % 60).padStart(2, "0")}`;
  }
  function svg(tag, attrs = {}, parent = null) {
    const node = document.createElementNS(SVG_NS, tag);
    for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, String(value));
    if (parent) parent.append(node);
    return node;
  }
  function animated() {
    return !reducedMotion.matches && !backfilling && !document.hidden;
  }
  function connection(mode, label) {
    connected = mode === "connected";
    byId("connection").className = `connection ${mode}`;
    text("connection-label", label);
    controls();
    renderMapMode();
  }
  function feedback(message, error = false) {
    text("command-feedback", message);
    byId("command-feedback").classList.toggle("error", error);
  }
  function controls() {
    const running = connected && state && state.pipeline_running !== false && !busy;
    const patrol = state?.patrol;
    const idle = state && ["LISTENING", "NO_RESPONSE", "CONFIRMED"].includes(state.state);
    for (const button of document.querySelectorAll("[data-command]")) {
      const command = button.dataset.command;
      const allowed = command === "reset" ||
        (command === "challenge" && ["LISTENING", "NO_RESPONSE"].includes(state?.state)) ||
        (command === "all_quiet" && !state?.coaching &&
          ["LISTENING", "NO_RESPONSE", "TOO_NOISY", "PRE_LISTEN"].includes(state?.state)) ||
        (command === "interview" && state?.state === "CONFIRMED") ||
        (command === "reassure" && idle);
      button.disabled = !(running && !patrol && allowed);
    }
    byId("approve-button").disabled = !(running && !patrol && state.state === "CONFIRMED" && !state.approved);
    patrolControls();
  }
  function patrolFeedback(message, error = false) {
    text("patrol-feedback", message);
    byId("patrol-feedback").classList.toggle("error", error);
  }
  function patrolControls() {
    const patrol = state?.patrol;
    const running = connected && patrol && state.pipeline_running !== false && !roverBusy && !busy;
    for (const button of document.querySelectorAll("[data-patrol-command]")) {
      const resume = button.dataset.patrolCommand === "resume";
      const allowed = !patrol?.done && !patrol?.estopped && !patrol?.fault && (resume ? patrol?.paused : !patrol?.paused);
      button.disabled = !(running && allowed);
    }
    for (const row of patrolRows.values()) {
      const result = (patrol?.results || []).find(item => item.gap === row.dataset.gap);
      row.querySelector(".gap-approve").disabled = !(running && result?.status === "CONFIRMED" && !result.approved);
    }
    // E-STOP remains usable through another request, a stopped pipeline, or a
    // lost event connection. Its own HTTP request must get a chance to arrive.
    byId("estop-button").disabled = !patrol;
  }
  function patrolPhase(patrol) {
    if (patrol.estopped) return "E-STOP";
    return ({MOVING: "Exploring", STOPPING: "Waiting for STOP acknowledgement", SETTLING: "Settling",
      VERIFYING: "Verifying fresh audio", REVIEW: "Awaiting operator review", FAULT: "Motion fault — check rover",
      LISTENING: "Challenge and response", PAUSED: "Paused", OBSERVING: "Taking an observation",
      DECIDING: "Choosing the next action", HOLDING: "Holding position",
      REPORTING: "Recording result", DRAINING: "Finishing dialog", DONE: "Route complete",
      ABORTED: "Aborted", ESTOP: "E-STOP", IDLE: "Ready"})[patrol.state] || String(patrol.state || "Ready").replaceAll("_", " ");
  }
  function answerSummary(answers) {
    const labels = {q_people: "People", q_move: "Can move", q_hurt: "Hurt"};
    return Object.entries(answers || {}).map(([key, value]) => `${labels[key] || key}: ${value}`).join(" · ") || "—";
  }
  function createPatrolRow(gap) {
    const row = document.createElement("tr");
    row.dataset.gap = gap;
    for (let i = 0; i < 5; i++) row.append(document.createElement("td"));
    const status = document.createElement("span"), note = document.createElement("span");
    status.className = "gap-status"; note.className = "gap-note";
    row.cells[1].append(status, note);
    row.cells[3].className = "gap-answers";
    const approve = document.createElement("button"), approved = document.createElement("span");
    approve.type = "button"; approve.className = "approve-button gap-approve";
    approve.textContent = "Approve dig"; approve.setAttribute("aria-label", `Approve dig at ${gap}`);
    approve.addEventListener("click", () => openApproval(gap));
    approved.className = "gap-approved";
    row.cells[4].append(approve, approved);
    return row;
  }
  function renderPatrol() {
    const patrol = state?.patrol, panel = byId("patrol-panel");
    panel.hidden = !patrol;
    document.body.classList.toggle("patrol-active", !!patrol);
    if (!patrol) return;
    panel.dataset.phase = patrol.estopped ? "ESTOP" : patrol.state;
    text("patrol-state", patrolPhase(patrol));
    text("patrol-gap", patrol.gap);
    text("patrol-motors", motorsLabel(patrol));
    const cue = patrol.verified_cue || patrol.trigger;
    text("patrol-audio", patrol.fault || (cue
      ? `${cue.label} · ${Number(cue.end).toFixed(1)}s · possible person; review evidence`
      : patrol.audio_stop_enabled ? "Audio investigation enabled" : "Scheduled listening stops"));
    const battery = patrol.battery;
    text("patrol-battery", typeof battery === "number" && Number.isFinite(battery) && battery >= 0 ? `${battery.toFixed(2)} V` : "—");
    const decisions = patrol.decisions, decisionLine = byId("patrol-decisions");
    decisionLine.hidden = !decisions;
    if (decisions) {
      const parts = [decisions.armed ? "Guarded movement enabled" : "Observation only · movement disabled"];
      if (decisions.last_action) parts.push(decisions.last_action.replaceAll("_", " "));
      if (typeof decisions.latency_s === "number") parts.push(`Decision ${decisions.latency_s.toFixed(2)}s`);
      if (typeof decisions.front_cm === "number") parts.push(`Front ${decisions.front_cm.toFixed(1)} cm`);
      if (decisions.reason) parts.push(decisions.reason);
      decisionLine.textContent = parts.join(" · ");
    }
    byId("patrol-arrival-warning").hidden = !patrol.arrival_unverified;
    const results = patrol.results || [], route = patrol.route || results.map(result => ({name: result.gap}));
    text("patrol-progress", `${results.length} of ${route.length} gaps recorded`);
    const body = byId("patrol-body"), names = new Set(route.map(gap => gap.name));
    for (const [name, row] of patrolRows) {
      if (!names.has(name)) { row.remove(); patrolRows.delete(name); }
    }
    if (route.length) body.querySelector(".empty-row")?.remove();
    route.forEach((gap, index) => {
      const name = gap.name, result = results.find(item => item.gap === name);
      let row = patrolRows.get(name);
      if (!row) { row = createPatrolRow(name); patrolRows.set(name, row); }
      // Keep existing buttons attached across polling, preserving keyboard focus.
      if (body.children[index] !== row) body.insertBefore(row, body.children[index] || null);
      const current = name === patrol.gap && !patrol.done;
      row.classList.toggle("current-gap", current);
      row.cells[0].textContent = name;
      const status = row.querySelector(".gap-status"), note = row.querySelector(".gap-note");
      status.textContent = result ? result.status.replaceAll("_", " ") : current ? patrolPhase(patrol) : "Pending";
      status.className = `gap-status ${result ? result.status.toLowerCase() : current ? "active" : "pending"}`;
      const notes = [];
      if (result?.arrival_unverified) notes.push("Arrival unverified");
      if (result?.noisy) notes.push("Noise limited the check");
      if (result?.reason === "TIMEOUT") notes.push("Listening timed out");
      if (result?.reason === "UNCONFIRMED") notes.push("No fresh qualifying signal; unconfirmed");
      if (result?.reason === "AUDIO_ERROR") notes.push("Audio verification incomplete");
      note.textContent = notes.join(" · "); note.hidden = !notes.length;
      row.cells[2].textContent = result?.odds || "—";
      row.cells[3].textContent = answerSummary(result?.answers);
      const approve = row.querySelector(".gap-approve"), approved = row.querySelector(".gap-approved");
      approve.hidden = result?.status !== "CONFIRMED" || !!result?.approved;
      approved.hidden = !result?.approved;
      approved.textContent = result?.approved ? `Approved${result.approved_by ? ` by ${result.approved_by}` : ""}` : "";
    });
    patrolControls();
  }
  function motorsLabel(patrol) {
    return patrol.fault ? "Stop unverified" : patrol.state === "STOPPING" ? "Stopping…" : patrol.moving ? "Moving" : "Stopped";
  }
  function phaseLabel(snapshot) {
    if (snapshot.pipeline_running === false) return "Session ended · recording available for review";
    if (snapshot.coaching) return "Hearing knocks: asking them to listen.";
    if (snapshot.state === "PRE_LISTEN") return "Listening for quiet before the challenge…";
    if (snapshot.state === "TOO_NOISY") return "Too noisy: call for quiet.";
    if (snapshot.state === "RESPONSE_WINDOW") return `Round ${snapshot.round}: listening for an answer…`;
    if (snapshot.state === "PROMPTING") return `Round ${snapshot.round || "—"}: sending the prompt…`;
    if (snapshot.state === "INTERVIEW") return "Interview: listening for knock answers…";
    if (snapshot.state === "NO_RESPONSE") return "No response to this challenge · listening resumes shortly";
    if (snapshot.state === "CONFIRMED") return "A response followed the changing knock patterns.";
    return snapshot.status === "POSSIBLE" ? "Knocks heard · ready to test a reply" : "Listening for a possible reply.";
  }
  function renderState() {
    if (!state) return;
    text("sentinel-name", state.sentinel_name);
    text("profile-name", state.profile);
    text("run-id", state.run_id);
    text("uptime", clock(now));
    const status = state.status || "NO_SIGNS", tone = status.toLowerCase();
    byId("status-card").className = `status-card ${tone}`;
    for (const name of ["no_signs", "possible", "confirmed"]) document.body.classList.toggle(`status-${name}`, name === tone);
    if (lastStatus !== null && lastStatus !== status) {
      const title = byId("status-title");
      title.classList.remove("swap"); void title.offsetWidth; title.classList.add("swap");
      if (status === "CONFIRMED") celebrate();
    }
    lastStatus = status;
    text("status-title", status.replaceAll("_", " "));
    text("phase", phaseLabel(state));
    text("odds", state.odds);
    renderSteps(state);
    const badge = byId("approval-badge");
    badge.hidden = !state.approved;
    if (state.approved) badge.textContent = state.approved_by ? `Approved by ${state.approved_by}` : "Operator approved";

    for (const [key, id] of [["q_people", "answer-people"], ["q_move", "answer-move"], ["q_hurt", "answer-hurt"]]) {
      const value = state.answers?.[key];
      text(id, value === undefined ? "Awaiting reply" : value);
      byId(id).className = value === undefined ? "unanswered" : value === "unclear" || value === "no answer" ? "unclear" : "";
    }
    renderRounds(state.rounds || []);
    renderExchange();
    const box = state.blackbox;
    if (box) {
      const failed = box.integrity_valid === false || box.error;
      byId("blackbox-status").classList.toggle("error", !!failed);
      text("blackbox-status", failed
        ? `Black box: recording error${box.error ? ` — ${box.error}` : ""}`
        : `Black box: ${Number(box.events || 0).toLocaleString()} events, hash-chained.`);
    }
    renderPatrol();
    renderMap();
    controls();
  }
  function renderSteps(snapshot) {
    const order = ["listen", "ask", "answer", "confirmed"];
    let current = "listen";
    if (snapshot.status === "CONFIRMED" || ["CONFIRMED", "INTERVIEW"].includes(snapshot.state)) current = "confirmed";
    else if (snapshot.state === "PROMPTING") current = "ask";
    else if (snapshot.state === "RESPONSE_WINDOW") current = "answer";
    const index = order.indexOf(current);
    for (const step of byId("story-steps").children) {
      const position = order.indexOf(step.dataset.step);
      step.classList.toggle("done", position < index);
      step.classList.toggle("current", position === index);
    }
  }
  function celebrate() {
    if (!animated()) return;
    const burst = byId("hero-burst");
    burst.classList.remove("go"); void burst.offsetWidth; burst.classList.add("go");
    for (let i = 0; i < 3; i++) pulse("answer", i * 380);
  }
  function renderRounds(rounds) {
    const body = byId("rounds-body");
    text("round-count", rounds.length ? `${rounds.length} completed` : "No rounds yet");
    if (!rounds.length) {
      if (!body.querySelector(".empty-row")) {
        const row = document.createElement("tr"), cell = document.createElement("td");
        row.className = "empty-row";
        cell.colSpan = 5;
        cell.textContent = "Challenge a possible reply to test whether it answers.";
        row.append(cell); body.replaceChildren(row);
      }
      return;
    }
    const rows = rounds.map(round => {
      const row = document.createElement("tr");
      const p = round.p === null || round.p === undefined ? "—" : Number(round.p).toExponential(2);
      for (const value of [round.round, round.expected, round.heard, p, round.matched ? "YES" : "NO"]) {
        const cell = document.createElement("td");
        cell.textContent = String(value); row.append(cell);
      }
      row.lastElementChild.className = round.matched ? "match" : "missed";
      return row;
    });
    body.replaceChildren(...rows);
  }

  // Call and response: one row per round, our knocks above what came back.
  const MAX_DOTS = 12;
  function setDots(box, filled, total) {
    filled = Math.min(filled, MAX_DOTS); total = Math.min(Math.max(total, filled), MAX_DOTS);
    let solid = box.querySelectorAll(".dot:not(.slot)").length;
    if (solid > filled) { box.replaceChildren(); solid = 0; }
    for (const slot of box.querySelectorAll(".slot")) slot.remove();
    for (let i = solid; i < filled; i++) {
      const dot = document.createElement("span");
      dot.className = "dot";
      if (!animated()) dot.style.animation = "none";
      box.append(dot);
    }
    for (let i = filled; i < total; i++) {
      const slot = document.createElement("span");
      slot.className = "dot slot"; box.append(slot);
    }
  }
  function exchangeRow(key) {
    let row = exchangeRows.get(key);
    if (row) return row;
    row = document.createElement("li");
    const tag = document.createElement("span"); tag.className = "round-tag";
    const lane = kind => {
      const box = document.createElement("div"), label = document.createElement("span");
      const dots = document.createElement("span"), count = document.createElement("span");
      box.className = `lane ${kind}`; label.className = "lane-label"; dots.className = "dots"; count.className = "lane-count";
      label.textContent = kind === "ask" ? "We knocked" : "Heard";
      box.append(label, dots, count);
      return box;
    };
    const verdict = document.createElement("div"), chip = document.createElement("span"), odds = document.createElement("div");
    verdict.className = "verdict"; chip.className = "verdict-chip"; odds.className = "verdict-odds";
    verdict.append(chip, odds);
    row.append(tag, lane("ask"), lane("reply"), verdict);
    if (!animated()) row.style.animation = "none";
    exchangeRows.set(key, row);
    return row;
  }
  function renderExchange() {
    const rounds = (state?.rounds || []).slice(-6);
    const items = rounds.map(round => ({...round, key: `r${round.round}`}));
    if (liveRound && !rounds.some(round => round.round === liveRound.round)) {
      items.push({...liveRound, key: `r${liveRound.round}`, live: true});
    }
    const list = byId("exchange");
    text("exchange-count", items.length ? `${rounds.length} ROUND${rounds.length === 1 ? "" : "S"}${liveRound ? " · LIVE" : ""}` : "NO ROUNDS YET");
    if (!items.length) {
      exchangeRows.clear();
      if (!list.querySelector(".exchange-empty")) {
        const empty = document.createElement("li");
        empty.className = "exchange-empty";
        empty.textContent = "Waiting for the first challenge.";
        list.replaceChildren(empty);
      }
      return;
    }
    const keep = new Set(items.map(item => item.key));
    for (const [key, row] of exchangeRows) if (!keep.has(key)) { row.remove(); exchangeRows.delete(key); }
    list.querySelector(".exchange-empty")?.remove();
    items.forEach((item, index) => {
      const row = exchangeRow(item.key);
      if (list.children[index] !== row) list.insertBefore(row, list.children[index] || null);
      const verdictClass = item.live ? "live" : item.matched ? "matched" : "missed";
      row.className = `exchange-row ${verdictClass}`;
      row.querySelector(".round-tag").textContent = `R${item.round ?? "—"}`;
      const [ask, reply] = row.querySelectorAll(".lane");
      const expected = Number(item.expected || 0);
      if (item.live) {
        setDots(ask.querySelector(".dots"), item.struck, expected);
        ask.querySelector(".lane-count").textContent = item.phase === "asking" ? "" : `×${expected}`;
        const listening = item.phase === "listening";
        setDots(reply.querySelector(".dots"), listening ? item.heard : 0, listening ? expected : 0);
        reply.querySelector(".lane-count").textContent = listening ? (item.heard > MAX_DOTS ? `×${item.heard}` : "") : "—";
        row.querySelector(".verdict-chip").textContent = listening ? "Listening" : "Knocking";
        row.querySelector(".verdict-odds").textContent = listening ? "for a copy" : `${expected} knock${expected === 1 ? "" : "s"}`;
      } else {
        const heard = Number(item.heard || 0);
        setDots(ask.querySelector(".dots"), expected, expected);
        ask.querySelector(".lane-count").textContent = `×${expected}`;
        setDots(reply.querySelector(".dots"), heard, heard);
        reply.querySelector(".lane-count").textContent = `×${heard}`;
        row.querySelector(".verdict-chip").textContent = item.matched ? "✓ Copied" : item.noisy ? "Too noisy" : "✗ No match";
        const p = item.p === null || item.p === undefined ? null : Number(item.p);
        row.querySelector(".verdict-odds").textContent = item.matched && item.odds ? `Random: ${item.odds}` : p !== null ? `p ${p.toExponential(1)}` : "not counted";
      }
    });
  }
  function strike(round) {
    if (liveRound !== round || round.struck >= round.expected) return;
    round.struck += 1;
    renderExchange();
  }

  // Search map: a schematic built only from data the sentinel reports.
  const MAP_W = 480, MAP_H = 300;
  function routeGeometry(route) {
    // Heading is clockwise from "up"; a positive TURN is drawn as a left turn.
    let x = 0, y = 0, heading = 0;
    const raw = [{x, y, heading}], gaps = [];
    for (const gap of route) {
      for (const move of gap.moves || []) {
        const [kind, amount] = move, value = Number(amount) || 0;
        if (kind === "turn") heading -= value;
        else if (kind === "drive") {
          const radians = heading * Math.PI / 180;
          x += value * Math.sin(radians); y -= value * Math.cos(radians);
          raw.push({x, y, heading});
        }
      }
      gaps.push({name: gap.name, point: raw.length - 1, heading});
    }
    const xs = raw.map(p => p.x), ys = raw.map(p => p.y);
    const minX = Math.min(...xs), maxX = Math.max(...xs), minY = Math.min(...ys), maxY = Math.max(...ys);
    const pad = 58, scale = Math.min((MAP_W - 2 * pad) / Math.max(maxX - minX, 1), (MAP_H - 2 * pad) / Math.max(maxY - minY, 1), 8);
    const cx = (minX + maxX) / 2, cy = (minY + maxY) / 2;
    const points = raw.map(p => ({x: MAP_W / 2 + (p.x - cx) * scale, y: MAP_H / 2 + (p.y - cy) * scale, heading: p.heading}));
    return {points, gaps: gaps.map(gap => ({...gap, ...points[gap.point], heading: gap.heading})), scale};
  }
  function mapMode() {
    const patrol = state?.patrol;
    if (!patrol) return "station";
    return patrol.route?.length ? "route" : "free";
  }
  function focusPoint() {
    return mapMode() === "station" ? {x: MAP_W / 2, y: MAP_H / 2} : {x: rover.x, y: rover.y};
  }
  function scaleBar(scale) {
    const layer = byId("map-scale");
    layer.replaceChildren();
    if (!scale) return;
    const cm = [5, 10, 20, 25, 50, 100, 200].find(value => value * scale >= 50) || 200;
    const length = cm * scale, x = MAP_W - 18 - length, y = MAP_H - 18;
    svg("path", {d: `M${x} ${y - 4}V${y}H${x + length}V${y - 4}`, class: "scale-bar", fill: "none"}, layer);
    svg("text", {x: x + length, y: y - 8, "text-anchor": "end", class: "map-sublabel"}, layer).textContent = `${cm} cm`;
  }
  function buildRover(parent) {
    const group = svg("g", {class: "rover", id: "rover-marker"}, parent);
    svg("rect", {x: -12, y: -11, width: 4, height: 22, rx: 1.5, class: "rover-track"}, group);
    svg("rect", {x: 8, y: -11, width: 4, height: 22, rx: 1.5, class: "rover-track"}, group);
    svg("rect", {x: -8, y: -12, width: 16, height: 24, rx: 4, class: "rover-body"}, group);
    svg("path", {d: "M-5 -9 L0 -16 L5 -9 Z", class: "rover-nose"}, group);
    const question = svg("text", {x: 0, y: 4, "text-anchor": "middle", class: "rover-question"}, group);
    question.textContent = "?";
    rover.node = group;
    placeRover();
  }
  function placeRover() {
    if (!rover.node) return;
    rover.node.setAttribute("transform", `translate(${rover.x.toFixed(1)} ${rover.y.toFixed(1)}) rotate(${rover.heading.toFixed(1)})`);
  }
  function buildMap(mode) {
    const route = byId("map-route"), nodes = byId("map-nodes"), sweep = byId("map-sweep");
    route.replaceChildren(); nodes.replaceChildren(); byId("map-pulses").replaceChildren();
    rover.node = null; rover.tween = null; rover.segment = null; mapGeo = null;
    sweep.classList.toggle("idle", mode !== "station");
    if (mode === "station") {
      sweep.setAttribute("transform", `translate(${MAP_W / 2} ${MAP_H / 2})`);
      const group = svg("g", {transform: `translate(${MAP_W / 2} ${MAP_H / 2})`}, nodes);
      svg("circle", {r: 14, class: "listen-ring"}, group);
      svg("circle", {r: 14, class: "listen-ring late"}, group);
      svg("circle", {r: 13, class: "sentinel-shell"}, group);
      svg("circle", {r: 6, class: "sentinel-core"}, group);
      svg("text", {y: 34, "text-anchor": "middle", class: "map-label", id: "map-node-label"}, group).textContent = "Sentinel";
      svg("text", {y: 47, "text-anchor": "middle", class: "map-sublabel", id: "map-node-sublabel"}, group).textContent = "CONTACT MIC + KNOCKER";
      scaleBar(0);
      return;
    }
    if (mode === "free") {
      rover.x = MAP_W / 2; rover.y = MAP_H - 70; rover.heading = 0;
      svg("line", {id: "front-ray", class: "front-ray", x1: rover.x, y1: rover.y - 16, x2: rover.x, y2: rover.y - 16}, route);
      svg("line", {id: "front-wall", class: "front-wall", x1: rover.x - 26, x2: rover.x + 26, y1: 0, y2: 0, visibility: "hidden"}, route);
      svg("text", {id: "front-label", x: rover.x + 32, y: 0, class: "map-sublabel", visibility: "hidden"}, route);
      buildRover(nodes);
      scaleBar(1.1);
      return;
    }
    const geo = mapGeo = routeGeometry(state.patrol.route);
    const line = points => points.map((p, i) => `${i ? "L" : "M"}${p.x.toFixed(1)} ${p.y.toFixed(1)}`).join(" ");
    svg("path", {d: line(geo.points), class: "route-planned"}, route);
    svg("path", {id: "route-done-glow", class: "route-done-glow", d: ""}, route);
    svg("path", {id: "route-done", class: "route-done", d: ""}, route);
    const start = svg("g", {class: "start-node", transform: `translate(${geo.points[0].x} ${geo.points[0].y})`}, nodes);
    svg("rect", {x: -6, y: -6, width: 12, height: 12, rx: 2}, start);
    svg("text", {y: 22, "text-anchor": "middle", class: "map-sublabel"}, start).textContent = "START";
    geo.gaps.forEach((gap, index) => {
      const group = svg("g", {class: "gap-node pending", transform: `translate(${gap.x.toFixed(1)} ${gap.y.toFixed(1)})`, "data-index": index}, nodes);
      svg("circle", {r: 13, class: "gap-ring"}, group);
      svg("circle", {r: 8, class: "gap-dot"}, group);
      const label = svg("text", {x: 0, y: -21, "text-anchor": "middle", class: "map-label"}, group);
      label.textContent = gap.name;
    });
    rover.x = geo.points[0].x; rover.y = geo.points[0].y; rover.heading = geo.points[0].heading;
    buildRover(nodes);
    scaleBar(geo.scale);
  }
  function gapClass(gap, index, patrol) {
    const result = (patrol.results || []).find(item => item.gap === gap.name);
    if (result) return {tone: String(result.status || "no_signs").toLowerCase(), approved: !!result.approved};
    // The hero shows the live dialog; a gap is coloured only by its own recorded result.
    if (gap.name === patrol.gap && !patrol.done && patrol.index === index) return {tone: "current", approved: false};
    return {tone: "pending", approved: false};
  }
  function decorateGap(group, cls) {
    group.setAttribute("class", `gap-node ${cls.tone}${cls.approved ? " approved" : ""}`);
    const confirmed = cls.tone.includes("confirmed");
    const beacons = group.querySelectorAll(".beacon-ring");
    if (confirmed && !cls.approved && !beacons.length) {
      svg("circle", {r: 12, class: "beacon-ring"}, group);
      svg("circle", {r: 12, class: "beacon-ring late"}, group);
    } else if ((!confirmed || cls.approved) && beacons.length) beacons.forEach(ring => ring.remove());
    const focus = group.querySelector(".current-ring");
    if (cls.tone === "current" && !focus) svg("circle", {r: 13, class: "focus-ring current-ring"}, group);
    else if (cls.tone !== "current" && focus) focus.remove();
    const check = group.querySelector(".gap-check");
    if (cls.approved && !check) svg("path", {d: "M-4 0 L-1 3.5 L4.5 -3", class: "gap-check"}, group);
    else if (!cls.approved && check) check.remove();
  }
  function polyline(points) {
    const lengths = [0];
    for (let i = 1; i < points.length; i++) lengths.push(lengths[i - 1] + Math.hypot(points[i].x - points[i - 1].x, points[i].y - points[i - 1].y));
    return {points, lengths, total: lengths[lengths.length - 1]};
  }
  function startTween(points, seconds) {
    const path = polyline(points);
    if (!animated() || path.total < 1) {
      const end = points[points.length - 1];
      rover.x = end.x; rover.y = end.y; rover.heading = end.heading; rover.tween = null; placeRover();
      return;
    }
    rover.tween = {...path, start: performance.now(), duration: seconds * 1000};
    wake(0);
  }
  function stepRover(time) {
    const tween = rover.tween;
    if (!tween) return;
    const k = Math.min(1, (time - tween.start) / tween.duration);
    const eased = k < .5 ? 2 * k * k : 1 - Math.pow(-2 * k + 2, 2) / 2;
    const distance = eased * tween.total;
    let i = 1;
    while (i < tween.points.length - 1 && tween.lengths[i] < distance) i++;
    const a = tween.points[i - 1], b = tween.points[i];
    const span = tween.lengths[i] - tween.lengths[i - 1] || 1, local = (distance - tween.lengths[i - 1]) / span;
    rover.x = a.x + (b.x - a.x) * local; rover.y = a.y + (b.y - a.y) * local;
    rover.heading = Math.atan2(b.x - a.x, -(b.y - a.y)) * 180 / Math.PI;
    placeRover();
    if (k >= 1) rover.tween = null;
  }
  function updateRoute(patrol) {
    const geo = mapGeo;
    if (!geo) return;
    const index = Number.isInteger(patrol.index) ? patrol.index : -1;
    byId("map-nodes").querySelectorAll(".gap-node").forEach(group => {
      const i = Number(group.dataset.index);
      decorateGap(group, gapClass(geo.gaps[i], i, patrol));
    });
    const unverified = !!(patrol.arrival_unverified || patrol.interrupted_move);
    const reached = patrol.moving || unverified ? index - 1 : patrol.done ? geo.gaps.length - 1 : index;
    const doneUntil = reached >= 0 && geo.gaps[reached] ? geo.gaps[reached].point : 0;
    const done = geo.points.slice(0, doneUntil + 1).map((p, i) => `${i ? "L" : "M"}${p.x.toFixed(1)} ${p.y.toFixed(1)}`).join(" ");
    byId("route-done").setAttribute("d", doneUntil ? done : "");
    byId("route-done-glow").setAttribute("d", doneUntil ? done : "");
    const target = index >= 0 ? geo.gaps[index] : null;
    rover.node?.classList.toggle("unverified", unverified);
    rover.node?.classList.toggle("estop", !!patrol.estopped);
    rover.node?.querySelector(".rover-question")?.setAttribute("visibility", unverified ? "visible" : "hidden");
    if (!target) return;
    const from = index > 0 ? geo.gaps[index - 1].point : 0;
    if (patrol.moving) {
      if (rover.segment !== index) {
        rover.segment = index;
        const points = geo.points.slice(from, target.point + 1);
        const centimetres = polyline(points).total / geo.scale;
        startTween(points, Math.max(1.6, Math.min(8, centimetres / 12)));
      }
      return;
    }
    if (unverified && rover.segment !== `mid-${index}`) {
      // Travel was interrupted, so the stop point is unknown: mark it partway
      // along the leg rather than at either end or at an estimated position.
      const leg = polyline(geo.points.slice(from, target.point + 1)), half = leg.total / 2;
      let i = 1;
      while (i < leg.points.length - 1 && leg.lengths[i] < half) i++;
      const a = leg.points[i - 1], b = leg.points[i], local = (half - leg.lengths[i - 1]) / ((leg.lengths[i] - leg.lengths[i - 1]) || 1);
      rover.x = a.x + (b.x - a.x) * local; rover.y = a.y + (b.y - a.y) * local; rover.heading = b.heading;
      rover.segment = `mid-${index}`; rover.tween = null; placeRover();
      return;
    }
    if (unverified || patrol.state === "STOPPING") { rover.tween = null; return; }
    if (rover.segment !== `at-${index}`) {
      rover.segment = `at-${index}`;
      startTween([{x: rover.x, y: rover.y, heading: rover.heading}, {x: target.x, y: target.y, heading: target.heading}], .35);
    }
  }
  function updateFree(patrol) {
    const front = patrol.decisions?.front_cm;
    const ray = byId("front-ray"), wall = byId("front-wall"), label = byId("front-label");
    if (!ray) return;
    rover.node?.classList.toggle("estop", !!patrol.estopped);
    const known = typeof front === "number" && Number.isFinite(front) && front > 0;
    const reach = known ? Math.min(front * 1.1, rover.y - 30) : 0;
    const y = rover.y - 16 - reach;
    ray.setAttribute("y2", known ? y : rover.y - 16);
    for (const node of [wall, label]) node.setAttribute("visibility", known ? "visible" : "hidden");
    if (known) {
      wall.setAttribute("y1", y); wall.setAttribute("y2", y);
      label.setAttribute("y", y + 4);
      label.textContent = `${front.toFixed(0)} cm${front * 1.1 > reach + .5 ? " (clipped)" : ""}`;
    }
  }
  function renderMapMode() {
    const label = !connected ? "OFFLINE" : state?.pipeline_running === false ? "ENDED" : "LIVE";
    text("map-mode", label);
  }
  function renderMap() {
    if (!state) return;
    const mode = mapMode(), patrol = state.patrol;
    const key = mode === "route" ? `route:${JSON.stringify(patrol.route)}` : mode;
    if (key !== mapKey) { mapKey = key; buildMap(mode); }
    renderMapMode();
    const chips = byId("map-chips");
    if (mode === "station") {
      text("map-subtitle", `Stationary sentinel${state.sentinel_name ? ` · ${state.sentinel_name}` : ""}`);
      text("map-note", "Schematic · a single microphone gives no direction");
      chips.hidden = true;
      const sub = byId("map-node-sublabel");
      if (sub && state.sentinel_name) sub.textContent = String(state.sentinel_name).toUpperCase();
      return;
    }
    const results = patrol.results || [], route = patrol.route || [];
    if (mode === "route") {
      text("map-subtitle", `Route patrol · ${results.length} of ${route.length} gaps recorded`);
      text("map-note", "Route schematic · from planned moves, not GPS");
      updateRoute(patrol);
    } else {
      const decisions = patrol.decisions;
      text("map-subtitle", decisions ? `Camera decisions · ${decisions.armed ? "guarded movement" : "observation only"}${typeof decisions.steps === "number" ? ` · ${decisions.steps} steps` : ""}` : "Ultrasonic exploration");
      text("map-note", "Schematic · rover-relative, not GPS");
      updateFree(patrol);
    }
    const phase = patrol.estopped ? "stop" : ["FAULT", "ABORTED"].includes(patrol.state) ? "stop"
      : ["REVIEW", "PAUSED"].includes(patrol.state) ? "alert" : patrol.moving ? "moving" : "";
    const parts = [[patrolPhase(patrol), phase], [motorsLabel(patrol), patrol.moving ? "moving" : ""]];
    if (mode === "route" && Number.isInteger(patrol.index) && patrol.index >= 0) parts.push([`Gap ${Math.min(patrol.index + 1, route.length)} / ${route.length}`, ""]);
    if (typeof patrol.battery === "number" && patrol.battery >= 0) parts.push([`${patrol.battery.toFixed(1)} V`, ""]);
    chips.hidden = false;
    chips.replaceChildren(...parts.map(([label, kind]) => {
      const chip = document.createElement("span");
      chip.className = `map-chip ${kind}`; chip.textContent = label;
      return chip;
    }));
  }
  function pulse(kind, delay = 0) {
    if (!animated()) return;
    const fire = () => {
      const layer = byId("map-pulses");
      if (layer.childElementCount > 24 || document.hidden) return;
      const {x, y} = focusPoint();
      const ring = svg("circle", {cx: x.toFixed(1), cy: y.toFixed(1), r: 10, class: `pulse pulse-${kind}`}, layer);
      ring.addEventListener("animationend", () => ring.remove());
      setTimeout(() => ring.remove(), 3000);
    };
    if (delay) setTimeout(fire, delay); else fire();
  }

  function eventText(event) {
    const d = event.data || {};
    switch (event.type) {
      case "audio_observation":
        return `${d.label} · score ${Number(d.score || 0).toFixed(2)}${d.alert ? " · Possible distress — review" : ""}${d.transcript ? ` · Heard: ${d.transcript}` : ""}${d.matched_phrases?.length ? ` · HELP_PHRASE: ${d.matched_phrases.join(", ")}` : ""}`;
      case "audio_monitor_status": return d.message || "Audio monitor status";
      case "status": return d.message || d.code || "Status update";
      case "patrol_cue": return `${d.phase === "trigger" ? "Stopping to investigate" : "Fresh stationary evidence"}: ${d.label}`;
      case "sound": {
        const top = d.top?.[0]?.[0];
        return `${d.gated ? "Our prompt / gated sound" : d.candidate ? "Candidate knock" : d.is_knock ? "Knock" : "Other sound"} · score ${Number(d.score || 0).toFixed(2)}${top ? ` · ${top}` : ""}`;
      }
      case "state": return `${String(d.state || "").replaceAll("_", " ")} · ${String(d.status || "").replaceAll("_", " ")}`;
      case "prompt_start": return "Sending prompt" + (d.actions ? ` · ${d.actions.map(a => a.key?.replaceAll("_", " ") || `${a.n} knocks`).join(", ")}` : "");
      case "prompt_end": return d.interrupted ? "Prompt interrupted" : "Prompt finished";
      case "window_open": return `Answer window opened${d.question ? ` · ${d.question.replace("q_", "")}` : ` · round ${d.round}`}`;
      case "round_result": return `Round ${d.round}: asked ${d.expected}, heard ${d.heard} · ${d.matched ? "matched" : "did not match"} · ${d.odds}`;
      case "answer": return `${({q_people: "People", q_move: "Can move", q_hurt: "Hurt"})[d.question] || d.question}: ${d.value}`;
      case "approval": return `Approved${d.gap ? ` ${d.gap}` : ""}${d.operator ? ` by ${d.operator}` : " by operator"}`;
      case "patrol": return `Patrol: ${patrolPhase(d)}${d.gap ? ` · ${d.gap}` : ""}`;
      case "gap_result": return `${d.gap}: ${String(d.status || "").replaceAll("_", " ")} · ${d.odds || "—"}${d.arrival_unverified ? " · arrival unverified" : ""}`;
      case "patrol_done": return d.aborted ? "Patrol stopped" : "Patrol route complete";
      case "operator": return d.message || (d.command === "all_quiet"
        ? `All quiet declared${d.operator ? ` by ${d.operator}` : ""}`
        : `Operator: ${d.command || d.action || "command"}`);
      case "error": return d.message || "An operation failed";
      case "session_start": return "Session started";
      case "session_end": return "Session ended";
      default: return event.type.replaceAll("_", " ");
    }
  }
  function eventKind(event) {
    const d = event.data || {};
    switch (event.type) {
      case "sound": return d.is_knock && !d.gated ? ["kind-knock", "●"] : ["kind-sound", "·"];
      case "prompt_start": case "prompt_end": return ["kind-prompt", "◆"];
      case "window_open": return ["kind-window", "◇"];
      case "round_result": return d.matched ? ["kind-round-ok", "✓"] : ["kind-round-miss", "✗"];
      case "answer": return ["kind-answer", "❯"];
      case "approval": return ["kind-approval", "✔"];
      case "audio_observation": return d.alert ? ["kind-distress", "!"] : ["kind-sound", "~"];
      case "state": return d.status === "CONFIRMED" ? ["kind-confirmed", "♥"] : ["kind-state", "↻"];
      case "patrol": case "gap_result": case "patrol_done": case "patrol_cue": return ["kind-patrol", "▲"];
      case "error": return ["kind-error", "!"];
      case "operator": return ["kind-operator", "›"];
      default: return ["kind-status", "·"];
    }
  }
  function renderLog() {
    const log = byId("event-log"), toggle = byId("log-toggle");
    if (!activity.length) {
      text("event-count", "WAITING FOR EVENTS");
      const row = document.createElement("li");
      row.className = "empty-log";
      row.textContent = "The sentinel’s activity will appear here.";
      log.replaceChildren(row);
      toggle.hidden = true;
      return;
    }
    text("event-count", `${activity.length} RECENT EVENTS`);
    toggle.hidden = activity.length <= 8;
    const expanded = log.classList.contains("expanded");
    toggle.textContent = expanded ? "Show fewer" : `Show all ${activity.length}`;
    log.replaceChildren(...activity.slice().reverse().map(event => {
      const row = document.createElement("li"), at = document.createElement("time");
      const icon = document.createElement("span"), detail = document.createElement("span");
      const [kind, glyph] = eventKind(event);
      at.textContent = seconds(event.t);
      icon.className = "ev-icon"; icon.textContent = glyph; icon.setAttribute("aria-hidden", "true");
      detail.textContent = eventText(event);
      const emphasis = event.type === "error" ? "warning" : ["answer", "approval", "round_result"].includes(event.type) ? "important" : "";
      row.className = `${kind} ${emphasis}`.trim();
      row.append(at, icon, detail);
      return row;
    }));
  }
  byId("log-toggle").addEventListener("click", () => {
    const log = byId("event-log"), expanded = !log.classList.contains("expanded");
    log.classList.toggle("expanded", expanded);
    byId("log-toggle").setAttribute("aria-expanded", String(expanded));
    renderLog();
  });
  function markCandidates(snapshot) {
    const times = new Set(snapshot.candidate_times || []);
    let changed = false;
    for (const event of [...sounds, ...activity]) {
      if (event.type === "sound" && event.data?.is_knock && !event.data.gated &&
          !event.data.candidate && times.has(event.t)) {
        event.data = {...event.data, candidate: true};
        changed = true;
      }
    }
    if (changed) renderLog();
  }
  function trackExchange(event) {
    const d = event.data || {};
    if (event.type === "state") {
      streamState = d.state; streamRound = Number(d.round || streamRound || 0);
      if (liveRound && !["PROMPTING", "RESPONSE_WINDOW"].includes(d.state)) liveRound = null;
    }
    if (event.type === "prompt_start") {
      const knocks = (d.actions || []).find(action => Number.isFinite(Number(action.n)) && action.n > 0);
      if (knocks) {
        const n = Math.round(Number(knocks.n)), interval = Math.max(.05, Number(knocks.interval_s) || .45);
        if (streamState === "PROMPTING") {
          const round = liveRound = {round: streamRound, expected: n, struck: 0, heard: 0, phase: "asking", start: null, end: null};
          if (animated()) for (let i = 0; i < n; i++) setTimeout(() => strike(round), i * interval * 1000);
          else round.struck = n;
        }
        for (let i = 0; i < n; i++) pulse("prompt", i * interval * 1000);
      } else pulse("prompt");
    }
    if (event.type === "window_open" && d.kind === "round") {
      if (!liveRound || liveRound.round !== d.round) liveRound = {round: d.round, heard: 0};
      Object.assign(liveRound, {expected: Number(d.expected || 0), struck: Number(d.expected || 0),
        phase: "listening", start: Number(d.start ?? event.t), end: Number(d.end ?? Infinity)});
    }
    if (event.type === "sound") {
      const knock = d.is_knock && !d.gated;
      const inWindow = knock && liveRound?.phase === "listening" && event.t >= liveRound.start && event.t <= liveRound.end;
      if (inWindow) liveRound.heard += 1;
      if (!d.gated) pulse(inWindow ? "answer" : knock ? "knock" : "other");
      if (knock) { event.arrived = performance.now(); wake(900); }
    }
    if (event.type === "audio_observation" && d.alert) pulse("distress");
    if (event.type === "round_result") {
      liveRound = null;
      if (state && event.seq > stateCursor) state.rounds = [...(state.rounds || []).filter(round => round.round !== d.round), d];
    }
    if (["state", "prompt_start", "window_open", "sound", "round_result"].includes(event.type)) renderExchange();
  }
  function ingest(event) {
    if (!event || typeof event.seq !== "number" || event.seq <= cursor) return;
    if (cursor && event.seq > cursor + 1) refreshState().catch(() => {});
    cursor = event.seq;
    now = Math.max(now, Number(event.t || 0));
    const d = event.data || {};
    if (["clap_degraded", "clap_retrying"].includes(d.code)) {
      text("clap-banner", `CLAP offline: using ${d.fallback || "heuristic"} (retrying)`);
      byId("clap-banner").hidden = false;
    } else if (d.code === "clap_restored" || ["session_start", "session_end"].includes(event.type)) {
      byId("clap-banner").hidden = true;
    }
    if (event.type === "level") {
      levels.push({t: event.t, level: d.dbfs, floor: d.floor_db});
      text("meter-value", `${Number(d.dbfs).toFixed(1)} dBFS`);
    } else {
      activity.push(event);
      if (ui.event_log_limit) activity = activity.slice(-ui.event_log_limit);
      renderLog();
    }
    if (event.type === "sound") {
      sounds.push(event);
      if (state) markCandidates(state);
    }
    if (event.type === "prompt_start") prompts.push({start: event.t, end: null, batch: d.batch});
    if (event.type === "prompt_end") {
      const prompt = prompts.slice().reverse().find(item => item.batch === d.batch && item.end === null);
      if (prompt) prompt.end = event.t;
    }
    if (event.type === "window_open") windows.push({...d, start: d.start ?? event.t, closed: false});
    if (event.type === "round_result" || event.type === "answer") {
      const window = windows.slice().reverse().find(item => !item.closed &&
        (event.type === "round_result" ? item.kind === "round" && item.round === d.round : item.question === d.question));
      if (window) { window.end = event.t; window.closed = true; }
    }
    trackExchange(event);
    // Historical candidate references may label trace/log sounds even when
    // their older state must not replace the current authoritative snapshot.
    if (event.type === "state") markCandidates(d);
    if (event.type === "state" && event.seq > stateCursor) {
      state = {...state, ...d};
      if (!d.approved) state.approved_by = null;
      renderState();
    }
    if (event.type === "answer" && state && event.seq > stateCursor) {
      state.answers = {...state.answers, [d.question]: d.value}; renderState();
    }
    if (event.type === "approval" && state && event.seq > stateCursor) {
      if (d.gap && state.patrol) {
        state.patrol.results = (state.patrol.results || []).map(result => result.gap === d.gap
          ? {...result, approved: true, approved_by: d.operator} : result);
      } else if (!d.gap) { state.approved = true; state.approved_by = d.operator; }
      renderState();
    }
    if (["patrol", "gap_result", "patrol_done"].includes(event.type) && state && event.seq > stateCursor) {
      const patrol = state.patrol || {};
      if (event.type === "patrol") state.patrol = {...patrol, ...d};
      if (event.type === "gap_result") {
        const results = (patrol.results || []).filter(result => result.gap !== d.gap);
        state.patrol = {...patrol, results: [...results, d]};
      }
      if (event.type === "patrol_done") state.patrol = {...patrol, state: d.state, done: true,
        moving: false, settling: false, listening: false, results: d.gaps || patrol.results || []};
      renderState();
    }
    if (event.type === "session_end") refreshState().catch(() => {});
    text("uptime", clock(now));
    prune(); requestPaint();
  }
  function prune() {
    if (!ui.trace_seconds) return;
    const cutoff = now - ui.trace_seconds;
    // Retain one older point so the line enters the left plot edge naturally.
    const first = levels.findIndex(item => item.t >= cutoff);
    if (first > 1) levels = levels.slice(first - 1);
    sounds = sounds.filter(event => event.t >= cutoff);
    prompts = prompts.filter(item => (item.end ?? now) >= cutoff);
    windows = windows.filter(item => item.end >= cutoff);
  }
  function requestPaint() {
    if (paintQueued) return;
    paintQueued = true;
    requestAnimationFrame(() => { paintQueued = false; draw(); });
  }
  // One shared animation loop, capped near 30 fps; browsers pause it in hidden tabs.
  function wake(ms) {
    if (reducedMotion.matches) return;
    flashUntil = Math.max(flashUntil, performance.now() + ms);
    if (!loopHandle) loopHandle = requestAnimationFrame(frame);
  }
  function frame(time) {
    loopHandle = 0;
    if (time - lastFrame >= 32) { lastFrame = time; draw(); stepRover(time); }
    if (time < flashUntil || rover.tween) loopHandle = requestAnimationFrame(frame);
  }
  function draw() {
    if (!context || !ui.trace_seconds) return;
    const width = canvas.clientWidth, height = canvas.clientHeight, scale = window.devicePixelRatio || 1;
    const size = `${width}x${height}x${scale}`;
    if (size !== canvasSize) {
      canvas.width = Math.round(width * scale); canvas.height = Math.round(height * scale); canvasSize = size;
    }
    context.setTransform(scale, 0, 0, scale, 0, 0);
    context.clearRect(0, 0, width, height);
    const left = 43, right = width - 12, top = 14, bottom = height - 29;
    const x = t => left + (t - (now - ui.trace_seconds)) / ui.trace_seconds * (right - left);
    const y = db => bottom - Math.max(0, Math.min(1, (db - ui.trace_min_dbfs) / (ui.trace_max_dbfs - ui.trace_min_dbfs))) * (bottom - top);
    plot = {left, right, top, bottom, x};
    const life = color("life") || "#45e8a0", signal = color("signal") || "#6cb8ff", caution = color("caution") || "#f6c160";
    context.font = `9px ${colors.getPropertyValue("--mono")}`;
    context.lineWidth = 1;
    // Monitor paper: faint one-second lines, stronger labelled grid lines.
    context.strokeStyle = "rgba(69,232,160,.045)";
    for (let s = Math.ceil(now - ui.trace_seconds); s <= now; s += 1) {
      const xx = x(s); context.beginPath(); context.moveTo(xx, top); context.lineTo(xx, bottom); context.stroke();
    }
    context.fillStyle = "rgba(142,163,174,.75)"; context.strokeStyle = "rgba(69,232,160,.1)";
    for (let db = ui.trace_min_dbfs; db <= ui.trace_max_dbfs; db += ui.trace_grid_db) {
      context.fillText(String(db), 8, y(db) + 3);
      context.beginPath(); context.moveTo(left, y(db)); context.lineTo(right, y(db)); context.stroke();
    }
    for (let age = ui.trace_seconds; age >= 0; age -= ui.trace_grid_s) {
      const xx = x(now - age);
      context.beginPath(); context.moveTo(xx, top); context.lineTo(xx, bottom); context.stroke();
      context.textAlign = age === 0 ? "right" : "left";
      context.fillText(age === 0 ? "now" : `−${age}s`, xx, height - 9);
    }
    context.textAlign = "left";
    context.save(); context.beginPath(); context.rect(left, top, right - left, bottom - top); context.clip();
    for (const prompt of prompts) {
      const start = x(prompt.start), span = x(prompt.end ?? now) - start;
      context.fillStyle = "rgba(108,184,255,.13)"; context.fillRect(start, top, span, bottom - top);
      context.fillStyle = signal; context.fillRect(start, top, span, 2);
      if (span > 70) { context.fillStyle = "rgba(108,184,255,.9)"; context.fillText("OUR KNOCKS", start + 6, top + 13); }
    }
    for (const window of windows) {
      const start = x(window.start), span = x(window.end) - start;
      context.strokeStyle = "rgba(108,184,255,.75)"; context.setLineDash([4, 4]);
      context.strokeRect(start, top + 2, span, bottom - top - 4);
      context.setLineDash([]);
      if (span > 90) { context.fillStyle = "rgba(108,184,255,.85)"; context.fillText("ANSWER WINDOW", start + 6, bottom - 8); }
    }
    const series = key => levels.filter(point => Number.isFinite(point[key])).map(point => [x(point.t), y(point[key])]);
    const floor = series("floor"), level = series("level");
    if (floor.length > 1) {
      context.beginPath(); context.moveTo(floor[0][0], bottom);
      for (const [px, py] of floor) context.lineTo(px, py);
      context.lineTo(floor[floor.length - 1][0], bottom); context.closePath();
      context.fillStyle = "rgba(142,163,174,.07)"; context.fill();
      context.beginPath(); floor.forEach(([px, py], i) => i ? context.lineTo(px, py) : context.moveTo(px, py));
      context.setLineDash([3, 5]); context.strokeStyle = "rgba(142,163,174,.7)"; context.stroke(); context.setLineDash([]);
    }
    if (level.length > 1) {
      const line = () => { context.beginPath(); level.forEach(([px, py], i) => i ? context.lineTo(px, py) : context.moveTo(px, py)); };
      const fill = context.createLinearGradient(0, top, 0, bottom);
      fill.addColorStop(0, "rgba(69,232,160,.26)"); fill.addColorStop(1, "rgba(69,232,160,0)");
      line(); context.lineTo(level[level.length - 1][0], bottom); context.lineTo(level[0][0], bottom); context.closePath();
      context.fillStyle = fill; context.fill();
      line(); context.lineJoin = "round"; context.strokeStyle = "rgba(69,232,160,.18)"; context.lineWidth = 6; context.stroke();
      line(); context.strokeStyle = life; context.lineWidth = 1.8; context.stroke();
      const [hx, hy] = level[level.length - 1];
      context.fillStyle = life; context.beginPath(); context.arc(hx, hy, 3, 0, Math.PI * 2); context.fill();
    }
    const nowMs = performance.now();
    for (const event of sounds) {
      const knock = event.data.is_knock && !event.data.gated, sx = x(event.t);
      context.strokeStyle = knock ? caution : "rgba(142,163,174,.75)"; context.lineWidth = knock ? 2.2 : 1.4;
      context.beginPath(); context.moveTo(sx, bottom - 2); context.lineTo(sx, knock ? top + 18 : bottom - 24); context.stroke();
      if (!knock) continue;
      context.fillStyle = caution; context.beginPath(); context.arc(sx, top + 18, 3.5, 0, Math.PI * 2); context.fill();
      const age = event.arrived ? (nowMs - event.arrived) / 900 : 1;
      if (age < 1) {
        context.strokeStyle = `rgba(246,193,96,${(1 - age).toFixed(3)})`; context.lineWidth = 2;
        context.beginPath(); context.arc(sx, top + 18, 4 + age * 16, 0, Math.PI * 2); context.stroke();
      }
    }
    context.restore();
  }
  function inspectSound(event) {
    if (!plot) return;
    const bounds = canvas.getBoundingClientRect();
    const px = event.clientX - bounds.left;
    const nearest = sounds.reduce((best, sound) => Math.abs(plot.x(sound.t) - px) < (best?.distance ?? Infinity)
      ? {sound, distance: Math.abs(plot.x(sound.t) - px)} : best, null);
    const tooltip = byId("trace-tooltip");
    if (!nearest || nearest.distance > 14) { tooltip.hidden = true; return; }
    const data = nearest.sound.data;
    tooltip.textContent = `${seconds(nearest.sound.t)} · ${data.gated ? "gated" : data.candidate ? "candidate knock" : data.is_knock ? "knock" : "other sound"}\nScore ${Number(data.score).toFixed(3)}${data.top?.length ? `\n${data.top[0][0]} · ${Number(data.top[0][1]).toFixed(3)}` : ""}`;
    tooltip.hidden = false;
    tooltip.style.left = `${Math.max(0, Math.min(px + 8, bounds.width - tooltip.offsetWidth))}px`;
    tooltip.style.top = "12px";
  }
  canvas.addEventListener("pointermove", inspectSound);
  canvas.addEventListener("pointerdown", inspectSound);
  canvas.addEventListener("pointerleave", () => { byId("trace-tooltip").hidden = true; });
  new ResizeObserver(requestPaint).observe(canvas);

  function buildVitals() {
    // Three looping monitor strips. Each repeats every 600 units, so sliding
    // the 1200-unit strip by half its width loops without a seam.
    const host = byId("vitals"), waves = {listen: "M0 30", knock: "M0 30", heart: "M0 30"};
    for (let x = 4; x <= 1200; x += 4) {
      const swell = .55 + .45 * Math.sin(x / 600 * 2 * Math.PI);
      waves.listen += ` L${x} ${(30 + 6 * swell * Math.sin(x / 150 * 2 * Math.PI)).toFixed(2)}`;
    }
    const counts = [3, 2, 4, 1];
    for (let period = 0; period < 8; period++) {
      const o = period * 150;
      let segment = "";
      for (let k = 0; k < counts[period % 4]; k++) {
        const s = o + 34 + k * 20;
        segment += ` H${s} L${s + 3} ${12 + (k % 2) * 4} L${s + 6} ${44 - (k % 2) * 3} L${s + 9} 30`;
      }
      waves.knock += `${segment} H${o + 150}`;
      waves.heart += ` H${o + 30} Q${o + 36} 24 ${o + 42} 30 H${o + 54} L${o + 57} 35 L${o + 62} 5 L${o + 67} 47 L${o + 71} 30 H${o + 90} Q${o + 100} 19 ${o + 110} 30 H${o + 150}`;
    }
    for (const [name, d] of Object.entries(waves)) {
      for (const layer of ["glow", "line"]) svg("path", {d, class: `wave wave-${name} ${layer}`, "vector-effect": "non-scaling-stroke"}, host);
    }
  }
  function setPresent(on) {
    document.body.classList.toggle("present", on);
    byId("present-toggle").setAttribute("aria-pressed", String(on));
    requestPaint();
  }
  byId("present-toggle").addEventListener("click", () => setPresent(!document.body.classList.contains("present")));
  document.addEventListener("keydown", event => {
    if (event.key.toLowerCase() !== "p" || event.metaKey || event.ctrlKey || event.altKey) return;
    const target = event.target;
    if (target instanceof HTMLInputElement || target instanceof HTMLTextAreaElement || byId("approval-dialog").open) return;
    setPresent(!document.body.classList.contains("present"));
  });
  if (new URLSearchParams(location.search).get("present") === "1") setPresent(true);

  async function jsonRequest(url, options = {}) {
    const response = await fetch(url, {cache: "no-store", ...options});
    const result = await response.json();
    if (!response.ok) {
      const detail = result.detail || result.error || `Request failed (${response.status})`;
      throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
    }
    return result;
  }
  function refreshState() {
    if (statePromise) return statePromise;
    statePromise = (async () => {
      const snapshot = await jsonRequest("/api/state");
      if (state?.run_id && snapshot.run_id !== state.run_id) {
        cursor = 0; levels = []; sounds = []; prompts = []; windows = []; activity = []; now = 0;
        liveRound = null; lastStatus = null; exchangeRows.clear(); mapKey = null;
        renderLog();
        patrolRows.clear(); byId("patrol-body").replaceChildren();
        // A socket's server-side cursor belongs to one run. Reconnect with the
        // reset cursor rather than waiting for a new run to reach the old one.
        if (socket) socket.close();
      }
      state = snapshot;
      markCandidates(state);
      // Backfilled events still draw the trace, but must not replace a newer
      // authoritative snapshot with an earlier status or interview answer.
      stateCursor = Number(snapshot.state_seq || 0);
      Object.assign(ui, snapshot.ui || {});
      now = Math.max(now, Number(snapshot.uptime || 0));
      text("trace-subtitle", `Live audio · last ${ui.trace_seconds} seconds`);
      renderState(); prune(); requestPaint();
      return snapshot;
    })().finally(() => { statePromise = null; });
    return statePromise;
  }
  async function connect() {
    const generation = ++connectionGeneration;
    clearTimeout(reconnectTimer);
    clearInterval(pollTimer);
    if (socket) {
      const previous = socket;
      socket = null;
      previous.close();
    }
    connection("offline", "Connecting");
    try {
      await refreshState();
      if (generation !== connectionGeneration) return;
      const history = await jsonRequest(`/api/events?since=${cursor}&limit=${ui.events_limit}`);
      if (generation !== connectionGeneration) return;
      backfilling = true;
      try { for (const event of history.events || []) ingest(event); }
      finally { backfilling = false; }
      renderExchange();
      const scheme = location.protocol === "https:" ? "wss:" : "ws:";
      const ws = new WebSocket(`${scheme}//${location.host}/ws?since=${cursor}`);
      socket = ws;
      ws.onopen = () => {
        if (socket !== ws) return;
        connection("connected", "Connected");
        feedback(state?.pipeline_running === false ? "Session ended. Review the recording and results."
          : state?.patrol ? "The patrol controls the dialog at each stopped gap." : "Operator controls ready.");
        pollTimer = setInterval(() => refreshState().catch(() => {}), ui.state_refresh_ms);
      };
      ws.onmessage = message => {
        if (socket !== ws) return;
        try { ingest(JSON.parse(message.data)); }
        catch (_) { feedback("An event could not be read. Reconnecting…", true); ws.close(); }
      };
      ws.onclose = () => {
        if (socket !== ws) return;
        clearInterval(pollTimer); connection("offline", "Reconnecting");
        reconnectTimer = setTimeout(connect, ui.reconnect_ms);
      };
      ws.onerror = () => ws.close();
    } catch (_) {
      if (generation !== connectionGeneration) return;
      backfilling = false;
      connection("offline", "Offline");
      feedback("The sentinel is unavailable. This page will reconnect automatically.", true);
      reconnectTimer = setTimeout(connect, ui.reconnect_ms);
    }
  }
  async function command(name, args = {}) {
    if (busy || state?.patrol) return;
    busy = true; controls(); feedback("Sending command…");
    try {
      const result = await jsonRequest("/api/command", {
        method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({name, args}),
      });
      if (result.state && typeof result.state === "object") state = {...state, ...result.state};
      const label = name === "approve" ? "Approval recorded." : name === "all_quiet"
        ? "All quiet recorded. Checking for quiet before the challenge." : `${name.charAt(0).toUpperCase() + name.slice(1)} received.`;
      feedback(label); await refreshState();
    } catch (error) { feedback(error.message, true); }
    finally { busy = false; controls(); }
  }
  function currentOperator() {
    return byId("current-operator").value.trim().toUpperCase();
  }
  for (const button of document.querySelectorAll("[data-command]")) {
    button.addEventListener("click", () => command(button.dataset.command,
      button.dataset.command === "all_quiet" ? {operator: currentOperator() || null, challenge: true} : {}));
  }
  async function roverCommand(name, args = {}) {
    if (!state?.patrol || roverBusy || busy) return;
    roverBusy = true; controls();
    const generation = stopGeneration;
    patrolFeedback(`Sending ${name}…`);
    try {
      const result = await jsonRequest("/api/rover", {
        method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({name, args}),
      });
      if (result.ok === false) throw new Error(result.error || "Patrol command failed");
      if (generation === stopGeneration) patrolFeedback(name === "approve" ? `Approval recorded for ${args.gap}.` : `${name.charAt(0).toUpperCase() + name.slice(1)} received.`);
      await refreshState();
    } catch (error) { if (generation === stopGeneration) patrolFeedback(error.message, true); }
    finally { roverBusy = false; controls(); }
  }
  async function emergencyStop() {
    if (!state?.patrol) return;
    const generation = ++stopGeneration;
    patrolFeedback("Sending E-STOP…");
    // No busy guard, shared request queue, or awaited state refresh before POST.
    // This request can overtake a pending ordinary command or approval.
    try {
      const result = await jsonRequest("/api/rover", {
        method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({name: "stop", args: {}}),
      });
      if (result.ok === false) throw new Error(result.error || "Rover did not acknowledge STOP");
      if (generation === stopGeneration) patrolFeedback("E-STOP acknowledged. The patrol cannot resume.");
      refreshState().catch(() => {});
    } catch (error) {
      if (generation === stopGeneration) patrolFeedback(`E-STOP request failed: ${error.message}. Use the rover power switch if it is moving.`, true);
    }
  }
  for (const button of document.querySelectorAll("[data-patrol-command]")) {
    button.addEventListener("click", () => roverCommand(button.dataset.patrolCommand));
  }
  byId("estop-button").addEventListener("click", emergencyStop);
  const dialog = byId("approval-dialog"), initials = byId("operator-initials");
  function openApproval(gap = null) {
    approvalGap = gap;
    text("approval-title", gap === null ? "Approve this finding" : `Approve ${gap}`);
    text("approval-description", gap === null ? "Your initials will be recorded with this session."
      : `Your initials will be recorded with the finding at ${gap}.`);
    byId("approval-estop").hidden = !state?.patrol;
    initials.value = currentOperator(); dialog.showModal(); initials.focus();
  }
  byId("approve-button").addEventListener("click", () => openApproval());
  byId("approval-estop").addEventListener("click", () => { dialog.close(); emergencyStop(); });
  byId("cancel-approval").addEventListener("click", () => dialog.close());
  byId("approval-form").addEventListener("submit", event => {
    event.preventDefault();
    const operator = initials.value.trim().toUpperCase();
    if (!operator) { initials.focus(); return; }
    byId("current-operator").value = operator;
    const gap = approvalGap;
    dialog.close();
    if (gap !== null) roverCommand("approve", {gap, operator});
    else if (state?.patrol) patrolFeedback("Choose a confirmed gap to approve its finding.", true);
    else command("approve", {operator});
  });
  window.addEventListener("online", () => { if (!connected) connect(); });
  buildVitals();
  connect();
})();
