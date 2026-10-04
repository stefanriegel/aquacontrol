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
let historyAfterStatus = false;

async function refreshStatus() {
  try {
    lastStatus = await api("GET", "/api/status");
  } catch (e) {
    $("#online").textContent = "keine Verbindung";
    $("#online").className = "pill off";
    return;
  }
  const s = lastStatus;
  if (!historyAfterStatus) { historyAfterStatus = true; refreshHistory(); } // legend labels need the names
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
  updateLedMarkers();
}

function tile(label, value, sub = "", stale = false) {
  return el("div", { class: `card tile${stale ? " stale" : ""}` },
    el("div", { class: "label" }, label), el("div", { class: "value" }, value),
    sub ? el("div", { class: "sub" }, sub) : "");
}

// history chart -------------------------------------------------------
const COLORS = ["--s1", "--s2", "--s3", "--s4", "--s5", "--s6"];
const hiddenSeries = new Set(loadHiddenSeries());

function loadHiddenSeries() {
  try {
    const v = JSON.parse(localStorageGet("hiddenSeries") || "[]");
    return Array.isArray(v) ? v : [];
  } catch { return []; }
}

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
    root.append(Object.assign(svg("text", { x: 4, y: y(v) + 4 }), { textContent: `${v.toFixed((hi - lo) / 4 < 1 ? 1 : 0)}°` }));
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

// Request body for saving a fan. `sensor` is only sent if the user picked one, so a controller whose
// stored sensor is outside 1-4 is never changed by an unrelated save.
function fanSaveBody(v) {
  const body = { mode: v.mode, min_percent: v.min, max_percent: v.max };
  if (v.sensorChanged) body.sensor = v.sensor;
  if (v.mode === "fixed") body.fixed_percent = v.fixed;
  if (v.mode === "target") body.target_c = v.target;
  if (v.mode === "curve") body.curve = v.curve;
  if (!["fixed", "target", "curve"].includes(v.mode)) delete body.mode;
  return body;
}

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
  if (!sensorNames.some((_, i) => i === fan.sensor)) sensor.prepend(el("option", { value: fan.sensor, selected: true, disabled: true }, `${fan.sensor} (nur Anzeige)`));
  let sensorChanged = false;
  sensor.addEventListener("change", () => { sensorChanged = true; });
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
    const body = fanSaveBody({ mode: mode.value, sensor: Number(sensor.value), sensorChanged, min: Number(min.value),
      max: Number(max.value), fixed: Number(fixed.value), target: Number(target.value), curve: state.curve });
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
    ? el("p", { class: "hint" }, `Die Kurve wird vom Gerät linear auf Minimum–Maximum abgebildet: 0 % = Minimum, 100 % = Maximum. Das Minimum darf nicht unter ${fan.floor_percent} % liegen.`) : "";
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
    const hi = i < state.curve.length - 1 ? state.curve[i + 1][0] - 0.1 : 100;
    const t = lo > hi ? state.curve[i][0] : round1(clamp(invX(p.x), lo, hi)); // neighbours too close: keep temperature
    state.curve[i] = [t, round1(clamp(invY(p.y), 0, 100))];
    drawCurve(state);
  };
  const up = () => {
    c.removeEventListener("pointermove", move);
    c.removeEventListener("pointerup", up);
    c.removeEventListener("pointercancel", up);
  };
  c.addEventListener("pointermove", move);
  c.addEventListener("pointerup", up);
  c.addEventListener("pointercancel", up);
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
const ledStates = [];

// Request body for saving one LED controller. The source is only sent once the user picked one, so a source
// that is shown but not selectable (flow, software sensor) is never rewritten by an unrelated save.
function ledSaveBody(v) {
  const body = { colors: v.colors, fade: v.fade, blink: v.blink, brightness_by_source: v.brightness };
  if (v.mode === "farbschalter") {
    body.thresholds = v.thresholds;
    if (v.sourceChanged) body.source = v.source;
  }
  return body;
}

// Colour bands of the preview bar: [{from, to, color}] over the source range, thresholds clamped into it.
function ledSegments(range, thresholds, colors) {
  const [lo, hi] = range;
  const edges = [lo];
  for (const t of thresholds) edges.push(Math.max(edges[edges.length - 1], Math.min(hi, Math.max(lo, t))));
  edges.push(hi);
  return colors.map((color, i) => ({ from: edges[i], to: edges[Math.min(i + 1, edges.length - 1)], color }));
}

function ledFraction(value, range) {
  const [lo, hi] = range;
  if (!(hi > lo)) return 0;
  return Math.min(1, Math.max(0, (value - lo) / (hi - lo)));
}

// A start value for "+ Schwelle": between the last threshold and the end of the range, or null if there is no room.
function ledNewThreshold(thresholds, range) {
  const last = thresholds[thresholds.length - 1];
  const next = Math.max(last + 1, Math.floor((last + range[1]) / 2));
  return next <= range[1] ? next : null;
}

// "" if the thresholds would be accepted, otherwise the reason (same rules as the server).
function ledProblem(thresholds, range) {
  if (!thresholds.every((t) => Number.isInteger(t))) return "Schwellen müssen ganze Zahlen sein.";
  if (thresholds.some((t, i) => i > 0 && t <= thresholds[i - 1])) return "Schwellen müssen streng steigen.";
  if (thresholds.some((t) => t < range[0] || t > range[1])) return `Schwellen müssen zwischen ${range[0]} und ${range[1]} liegen.`;
  return "";
}

// Drop-down entries for the data source: the four temperature sensors; any other current source is shown, not selectable.
function ledSourceOptions(sensorNames, source, sourceName) {
  const opts = sensorNames.map((n, i) => ({ value: i, label: `${i + 1}: ${n}`, disabled: false }));
  if (!sensorNames.some((_, i) => i === source)) opts.unshift({ value: source, label: `${sourceName} (nur Anzeige)`, disabled: true });
  return opts;
}

// Which switches a controller card offers. A static colour has no data source, so "Helligkeit nach Datenquelle"
// is only shown when the flag is already set (it can then be switched off, never on).
function ledToggleKeys(mode, flags) {
  const keys = ["fade", "blink"];
  if (mode === "farbschalter" || flags.brightness_by_source) keys.push("brightness_by_source");
  return keys;
}

// True if the current data source is not one of the four temperature sensors: it is then only shown.
function ledSourceLocked(source, sensorCount) {
  return !(Number.isInteger(source) && source >= 0 && source < sensorCount);
}

function ledPreview(state) {
  const W = 400, X0 = 6, X1 = 394, Y = 26, H = 20;
  const bar = state.preview;
  bar.replaceChildren();
  const x = (v) => X0 + ledFraction(v, state.led.range) * (X1 - X0);
  for (const seg of ledSegments(state.led.range, state.thresholds, state.colors)) {
    bar.append(svg("rect", { x: x(seg.from), y: Y, width: Math.max(0, x(seg.to) - x(seg.from)), height: H, fill: seg.color }));
  }
  bar.append(svg("rect", { x: X0, y: Y, width: X1 - X0, height: H, fill: "none", stroke: "currentColor", "stroke-opacity": 0.35 }));
  const label = (v, anchor, cls = "") => {
    const t = svg("text", { x: x(v), y: 62, "text-anchor": anchor, class: cls });
    t.textContent = String(v);
    bar.append(t);
  };
  label(state.led.range[0], "start");
  label(state.led.range[1], "end");
  for (const t of state.thresholds) {
    if (!Number.isFinite(t)) continue;
    bar.append(svg("line", { x1: x(t), x2: x(t), y1: Y - 3, y2: Y + H + 3, stroke: "currentColor", "stroke-width": 2 }));
    label(t, "middle");
  }
  const now = state.now;
  if (now !== undefined && now !== null) {
    bar.append(svg("line", { x1: x(now), x2: x(now), y1: 17, y2: Y + H + 6, class: "now-marker" }));
    const t = svg("text", { x: Math.min(W - 24, Math.max(24, x(now))), y: 12, "text-anchor": "middle", class: "now-text" });
    t.textContent = `${now.toFixed(1)} °C`;
    bar.append(t);
  }
}

function updateLedMarkers() {
  const temps = lastStatus && lastStatus.status ? lastStatus.status.temps : null;
  for (const state of ledStates) {
    const v = temps && state.led.mode_name === "farbschalter" && state.source >= 0 && state.source < temps.length ? temps[state.source] : null;
    const now = v === null || v === undefined ? undefined : v;
    if (now !== state.now) {
      state.now = now;
      ledPreview(state);
    }
  }
}

function ledCard(led, sensorNames) {
  const isSwitch = led.mode_name === "farbschalter";
  const state = { led, thresholds: [...led.thresholds], colors: [...led.colors], source: led.source, sourceChanged: false,
    preview: svg("svg", { class: "led-bar", viewBox: "0 0 400 68", role: "img", "aria-label": "Vorschau der Farben" }) };
  ledStates.push(state);
  const msg = el("span", { class: "msg" });
  const save = el("button", { class: "primary" }, "Speichern");
  const problem = el("p", { class: "msg err" });
  const chain = el("div", { class: "led-chain" });
  const toggles = {};
  const toggleTexts = { fade: "Überblenden", blink: "Blinken", brightness_by_source: "Helligkeit nach Datenquelle" };
  for (const key of ledToggleKeys(led.mode_name, led.flags)) toggles[key] = el("input", { type: "checkbox", checked: led.flags[key] });

  const refresh = () => {
    const p = isSwitch ? ledProblem(state.thresholds, led.range) : "";
    problem.textContent = p;
    save.disabled = p !== "";
    add.disabled = state.thresholds.length >= 5 || ledNewThreshold(state.thresholds, led.range) === null;
    ledPreview(state);
  };
  const build = () => {  // rebuilt when the number of thresholds changes
    chain.replaceChildren();
    state.colors.forEach((color, k) => {
      const c = el("input", { type: "color", value: color, "aria-label": `Farbe ${k + 1}` });
      c.addEventListener("input", () => { state.colors[k] = c.value; ledPreview(state); });
      chain.append(c);
      if (isSwitch && k < state.thresholds.length) {
        const t = el("input", { type: "number", step: 1, min: led.range[0], max: led.range[1], value: state.thresholds[k], "aria-label": `Schwelle ${k + 1}` });
        t.addEventListener("input", () => { state.thresholds[k] = t.value === "" ? NaN : Number(t.value); refresh(); });
        chain.append(el("span", { class: "led-lt" }, "<"), t);
        chain.append(el("span", { class: "led-lt" }, "<"));
      }
    });
    remove.disabled = state.thresholds.length <= 1;
    refresh();
  };
  const add = el("button", {}, "+ Schwelle");
  add.addEventListener("click", () => {
    state.thresholds.push(ledNewThreshold(state.thresholds, led.range));
    state.colors.push(state.colors[state.colors.length - 1]);
    build();
  });
  const remove = el("button", {}, "− Schwelle");
  remove.addEventListener("click", () => {
    state.thresholds.pop();
    state.colors.pop();
    build();
  });

  const sourceLocked = ledSourceLocked(led.source, sensorNames.length);
  const source = el("select", { disabled: sourceLocked }, ...ledSourceOptions(sensorNames, led.source, led.source_name)
    .map((o) => el("option", { value: o.value, selected: o.value === led.source, disabled: o.disabled }, o.label)));
  source.addEventListener("change", () => { state.source = Number(source.value); state.sourceChanged = true; updateLedMarkers(); });

  save.addEventListener("click", async () => {
    const body = ledSaveBody({ mode: led.mode_name, thresholds: state.thresholds, colors: state.colors,
      fade: toggles.fade.checked, blink: toggles.blink.checked, brightness: toggles.brightness_by_source ? toggles.brightness_by_source.checked : led.flags.brightness_by_source,
      source: state.source, sourceChanged: state.sourceChanged });
    save.disabled = true;
    try {
      showMsg(msg, savedText(await api("PUT", `/api/settings/led/${led.index}`, body)), true);
    } catch (e) {
      showMsg(msg, e.message, false);
    } finally {
      refresh();
    }
  });

  const card = el("div", { class: "card led-card" },
    el("h3", {}, `${led.index}: ${led.name}`,
      el("span", { class: "hint" }, ` · LED ${led.led_start + 1}–${led.led_start + led.led_count} · ${isSwitch ? "Farbschalter" : "Statische Farbe"}`)),
    isSwitch ? state.preview : "",
    chain,
    isSwitch ? el("div", { class: "led-buttons" }, add, remove) : "",
    problem,
    el("div", { class: "led-toggles" },
      ...Object.keys(toggles).map((key) => el("label", { class: "strip-row" }, toggles[key], toggleTexts[key]))),
    isSwitch ? el("label", { class: "strip-row" }, "Datenquelle", source,
      sourceLocked ? el("span", { class: "hint" }, "Quelle nur in der Aquasuite änderbar") : "") : "",
    el("div", { class: "strip-actions" }, save, msg));
  build();
  return card;
}

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
  ledStates.length = 0;
  const readOnly = el("div", { class: "grid" });
  for (const led of settings.leds) {
    if (led.mode_name === "unbenutzt") continue;  // unused controllers are not shown
    if (led.editable) list.append(ledCard(led, settings.sensors));
    else readOnly.append(tile(`${led.index}: ${led.name}`, `LED ${led.led_start + 1}–${led.led_start + led.led_count}`, `Modus ${led.mode} (nur Anzeige)`));
  }
  if (readOnly.children.length) list.append(readOnly);
  if (!list.children.length) list.append(el("p", { class: "hint" }, "Keine LED-Controller in Benutzung."));
  updateLedMarkers();
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

// ---------------------------------------------------------------- climate
const HVAC_LABELS = { cool: "Kühlen (cool)", dry: "Entfeuchten (dry)", fan_only: "Nur Lüfter (fan_only)" };
// Everything with a fixed set of valid values is a drop-down filled from GET /api/climate/options (`list` names
// the key of that response). Only the address, numbers and the token are typed.
const CLIMATE_FIELDS = [
  { group: "Home Assistant" },
  { path: "enabled", label: "Klima-Automatik aktiviert", kind: "bool" },
  { path: "ha_url", label: "Adresse", kind: "text", placeholder: "http://homeassistant.local:8123" },
  { kind: "token", label: "Zugangs-Token" },
  { path: "entity_id", label: "Klimaanlage (Entity)", kind: "select", list: "climate_entities", wide: true, reload: true },
  { path: "horizontal_select", label: "Lamellen horizontal (Select)", kind: "select", list: "select_entities", wide: true, reload: true },
  { path: "vertical_select", label: "Lamellen vertikal (Select)", kind: "select", list: "select_entities", wide: true, reload: true },
  { group: "Einschalten, wenn alles ununterbrochen gilt" },
  { path: "on.water_c", label: "Wasser mindestens °C", kind: "number", min: 0, max: 100, step: 0.5 },
  { path: "on.fan_percent", label: "Lüfter mindestens %", kind: "number", min: 0, max: 100, step: 1 },
  { path: "on.fan_channels", label: "Lüfter (alle gewählten)", kind: "channels" },
  { path: "on.minutes", label: "für Minuten", kind: "number", min: 1, max: 240, step: 1 },
  { group: "Ausschalten, wenn alles ununterbrochen gilt" },
  { path: "off.water_c", label: "Wasser höchstens °C", kind: "number", min: 0, max: 100, step: 0.5 },
  { path: "off.minutes", label: "für Minuten", kind: "number", min: 1, max: 240, step: 1 },
  { group: "Gegen Pendeln" },
  { path: "min_on_minutes", label: "Mindestlaufzeit (Min.)", kind: "number", min: 1, max: 240, step: 1 },
  { path: "min_off_minutes", label: "Sperrzeit nach Ausschalten (Min.)", kind: "number", min: 1, max: 240, step: 1 },
  { path: "max_switches_per_hour", label: "Höchstens Schaltvorgänge pro Stunde (nur Einschalten)", kind: "number", min: 1, max: 20, step: 1 },
  { path: "manual_off_pause_minutes", label: "Pause nach Ausschalten von Hand (Min., höchstens)", kind: "number", min: 10, max: 480, step: 1 },
  { group: "Klimaanlage beim Einschalten" },
  { path: "ac.hvac_mode", label: "Betriebsart", kind: "select", list: "hvac_modes", labels: HVAC_LABELS },
  { path: "ac.temperature", label: "Solltemperatur °C", kind: "number", min: 16, max: 30, step: 0.5 },
  { path: "ac.preset", label: "Preset", kind: "select", list: "preset_modes" },
  { path: "ac.fan_mode", label: "Lüfterstufe", kind: "select", list: "fan_modes" },
  { path: "ac.horizontal", label: "Lamellen horizontal", kind: "select", list: "horizontal" },
  { path: "ac.vertical", label: "Lamellen vertikal", kind: "select", list: "vertical" },
];
const STATE_TEXT = { idle: "Bereit", arming: "Einschalt-Bedingung läuft", owned: "Von aquacontrol eingeschaltet",
  cooldown: "Pause / Sperrzeit", disabled: "Deaktiviert" };

// [value, label] pairs for a drop-down. The saved value stays selectable even if the list lacks it (marked).
function choiceOptions(list, saved, labels = {}) {
  const items = [...new Set(list)].map((v) => [v, labels[v] || v]);
  if (saved !== null && saved !== undefined && saved !== "" && !list.includes(saved)) items.unshift([saved, `${labels[saved] || saved} (gespeichert)`]);
  return items;
}

function fanChannelLabel(index, names) {
  const name = names && names[index];
  return name ? `${index + 1}: ${name}` : String(index + 1);
}

function climateOptionsNote(opts) {
  if (!opts) return "Die Auswahllisten konnten nicht geladen werden.";
  if (opts.source === "ha") return "Die Auswahl kommt aus Home Assistant." + (opts.error ? ` ${opts.error}` : "");
  return `Eingebaute Auswahl${opts.error ? `, weil Home Assistant nicht gelesen werden konnte: ${opts.error}` : ""}`;
}

function climateSavedText(res) {
  return res.notice ? `Gespeichert. ${res.notice}` : "Gespeichert.";
}

// Two-step button for dangerous actions: the first click arms it, a second click within `ms` runs `action`.
function armedClick(btn, armedText, action, ms = 5000, timers = { set: setTimeout, clear: clearTimeout }) {
  const idleText = btn.textContent, idleClass = btn.className;
  let timer = null;
  const disarm = () => { timers.clear(timer); timer = null; btn.textContent = idleText; btn.className = idleClass; };
  btn.addEventListener("click", () => {
    if (timer === null) { btn.textContent = armedText; btn.className = "danger"; timer = timers.set(disarm, ms); return; }
    disarm();
    action();
  });
}

// Request body for PUT /api/climate: a nested object built from the flat field values. Empty numbers become null
// (the server rejects them with a message) instead of silently turning into 0. The token is sent only if typed.
function climateBody(fields, values, token) {
  const body = {};
  for (const f of fields) {
    if (!f.path) continue;
    const raw = values[f.path];
    let v;
    if (f.kind === "number") v = raw === "" || raw === null || raw === undefined ? null : Number(raw);
    else if (f.kind === "bool") v = Boolean(raw);
    else if (f.kind === "channels") v = (raw || []).map(Number);
    else v = String(raw === null || raw === undefined ? "" : raw);
    const keys = f.path.split(".");
    let node = body;
    for (const key of keys.slice(0, -1)) {
      if (!node[key]) node[key] = {};
      node = node[key];
    }
    node[keys[keys.length - 1]] = v;
  }
  if (token) body.token = token;
  return body;
}

function fmtDuration(seconds) {
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s} s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} min`;
  return `${Math.floor(m / 60)} h ${String(m % 60).padStart(2, "0")} min`;
}

// Text lines for the timers of the climate status (`now` in epoch seconds).
function climateTimers(st, now) {
  const lines = [];
  if (st.arming_since !== null && st.arming_since !== undefined) lines.push(`Einschalt-Bedingung erfüllt seit ${fmtDuration(now - st.arming_since)}`);
  if (st.owned_since !== null && st.owned_since !== undefined) lines.push(`Läuft seit ${fmtDuration(now - st.owned_since)}`);
  if (st.off_condition_since !== null && st.off_condition_since !== undefined) lines.push(`Ausschalt-Bedingung erfüllt seit ${fmtDuration(now - st.off_condition_since)}`);
  if (st.cooldown_until !== null && st.cooldown_until !== undefined && st.cooldown_until > now) lines.push(`Sperrzeit noch ${fmtDuration(st.cooldown_until - now)}`);
  if (st.manual_off_until !== null && st.manual_off_until !== undefined) {
    const until = new Date(st.manual_off_until * 1000).toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit" });
    lines.push(`Von Hand ausgeschaltet: Automatik pausiert bis das Wasser wieder kühl ist, spätestens ${until}`);
  }
  if (st.unconfirmed) lines.push("Einschalten unbestätigt: Home Assistant zeigt die eingestellten Werte noch nicht");
  lines.push(`Eigene Schaltvorgänge in der letzten Stunde: ${st.switches_last_hour ?? 0}`);
  if (st.last_error) lines.push(`Letzter Fehler: ${st.last_error}`);
  return lines;
}

function climateTestText(res) {
  const v = (x) => (x === null || x === undefined ? "–" : x);
  return `Verbunden. Zustand: ${v(res.state)}, Soll ${v(res.temperature)} °C, Preset ${v(res.preset)}, Lüfter ${v(res.fan_mode)}, `
    + `Lamellen horizontal ${v(res.horizontal)} / vertikal ${v(res.vertical)}`;
}

const climateInputs = {};  // field path -> { read(), write(value), refill() }
let climateTokenInput = null;
let climateFormBuilt = false;
let climateOptions = null;  // last answer of GET /api/climate/options

function buildClimateForm() {
  const form = $("#climate-form");
  form.replaceChildren();
  for (const f of CLIMATE_FIELDS) {
    if (f.group) { form.append(el("h4", {}, f.group)); continue; }
    if (f.kind === "token") {
      climateTokenInput = el("input", { type: "password", autocomplete: "new-password", "aria-label": f.label });
      const clear = el("button", { type: "button", class: "danger", id: "climate-token-clear", hidden: true }, "Token löschen");
      armedClick(clear, "Wirklich löschen?", async () => {
        try { renderClimate(await api("PUT", "/api/climate", { token: "" })); showMsg($("#climate-msg"), "Token gelöscht.", true); }
        catch (e) { showMsg($("#climate-msg"), e.message, false); }
      });
      form.append(el("label", {}, f.label, climateTokenInput), clear);
      continue;
    }
    let input;
    if (f.kind === "bool") {
      input = el("input", { type: "checkbox" });
      climateInputs[f.path] = { read: () => input.checked, write: (v) => { input.checked = Boolean(v); } };
    } else if (f.kind === "select") {
      input = el("select", { class: f.wide ? "wide" : null });
      let saved = null;
      const fill = () => {
        const current = input.value;
        input.replaceChildren(...choiceOptions((climateOptions && climateOptions[f.list]) || [], saved, f.labels).map(([v, t]) => el("option", { value: v }, t)));
        if ([...input.options].some((o) => o.value === current)) input.value = current;
      };
      climateInputs[f.path] = { read: () => input.value, write: (v) => { saved = v; fill(); input.value = String(v); }, refill: fill };
      if (f.reload) input.addEventListener("change", reloadClimateOptions);
    } else if (f.kind === "channels") {
      const boxes = [1, 2, 3, 4].map((n) => el("input", { type: "checkbox", value: n }));
      const texts = boxes.map((_, i) => el("span", {}, fanChannelLabel(i, null)));
      input = el("span", { class: "channels" }, ...boxes.map((b, i) => el("label", {}, b, texts[i])));
      climateInputs[f.path] = {
        read: () => boxes.filter((b) => b.checked).map((b) => Number(b.value)),
        write: (v) => boxes.forEach((b) => { b.checked = (v || []).includes(Number(b.value)); }),
        names: (names) => texts.forEach((t, i) => { t.textContent = fanChannelLabel(i, names); }),
      };
    } else {
      const attrs = f.kind === "number" ? { type: "number", min: f.min, max: f.max, step: f.step } : { type: "text", placeholder: f.placeholder || null, autocomplete: "off", spellcheck: "false" };
      input = el("input", attrs);
      climateInputs[f.path] = { read: () => input.value, write: (v) => { input.value = v === null || v === undefined ? "" : v; } };
    }
    form.append(el("label", {}, f.label, input));
  }
  climateFormBuilt = true;
}

// Fetch the choices again (for the entities currently picked in the form) and refill every drop-down.
async function reloadClimateOptions() {
  const params = new URLSearchParams();
  for (const key of ["entity_id", "horizontal_select", "vertical_select"]) {
    if (climateFormBuilt && climateInputs[key] && climateInputs[key].read()) params.set(key, climateInputs[key].read());
  }
  try {
    climateOptions = await api("GET", `/api/climate/options?${params}`);
  } catch (e) {
    climateOptions = null;
  }
  for (const io of Object.values(climateInputs)) if (io.refill) io.refill();
  $("#climate-options-note").textContent = climateOptionsNote(climateOptions);
}

function renderClimate(data) {
  if (!climateFormBuilt) buildClimateForm();
  for (const f of CLIMATE_FIELDS) {
    if (f.path) climateInputs[f.path].write(f.path.split(".").reduce((o, k) => o[k], data.config));
  }
  climateInputs["on.fan_channels"].names(lastStatus ? lastStatus.fan_names : null);
  $("#climate-options-note").textContent = climateOptionsNote(climateOptions);
  climateTokenInput.value = "";
  climateTokenInput.placeholder = data.token_set ? "gesetzt — leer lassen = unverändert" : "Long-Lived Access Token aus Home Assistant";
  $("#climate-token-clear").hidden = !data.token_set;
  renderClimateStatus(data.status);
}

function renderClimateStatus(st) {
  const tone = st.state === "owned" ? "on" : "";
  $("#climate-status").replaceChildren(
    el("div", { class: "climate-state" }, el("span", { class: `pill ${tone}` }, STATE_TEXT[st.state] || st.state), el("span", {}, st.reason || "")),
    el("ul", { class: "climate-lines" }, ...climateTimers(st, Date.now() / 1000).map((l) => el("li", {}, l))));
  const tbody = $("#climate-events tbody");
  tbody.replaceChildren();
  for (const ev of st.events || []) {
    tbody.append(el("tr", {}, el("td", {}, new Date(ev.t * 1000).toLocaleString("de-DE")), el("td", {}, ev.message)));
  }
  if (!(st.events || []).length) tbody.append(el("tr", {}, el("td", { colspan: 2 }, "Noch keine Ereignisse.")));
}

loaders.climate = async () => {
  try {
    if (!lastStatus) await refreshStatus();  // the fan names label the channel boxes
    const [data, options] = await Promise.all([api("GET", "/api/climate"), api("GET", "/api/climate/options").catch(() => null)]);
    climateOptions = options;
    renderClimate(data);
  } catch (e) {
    showMsg($("#climate-msg"), `Klima-Einstellungen nicht lesbar: ${e.message}`, false);
  }
};

async function refreshClimateStatus() {
  if ($("#tab-climate").hidden || document.hidden) return;
  try {
    const data = await api("GET", "/api/climate");
    renderClimateStatus(data.status);  // the form stays untouched: it may hold unsaved edits
  } catch { /* the next cycle tries again */ }
}

$("#climate-save").addEventListener("click", async () => {
  if (!climateFormBuilt) return;
  const values = {};
  for (const [path, io] of Object.entries(climateInputs)) values[path] = io.read();
  const btn = $("#climate-save");
  btn.disabled = true;
  try {
    const res = await api("PUT", "/api/climate", climateBody(CLIMATE_FIELDS, values, climateTokenInput.value.trim()));
    renderClimate(res);
    showMsg($("#climate-msg"), climateSavedText(res), !res.notice);
    await reloadClimateOptions();  // address or token may have changed
  } catch (e) {
    showMsg($("#climate-msg"), e.message, false);
  } finally {
    btn.disabled = false;
  }
});

$("#climate-options-reload").addEventListener("click", reloadClimateOptions);

$("#climate-test").addEventListener("click", async () => {
  const btn = $("#climate-test");
  const out = $("#climate-test-result");
  btn.disabled = true;
  showMsg(out, "Teste …", true);
  try {
    showMsg(out, climateTestText(await api("POST", "/api/climate/test", {})), true);
  } catch (e) {
    showMsg(out, e.message, false);
  } finally {
    btn.disabled = false;
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
    armedClick(btn, "Wirklich wiederherstellen?", async () => {
      btn.disabled = true;
      try {
        showMsg($("#backup-msg"), savedText(await api("POST", `/api/backups/${encodeURIComponent(b.name)}/restore`, {})), true);
        loaders.backups();
      } catch (e) {
        showMsg($("#backup-msg"), e.message, false);
        btn.disabled = false;
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
setInterval(refreshClimateStatus, 10000);
