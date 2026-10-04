"use strict";
// aquacontrol UI – vanilla JS, no build step. All device writes go through the JSON API.

const $ = (sel, root = document) => root.querySelector(sel);
const el = (tag, attrs = {}, ...children) => {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") e.className = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined && v !== false) e.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children) e.append(c instanceof Node ? c : document.createTextNode(String(c)));
  return e;
};
const SVG = "http://www.w3.org/2000/svg";
const svg = (tag, attrs = {}) => {
  const e = document.createElementNS(SVG, tag);
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
  return e;
};
const fmt = (v, digits = 1, unit = "") => (v === null || v === undefined ? "–" : `${Number(v).toFixed(digits)}${unit}`);

async function api(method, path, body) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const resp = await fetch(path, opts);
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
  return data;
}

function showMsg(node, text, ok) {
  node.textContent = text;
  node.className = `msg ${ok ? "ok" : "err"}`;
}

function savedText(res) {
  if (!res.changed) return "Keine Änderung.";
  return res.backup ? `Gespeichert (Backup ${res.backup}).` : "Gespeichert.";
}

// ---------------------------------------------------------------- tabs
const loaders = {};
for (const btn of document.querySelectorAll("nav button")) {
  btn.addEventListener("click", () => {
    for (const b of document.querySelectorAll("nav button")) b.classList.toggle("active", b === btn);
    for (const s of document.querySelectorAll("main section")) s.hidden = s.id !== `tab-${btn.dataset.tab}`;
    if (loaders[btn.dataset.tab]) loaders[btn.dataset.tab]();
  });
}

// ---------------------------------------------------------------- overview
let lastStatus = null;

async function refreshStatus() {
  try {
    lastStatus = await api("GET", "/api/status");
  } catch (e) {
    $("#online").textContent = "keine Verbindung";
    $("#online").className = "pill off";
    return;
  }
  const s = lastStatus;
  $("#online").textContent = s.online ? "QUADRO online" : "QUADRO offline";
  $("#online").className = `pill ${s.online ? "on" : "off"}`;
  const st = s.status;
  $("#water").textContent = st ? fmt(st.temps[0], 1, " °C") : "–";
  const tiles = $("#tiles");
  tiles.replaceChildren();
  if (st) {
    st.temps.forEach((t, i) => {
      if (t !== null) tiles.append(tile(s.sensor_names[i], fmt(t, 2, " °C")));
    });
    tiles.append(tile("Durchfluss", fmt(st.flow_lph, 1, " l/h")));
    st.fans.forEach((f, i) => tiles.append(tile(s.fan_names[i], `${f.rpm} rpm`, `${fmt(f.percent, 1, " %")} · ${fmt(f.power_w, 2, " W")}`)));
  }
  const sensors = $("#sensors");
  sensors.replaceChildren();
  for (const r of s.sensors) sensors.append(tile(r.label, fmt(r.value, 1, ` ${r.unit}`)));
  for (const [src, age] of Object.entries(s.external_sources || {})) {
    if (age > 30) sensors.append(tile(src, "keine Daten", `letzter Push vor ${Math.round(age)} s`, true));
  }
  if (fanEditorsReady) updateOperatingPoints();
}

function tile(label, value, sub = "", stale = false) {
  return el("div", { class: `card tile${stale ? " stale" : ""}` },
    el("div", { class: "label" }, label), el("div", { class: "value" }, value),
    sub ? el("div", { class: "sub" }, sub) : "");
}

// history chart -------------------------------------------------------
const COLORS = ["--s1", "--s2", "--s3", "--s4", "--s5", "--s6"];
const hiddenSeries = new Set(JSON.parse(localStorageGet("hiddenSeries") || "[]"));

function localStorageGet(k) { try { return localStorage.getItem(k); } catch { return null; } }
function localStorageSet(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode */ } }

function seriesLabel(key) {
  const s = lastStatus;
  if (!s) return key;
  let m = key.match(/^temp(\d)$/);
  if (m) return s.sensor_names[m[1] - 1];
  m = key.match(/^fan(\d)_(rpm|percent)$/);
  if (m) return `${s.fan_names[m[1] - 1]} ${m[2] === "rpm" ? "rpm" : "%"}`;
  if (key === "flow") return "Durchfluss l/h";
  const r = (s.sensors || []).find((x) => x.id === key);
  return r ? r.label : key;
}

async function refreshHistory() {
  let data;
  try {
    data = await api("GET", `/api/history?minutes=${$("#hist-range").value}`);
  } catch { return; }
  const keys = [...new Set(data.flatMap((d) => Object.keys(d)))]
    .filter((k) => k !== "t" && !k.endsWith("_rpm") && !k.endsWith("_percent") && k !== "flow");
  const series = $("#series");
  series.replaceChildren();
  keys.forEach((k, i) => {
    const color = `var(${COLORS[i % COLORS.length]})`;
    const cb = el("input", { type: "checkbox", checked: !hiddenSeries.has(k) });
    cb.addEventListener("change", () => {
      cb.checked ? hiddenSeries.delete(k) : hiddenSeries.add(k);
      localStorageSet("hiddenSeries", JSON.stringify([...hiddenSeries]));
      refreshHistory();
    });
    const sw = el("span", { class: "swatch" });
    sw.style.background = color;
    series.append(el("label", {}, cb, sw, seriesLabel(k)));
  });
  drawChart($("#chart"), data, keys.filter((k) => !hiddenSeries.has(k)), keys);
}

function drawChart(root, data, visible, allKeys) {
  root.replaceChildren();
  const W = 800, H = 260, L = 40, B = 20, T = 10;
  if (data.length < 2 || visible.length === 0) {
    root.append(Object.assign(svg("text", { x: W / 2, y: H / 2, "text-anchor": "middle" }), { textContent: "Noch zu wenig Daten" }));
    return;
  }
  const vals = data.flatMap((d) => visible.map((k) => d[k]).filter((v) => v !== undefined));
  let lo = Math.floor(Math.min(...vals) - 1), hi = Math.ceil(Math.max(...vals) + 1);
  const t0 = data[0].t, t1 = data[data.length - 1].t;
  const x = (t) => L + ((t - t0) / (t1 - t0 || 1)) * (W - L - 5);
  const y = (v) => T + (1 - (v - lo) / (hi - lo || 1)) * (H - T - B);
  for (let i = 0; i <= 4; i++) {
    const v = lo + ((hi - lo) * i) / 4;
    root.append(svg("line", { x1: L, x2: W, y1: y(v), y2: y(v), class: "gridline" }));
    root.append(Object.assign(svg("text", { x: 4, y: y(v) + 4 }), { textContent: `${v.toFixed(0)}°` }));
  }
  for (const frac of [0, 0.5, 1]) {
    const t = t0 + (t1 - t0) * frac;
    const label = new Date(t * 1000).toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit" });
    root.append(Object.assign(svg("text", { x: x(t), y: H - 4, "text-anchor": frac === 0 ? "start" : frac === 1 ? "end" : "middle" }), { textContent: label }));
  }
  for (const k of visible) {
    const idx = allKeys.indexOf(k);
    const pts = data.filter((d) => d[k] !== undefined).map((d) => `${x(d.t).toFixed(1)},${y(d[k]).toFixed(1)}`);
    const line = svg("polyline", { points: pts.join(" "), fill: "none", "stroke-width": 2 });
    line.style.stroke = `var(${COLORS[idx % COLORS.length]})`;
    root.append(line);
  }
}

$("#hist-range").addEventListener("change", refreshHistory);

// ---------------------------------------------------------------- fans
let fanEditorsReady = false;
const fanState = [];

loaders.fans = async () => {
  const box = $("#fan-editors");
  let settings;
  try {
    settings = await api("GET", "/api/settings");
  } catch (e) {
    box.replaceChildren(el("p", { class: "msg err" }, `Einstellungen nicht lesbar: ${e.message}`));
    return;
  }
  box.replaceChildren();
  fanState.length = 0;
  for (const fan of settings.fans) box.append(fanEditor(fan, settings.sensors));
  fanEditorsReady = true;
  updateOperatingPoints();
};

function fanEditor(fan, sensorNames) {
  const state = { fan, curve: fan.curve.map((p) => [...p]) };
  fanState.push(state);
  const msg = el("span", { class: "msg" });
  const mode = el("select", {},
    ...[["fixed", "Fest"], ["target", "Zieltemperatur"], ["curve", "Kurve"]].map(([v, t]) => el("option", { value: v, selected: fan.mode === v }, t)));
  if (!["fixed", "target", "curve"].includes(fan.mode)) mode.prepend(el("option", { value: fan.mode, selected: true, disabled: true }, `${fan.mode} (nur Anzeige)`));
  const fixed = el("input", { type: "number", min: 0, max: 100, step: 0.5, value: fan.fixed_percent });
  const target = el("input", { type: "number", min: 20, max: 60, step: 0.5, value: fan.target_c });
  const sensor = el("select", {}, ...sensorNames.map((n, i) => el("option", { value: i, selected: fan.sensor === i }, `${i + 1}: ${n}`)));
  const min = el("input", { type: "number", min: fan.floor_percent ?? 0, max: 100, step: 0.5, value: fan.min_percent });
  const max = el("input", { type: "number", min: 0, max: 100, step: 0.5, value: fan.max_percent });
  const chart = svg("svg", { class: "curve", viewBox: "0 0 400 240" });
  state.chart = chart;
  const points = el("div", { class: "points" });
  state.points = points;

  const rowFixed = el("label", {}, "Fest-Wert %", fixed);
  const rowTarget = el("label", {}, "Zieltemperatur °C", target);
  const curveBox = el("div", {}, chart, el("details", {}, el("summary", {}, "Punkte als Tabelle"), points));
  const sync = () => {
    rowFixed.hidden = mode.value !== "fixed";
    rowTarget.hidden = mode.value !== "target";
    curveBox.hidden = mode.value !== "curve";
  };
  mode.addEventListener("change", sync);

  const save = el("button", { class: "primary" }, "Speichern");
  save.addEventListener("click", async () => {
    const body = { mode: mode.value, sensor: Number(sensor.value), min_percent: Number(min.value), max_percent: Number(max.value) };
    if (mode.value === "fixed") body.fixed_percent = Number(fixed.value);
    if (mode.value === "target") body.target_c = Number(target.value);
    if (mode.value === "curve") body.curve = state.curve;
    if (!["fixed", "target", "curve"].includes(mode.value)) delete body.mode;
    save.disabled = true;
    try {
      showMsg(msg, savedText(await api("PUT", `/api/settings/fan/${fan.index}`, body)), true);
    } catch (e) {
      showMsg(msg, e.message, false);
    } finally {
      save.disabled = false;
    }
  });

  const floorHint = fan.floor_percent != null
    ? el("p", { class: "hint" }, `Minimum darf nicht unter ${fan.floor_percent} % liegen. Kurvenpunkte darunter werden vom Gerät auf das Minimum angehoben.`) : "";
  const card = el("div", { class: "card" },
    el("h3", {}, `${fan.index}: ${fan.name}`),
    el("div", { class: "fan-editor" },
      el("div", { class: "fields" },
        el("label", {}, "Modus", mode), rowFixed, rowTarget, el("label", {}, "Sensor", sensor),
        el("label", {}, "Minimum %", min), el("label", {}, "Maximum %", max),
        el("p", { class: "hint" }, `Fallback ${fan.fallback_percent} % · PID ${fan.pid.slice(0, 3).join("/")} (nur Anzeige)`),
        floorHint, save, " ", msg),
      curveBox));
  sync();
  drawCurve(state);
  return card;
}

const CX = (t) => 30 + ((t - 0) / 70) * 360;        // 0..70 °C
const CY = (p) => 10 + (1 - p / 100) * 210;          // 0..100 %
const invX = (x) => ((x - 30) / 360) * 70;
const invY = (y) => (1 - (y - 10) / 210) * 100;
const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
const round1 = (v) => Math.round(v * 10) / 10;

function drawCurve(state) {
  const c = state.chart;
  c.replaceChildren();
  for (let t = 0; t <= 70; t += 10) {
    c.append(svg("line", { x1: CX(t), x2: CX(t), y1: 10, y2: 220, class: "gridline" }));
    c.append(Object.assign(svg("text", { x: CX(t), y: 236, "text-anchor": "middle" }), { textContent: `${t}°` }));
  }
  for (let p = 0; p <= 100; p += 25) {
    c.append(svg("line", { x1: 30, x2: 390, y1: CY(p), y2: CY(p), class: "gridline" }));
    c.append(Object.assign(svg("text", { x: 2, y: CY(p) + 4 }), { textContent: `${p}%` }));
  }
  c.append(svg("polyline", { points: state.curve.map(([t, p]) => `${CX(t)},${CY(p)}`).join(" ") }));
  if (state.now !== undefined) c.append(svg("line", { x1: CX(state.now), x2: CX(state.now), y1: 10, y2: 220, class: "now" }));
  state.curve.forEach(([t, p], i) => {
    const dot = svg("circle", { cx: CX(t), cy: CY(p), r: 6 });
    dot.addEventListener("pointerdown", (ev) => startDrag(ev, state, i));
    c.append(dot);
  });
  renderPointTable(state);
}

function startDrag(ev, state, i) {
  ev.preventDefault();
  const c = state.chart;
  c.setPointerCapture(ev.pointerId);
  const move = (e) => {
    const pt = c.createSVGPoint();
    pt.x = e.clientX; pt.y = e.clientY;
    const p = pt.matrixTransform(c.getScreenCTM().inverse());
    const lo = i > 0 ? state.curve[i - 1][0] + 0.1 : 0;
    const hi = i < 15 ? state.curve[i + 1][0] - 0.1 : 100;
    state.curve[i] = [round1(clamp(invX(p.x), lo, hi)), round1(clamp(invY(p.y), 0, 100))];
    drawCurve(state);
  };
  const up = () => {
    c.removeEventListener("pointermove", move);
    c.removeEventListener("pointerup", up);
  };
  c.addEventListener("pointermove", move);
  c.addEventListener("pointerup", up);
}

function renderPointTable(state) {
  if (state.points.contains(document.activeElement)) return; // don't rebuild while typing
  state.points.replaceChildren();
  state.curve.forEach(([t, p], i) => {
    const ti = el("input", { type: "number", step: 0.1, value: t, "aria-label": `Punkt ${i + 1} °C` });
    const pi = el("input", { type: "number", step: 0.1, value: p, "aria-label": `Punkt ${i + 1} %` });
    const upd = () => { state.curve[i] = [Number(ti.value), Number(pi.value)]; drawCurve(state); };
    ti.addEventListener("change", upd);
    pi.addEventListener("change", upd);
    state.points.append(el("span", {}, `${i + 1}`), ti, pi, el("span", {}, ""));
  });
}

function updateOperatingPoints() {
  if (!lastStatus || !lastStatus.status) return;
  for (const state of fanState) {
    const temp = lastStatus.status.temps[state.fan.sensor];
    if (temp !== null && temp !== undefined && Math.abs((state.now ?? -1) - temp) > 0.05) {
      state.now = temp;
      drawCurve(state);
    }
  }
}

// ---------------------------------------------------------------- LEDs
loaders.leds = async () => {
  let settings;
  try {
    settings = await api("GET", "/api/settings");
  } catch (e) {
    showMsg($("#strip-msg"), e.message, false);
    return;
  }
  $("#strip-on").checked = settings.strip.enabled;
  $("#strip-bright").value = settings.strip.brightness;
  $("#strip-bright-val").textContent = settings.strip.brightness;
  const list = $("#led-list");
  list.replaceChildren();
  for (const led of settings.leds.filter((l) => l.led_count > 1)) {
    list.append(tile(`${led.index}: ${led.name}`, `LED ${led.led_start}–${led.led_start + led.led_count - 1}`, `Modus ${led.mode}`));
  }
};
$("#strip-bright").addEventListener("input", () => { $("#strip-bright-val").textContent = $("#strip-bright").value; });
$("#strip-save").addEventListener("click", async () => {
  try {
    const res = await api("PUT", "/api/settings/strip", { enabled: $("#strip-on").checked, brightness: Number($("#strip-bright").value) });
    showMsg($("#strip-msg"), savedText(res), true);
  } catch (e) {
    showMsg($("#strip-msg"), e.message, false);
  }
});

// ---------------------------------------------------------------- schedule
const DAY_NAMES = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"];
let rules = [];

loaders.schedule = async () => {
  try {
    renderSchedule(await api("GET", "/api/schedule"));
  } catch (e) {
    showMsg($("#rules-msg"), e.message, false);
  }
};

function renderSchedule(data) {
  rules = data.rules.map((r) => ({ days: [0, 1, 2, 3, 4, 5, 6], ...r }));
  const d = data.desired;
  const status = $("#sched-status");
  const now = (on) => {
    const b = el("button", {}, on ? "LEDs jetzt an" : "LEDs jetzt aus");
    b.addEventListener("click", async () => {
      try { renderSchedule(await api("POST", "/api/schedule/override", { on })); } catch (e) { showMsg($("#rules-msg"), e.message, false); }
    });
    return b;
  };
  status.replaceChildren(
    el("div", {}, `Soll-Zustand: ${d ? (d.on ? "an" : "aus") + (d.brightness != null ? `, Helligkeit ${d.brightness}` : "") : "keine Regel"}`
      + (data.override_active ? " (manuell übersteuert bis zur nächsten Regel)" : "")),
    el("div", { class: "hint" }, `Nächster Wechsel: ${data.next_change ? new Date(data.next_change).toLocaleString("de-DE") : "–"}`),
    data.last_error ? el("div", { class: "msg err" }, `Letzter Fehler: ${data.last_error}`) : "",
    now(true), " ", now(false));
  renderRules();
}

function renderRules() {
  const tbody = $("#rules tbody");
  tbody.replaceChildren();
  rules.forEach((r, i) => {
    const time = el("input", { type: "time", value: r.time });
    time.addEventListener("change", () => { r.time = time.value; });
    const days = el("td", { class: "days" }, ...DAY_NAMES.map((n, d) => {
      const cb = el("input", { type: "checkbox", checked: r.days.includes(d) });
      cb.addEventListener("change", () => { r.days = DAY_NAMES.map((_, k) => k).filter((k) => (k === d ? cb.checked : r.days.includes(k))); });
      return el("label", {}, cb, n);
    }));
    const action = el("select", {}, el("option", { value: "on", selected: r.on }, "an"), el("option", { value: "off", selected: !r.on }, "aus"));
    action.addEventListener("change", () => { r.on = action.value === "on"; });
    const bright = el("input", { type: "number", min: 0, max: 255, placeholder: "unverändert", value: r.brightness ?? "" });
    bright.addEventListener("change", () => { r.brightness = bright.value === "" ? undefined : Number(bright.value); });
    const del = el("button", { class: "danger" }, "Löschen");
    del.addEventListener("click", () => { rules.splice(i, 1); renderRules(); });
    tbody.append(el("tr", {}, el("td", {}, time), days, el("td", {}, action), el("td", {}, bright), el("td", {}, del)));
  });
}

$("#rule-add").addEventListener("click", () => {
  rules.push({ time: "09:00", target: "strip", on: true, days: [0, 1, 2, 3, 4, 5, 6] });
  renderRules();
});
$("#rules-save").addEventListener("click", async () => {
  const payload = rules.map((r) => {
    const out = { time: r.time, target: "strip", on: r.on };
    if (r.brightness !== undefined && r.brightness !== null) out.brightness = r.brightness;
    if (r.days.length !== 7) out.days = r.days;
    return out;
  });
  try {
    renderSchedule(await api("PUT", "/api/schedule", { rules: payload }));
    showMsg($("#rules-msg"), "Zeitplan gespeichert.", true);
  } catch (e) {
    showMsg($("#rules-msg"), e.message, false);
  }
});

// ---------------------------------------------------------------- backups
loaders.backups = async () => {
  const tbody = $("#backup-list tbody");
  let items;
  try {
    items = await api("GET", "/api/backups");
  } catch (e) {
    showMsg($("#backup-msg"), e.message, false);
    return;
  }
  tbody.replaceChildren();
  for (const b of items) {
    const btn = el("button", {}, "Wiederherstellen");
    let armed = false;
    btn.addEventListener("click", async () => {
      if (!armed) { armed = true; btn.textContent = "Wirklich wiederherstellen?"; btn.className = "danger"; return; }
      try {
        showMsg($("#backup-msg"), savedText(await api("POST", `/api/backups/${encodeURIComponent(b.name)}/restore`, {})), true);
        loaders.backups();
      } catch (e) {
        showMsg($("#backup-msg"), e.message, false);
      }
    });
    tbody.append(el("tr", {}, el("td", {}, b.name), el("td", {}, btn)));
  }
};

// ---------------------------------------------------------------- start
refreshStatus();
refreshHistory();
setInterval(refreshStatus, 2000);
setInterval(refreshHistory, 30000);
