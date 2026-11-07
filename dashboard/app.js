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
  let state = null, socket = null, cursor = 0, connected = false;
  let stateCursor = 0;
  let busy = false, statePromise = null, reconnectTimer = null, pollTimer = null;
  let connectionGeneration = 0;
  let levels = [], sounds = [], prompts = [], windows = [], activity = [];
  let now = 0, plot = null, paintQueued = false;
  const canvas = byId("trace"), context = canvas.getContext("2d");

  function seconds(value) {
    return `${Number(value || 0).toFixed(2)}s`;
  }
  function clock(value) {
    const whole = Math.max(0, Math.floor(Number(value || 0)));
    return `${String(Math.floor(whole / 60)).padStart(2, "0")}:${String(whole % 60).padStart(2, "0")}`;
  }
  function connection(mode, label) {
    connected = mode === "connected";
    byId("connection").className = `connection ${mode}`;
    text("connection-label", label);
    controls();
  }
  function feedback(message, error = false) {
    text("command-feedback", message);
    byId("command-feedback").classList.toggle("error", error);
  }
  function controls() {
    const running = connected && state && state.pipeline_running !== false && !busy;
    const idle = state && ["LISTENING", "NO_RESPONSE", "CONFIRMED"].includes(state.state);
    for (const button of document.querySelectorAll("[data-command]")) {
      const command = button.dataset.command;
      const allowed = command === "reset" ||
        (command === "challenge" && ["LISTENING", "NO_RESPONSE"].includes(state?.state)) ||
        (command === "interview" && state?.state === "CONFIRMED") ||
        (command === "reassure" && idle);
      button.disabled = !(running && allowed);
    }
    byId("approve-button").disabled = !(running && state.state === "CONFIRMED" && !state.approved);
  }
  function phaseLabel(snapshot) {
    if (snapshot.pipeline_running === false) return "Session ended · recording available for review";
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
    const status = state.status || "NO_SIGNS";
    byId("status-card").className = `status-card ${status.toLowerCase()}`;
    text("status-title", status.replaceAll("_", " "));
    text("phase", phaseLabel(state));
    text("odds", state.odds);
    const badge = byId("approval-badge");
    badge.hidden = !state.approved;
    if (state.approved) badge.textContent = state.approved_by ? `Approved by ${state.approved_by}` : "Operator approved";

    for (const [key, id] of [["q_people", "answer-people"], ["q_move", "answer-move"], ["q_hurt", "answer-hurt"]]) {
      const value = state.answers?.[key];
      text(id, value === undefined ? "Awaiting reply" : value);
      byId(id).className = value === undefined ? "unanswered" : value === "unclear" || value === "no answer" ? "unclear" : "";
    }
    renderRounds(state.rounds || []);
    const box = state.blackbox;
    if (box) {
      const failed = box.integrity_valid === false || box.error;
      byId("blackbox-status").classList.toggle("error", !!failed);
      text("blackbox-status", failed
        ? `Black box: recording error${box.error ? ` — ${box.error}` : ""}`
        : `Black box: ${Number(box.events || 0).toLocaleString()} events, hash-chained.`);
    }
    controls();
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
  function eventText(event) {
    const d = event.data || {};
    switch (event.type) {
      case "sound": {
        const top = d.top?.[0]?.[0];
        return `${d.gated ? "Our prompt / gated sound" : d.is_knock ? "Knock" : "Other sound"} · score ${Number(d.score || 0).toFixed(2)}${top ? ` · ${top}` : ""}`;
      }
      case "state": return `${String(d.state || "").replaceAll("_", " ")} · ${String(d.status || "").replaceAll("_", " ")}`;
      case "prompt_start": return "Sending prompt" + (d.actions ? ` · ${d.actions.map(a => a.key?.replaceAll("_", " ") || `${a.n} knocks`).join(", ")}` : "");
      case "prompt_end": return d.interrupted ? "Prompt interrupted" : "Prompt finished";
      case "window_open": return `Answer window opened${d.question ? ` · ${d.question.replace("q_", "")}` : ` · round ${d.round}`}`;
      case "round_result": return `Round ${d.round}: asked ${d.expected}, heard ${d.heard} · ${d.matched ? "matched" : "did not match"} · ${d.odds}`;
      case "answer": return `${({q_people: "People", q_move: "Can move", q_hurt: "Hurt"})[d.question] || d.question}: ${d.value}`;
      case "approval": return `Approved${d.operator ? ` by ${d.operator}` : " by operator"}`;
      case "operator": return d.message || `Operator: ${d.command || d.action || "command"}`;
      case "error": return d.message || "An operation failed";
      case "session_start": return "Session started";
      case "session_end": return "Session ended";
      default: return event.type.replaceAll("_", " ");
    }
  }
  function renderLog() {
    if (!activity.length) {
      text("event-count", "WAITING FOR EVENTS");
      const row = document.createElement("li");
      row.className = "empty-log";
      row.textContent = "The sentinel’s activity will appear here.";
      byId("event-log").replaceChildren(row);
      return;
    }
    text("event-count", `${activity.length} RECENT EVENTS`);
    byId("event-log").replaceChildren(...activity.slice().reverse().map(event => {
      const row = document.createElement("li"), at = document.createElement("time"), detail = document.createElement("span");
      at.textContent = seconds(event.t);
      detail.textContent = eventText(event);
      row.className = event.type === "error" ? "warning" : ["answer", "approval", "round_result"].includes(event.type) ? "important" : "";
      row.append(at, detail);
      return row;
    }));
  }
  function ingest(event) {
    if (!event || typeof event.seq !== "number" || event.seq <= cursor) return;
    if (cursor && event.seq > cursor + 1) refreshState().catch(() => {});
    cursor = event.seq;
    now = Math.max(now, Number(event.t || 0));
    const d = event.data || {};
    if (event.type === "level") {
      levels.push({t: event.t, level: d.dbfs, floor: d.floor_db});
      text("meter-value", `${Number(d.dbfs).toFixed(1)} dBFS`);
    } else {
      activity.push(event);
      if (ui.event_log_limit) activity = activity.slice(-ui.event_log_limit);
      renderLog();
    }
    if (event.type === "sound") sounds.push(event);
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
    if (event.type === "state" && event.seq > stateCursor) {
      state = {...state, ...d};
      if (!d.approved) state.approved_by = null;
      renderState();
    }
    if (event.type === "answer" && state && event.seq > stateCursor) {
      state.answers = {...state.answers, [d.question]: d.value}; renderState();
    }
    if (event.type === "approval" && state && event.seq > stateCursor) {
      state.approved = true; state.approved_by = d.operator; renderState();
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
  function draw() {
    if (!context || !ui.trace_seconds) return;
    const width = canvas.clientWidth, height = canvas.clientHeight, scale = window.devicePixelRatio || 1;
    canvas.width = Math.round(width * scale); canvas.height = Math.round(height * scale);
    context.setTransform(scale, 0, 0, scale, 0, 0);
    const left = 43, right = width - 12, top = 12, bottom = height - 29;
    const x = t => left + (t - (now - ui.trace_seconds)) / ui.trace_seconds * (right - left);
    const y = db => bottom - Math.max(0, Math.min(1, (db - ui.trace_min_dbfs) / (ui.trace_max_dbfs - ui.trace_min_dbfs))) * (bottom - top);
    plot = {left, right, top, bottom, x};
    context.font = `9px ${colors.getPropertyValue("--mono")}`;
    context.fillStyle = color("slate"); context.strokeStyle = "rgba(158,177,188,.14)"; context.lineWidth = 1;
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
      context.fillStyle = "rgba(123,185,237,.16)";
      context.fillRect(x(prompt.start), top, x(prompt.end ?? now) - x(prompt.start), bottom - top);
    }
    for (const window of windows) {
      context.strokeStyle = "rgba(123,185,237,.7)"; context.setLineDash([4, 4]);
      context.strokeRect(x(window.start), top + 2, x(window.end) - x(window.start), bottom - top - 4);
    }
    for (const [key, stroke, dashed] of [["floor", color("slate"), true], ["level", color("paper"), false]]) {
      context.setLineDash(dashed ? [3, 5] : []); context.strokeStyle = stroke; context.lineWidth = dashed ? 1 : 1.5;
      context.beginPath(); let started = false;
      for (const point of levels) {
        if (!Number.isFinite(point[key])) continue;
        if (!started) { context.moveTo(x(point.t), y(point[key])); started = true; }
        else context.lineTo(x(point.t), y(point[key]));
      }
      context.stroke();
    }
    context.setLineDash([]);
    for (const event of sounds) {
      const knock = event.data.is_knock && !event.data.gated;
      context.strokeStyle = knock ? color("caution") : color("slate"); context.lineWidth = knock ? 2 : 1.5;
      context.beginPath(); context.moveTo(x(event.t), bottom - 2);
      context.lineTo(x(event.t), knock ? top + 16 : bottom - 24); context.stroke();
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
    tooltip.textContent = `${seconds(nearest.sound.t)} · ${data.gated ? "gated" : data.is_knock ? "knock" : "other sound"}\nScore ${Number(data.score).toFixed(3)}${data.top?.length ? `\n${data.top[0][0]} · ${Number(data.top[0][1]).toFixed(3)}` : ""}`;
    tooltip.hidden = false;
    tooltip.style.left = `${Math.max(0, Math.min(px + 8, bounds.width - tooltip.offsetWidth))}px`;
    tooltip.style.top = "12px";
  }
  canvas.addEventListener("pointermove", inspectSound);
  canvas.addEventListener("pointerdown", inspectSound);
  canvas.addEventListener("pointerleave", () => { byId("trace-tooltip").hidden = true; });
  new ResizeObserver(requestPaint).observe(canvas);

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
        renderLog();
        // A socket's server-side cursor belongs to one run. Reconnect with the
        // reset cursor rather than waiting for a new run to reach the old one.
        if (socket) socket.close();
      }
      state = snapshot;
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
      for (const event of history.events || []) ingest(event);
      const scheme = location.protocol === "https:" ? "wss:" : "ws:";
      const ws = new WebSocket(`${scheme}//${location.host}/ws?since=${cursor}`);
      socket = ws;
      ws.onopen = () => {
        if (socket !== ws) return;
        connection("connected", "Connected");
        feedback(state?.pipeline_running === false ? "Session ended. Review the recording and results." : "Operator controls ready.");
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
      connection("offline", "Offline");
      feedback("The sentinel is unavailable. This page will reconnect automatically.", true);
      reconnectTimer = setTimeout(connect, ui.reconnect_ms);
    }
  }
  async function command(name, args = {}) {
    if (busy) return;
    busy = true; controls(); feedback("Sending command…");
    try {
      const result = await jsonRequest("/api/command", {
        method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({name, args}),
      });
      if (result.state && typeof result.state === "object") state = {...state, ...result.state};
      const label = name === "approve" ? "Approval recorded." : `${name.charAt(0).toUpperCase() + name.slice(1)} received.`;
      feedback(label); await refreshState();
    } catch (error) { feedback(error.message, true); }
    finally { busy = false; controls(); }
  }
  for (const button of document.querySelectorAll("[data-command]")) {
    button.addEventListener("click", () => command(button.dataset.command));
  }
  const dialog = byId("approval-dialog"), initials = byId("operator-initials");
  byId("approve-button").addEventListener("click", () => { initials.value = ""; dialog.showModal(); initials.focus(); });
  byId("cancel-approval").addEventListener("click", () => dialog.close());
  byId("approval-form").addEventListener("submit", event => {
    event.preventDefault();
    const operator = initials.value.trim().toUpperCase();
    if (!operator) { initials.focus(); return; }
    dialog.close(); command("approve", {operator});
  });
  window.addEventListener("online", () => { if (!connected) connect(); });
  connect();
})();
