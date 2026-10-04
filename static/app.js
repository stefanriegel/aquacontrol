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

// ---------------------------------------------------------------- climate
const HVAC_OPTIONS = [["cool", "Kühlen (cool)"], ["dry", "Entfeuchten (dry)"], ["fan_only", "Nur Lüfter (fan_only)"]];
const same = (list) => list.map((v) => [v, v]);
const CLIMATE_FIELDS = [
  { group: "Home Assistant" },
  { path: "enabled", label: "Klima-Automatik aktiviert", kind: "bool" },
  { path: "ha_url", label: "Adresse", kind: "text", placeholder: "http://homeassistant.local:8123" },
  { kind: "token", label: "Zugangs-Token" },
  { path: "entity_id", label: "Klimaanlage (Entity)", kind: "text" },
  { path: "horizontal_select", label: "Lamellen horizontal (Select)", kind: "text" },
  { path: "vertical_select", label: "Lamellen vertikal (Select)", kind: "text" },
  { group: "Einschalten, wenn alles ununterbrochen gilt" },
  { path: "on.water_c", label: "Wasser mindestens °C", kind: "number", min: 0, max: 100, step: 0.5 },
  { path: "on.fan_percent", label: "Lüfter mindestens %", kind: "number", min: 0, max: 100, step: 1 },
  { path: "on.fan_channels", label: "Lüfterkanäle (alle)", kind: "channels" },
  { path: "on.minutes", label: "für Minuten", kind: "number", min: 1, max: 240, step: 1 },
  { group: "Ausschalten, wenn alles ununterbrochen gilt" },
  { path: "off.water_c", label: "Wasser höchstens °C", kind: "number", min: 0, max: 100, step: 0.5 },
  { path: "off.minutes", label: "für Minuten", kind: "number", min: 1, max: 240, step: 1 },
  { group: "Gegen Pendeln" },
  { path: "min_on_minutes", label: "Mindestlaufzeit (Min.)", kind: "number", min: 1, max: 240, step: 1 },
  { path: "min_off_minutes", label: "Sperrzeit nach Ausschalten (Min.)", kind: "number", min: 1, max: 240, step: 1 },
  { path: "max_switches_per_hour", label: "Höchstens Schaltvorgänge pro Stunde (nur Einschalten)", kind: "number", min: 1, max: 20, step: 1 },
  { group: "Klimaanlage beim Einschalten" },
  { path: "ac.hvac_mode", label: "Betriebsart", kind: "select", options: HVAC_OPTIONS },
  { path: "ac.temperature", label: "Solltemperatur °C", kind: "number", min: 16, max: 30, step: 0.5 },
  { path: "ac.preset", label: "Preset", kind: "select", options: same(["Normal", "Quiet", "Powerful"]) },
  { path: "ac.fan_mode", label: "Lüfterstufe", kind: "select", options: same(["Automatic", "1", "2", "3", "4", "5"]) },
  { path: "ac.horizontal", label: "Lamellen horizontal", kind: "select",
    options: same(["auto", "left", "left_center", "center", "right_center", "right"]) },
  { path: "ac.vertical", label: "Lamellen vertikal", kind: "select",
    options: same(["swing", "auto", "up", "up_center", "center", "down_center", "down"]) },
];
const STATE_TEXT = { idle: "Bereit", arming: "Einschalt-Bedingung läuft", owned: "Von aquacontrol eingeschaltet",
  cooldown: "Sperrzeit", disabled: "Deaktiviert" };

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
  lines.push(`Eigene Schaltvorgänge in der letzten Stunde: ${st.switches_last_hour ?? 0}`);
  if (st.last_error) lines.push(`Letzter Fehler: ${st.last_error}`);
  return lines;
}

function climateTestText(res) {
  const v = (x) => (x === null || x === undefined ? "–" : x);
  return `Verbunden. Zustand: ${v(res.state)}, Soll ${v(res.temperature)} °C, Preset ${v(res.preset)}, Lüfter ${v(res.fan_mode)}, `
    + `Lamellen horizontal ${v(res.horizontal)} / vertikal ${v(res.vertical)}`;
}

const climateInputs = {};  // field path -> { read(), write(value) }
let climateTokenInput = null;
let climateFormBuilt = false;

function buildClimateForm() {
  const form = $("#climate-form");
  form.replaceChildren();
  for (const f of CLIMATE_FIELDS) {
    if (f.group) { form.append(el("h4", {}, f.group)); continue; }
    if (f.kind === "token") {
      climateTokenInput = el("input", { type: "password", autocomplete: "new-password", "aria-label": f.label });
      const clear = el("button", { type: "button", class: "danger", id: "climate-token-clear", hidden: true }, "Token löschen");
      let armed = false;
      clear.addEventListener("click", async () => {
        if (!armed) { armed = true; clear.textContent = "Wirklich löschen?"; return; }
        armed = false;
        clear.textContent = "Token löschen";
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
      input = el("select", {}, ...f.options.map(([v, t]) => el("option", { value: v }, t)));
      climateInputs[f.path] = { read: () => input.value, write: (v) => { input.value = String(v); } };
    } else if (f.kind === "channels") {
      const boxes = [1, 2, 3, 4].map((n) => el("input", { type: "checkbox", value: n }));
      input = el("span", { class: "channels" }, ...boxes.map((b, i) => el("label", {}, b, String(i + 1))));
      climateInputs[f.path] = {
        read: () => boxes.filter((b) => b.checked).map((b) => Number(b.value)),
        write: (v) => boxes.forEach((b) => { b.checked = (v || []).includes(Number(b.value)); }),
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

function renderClimate(data) {
  if (!climateFormBuilt) buildClimateForm();
  for (const f of CLIMATE_FIELDS) {
    if (f.path) climateInputs[f.path].write(f.path.split(".").reduce((o, k) => o[k], data.config));
  }
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
    renderClimate(await api("GET", "/api/climate"));
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
    renderClimate(await api("PUT", "/api/climate", climateBody(CLIMATE_FIELDS, values, climateTokenInput.value.trim())));
    showMsg($("#climate-msg"), "Gespeichert.", true);
  } catch (e) {
    showMsg($("#climate-msg"), e.message, false);
  } finally {
    btn.disabled = false;
  }
});

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
    let armed = false;
    btn.addEventListener("click", async () => {
      if (!armed) { armed = true; btn.textContent = "Wirklich wiederherstellen?"; btn.className = "danger"; return; }
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
