/*
  web/ops.js - behavior of the BusPulse operator dashboard.
  Inputs: /api/ops, /api/meta, /api/live/status, /api/network/segments, /api/network/speeds.
  Outputs: KPI tiles, speed-through-the-day chart (inline SVG), speed map with chokepoints
  and an hour slider, timetable audit with a corrected-timetable CSV download,
  reliability and bunching tables.
*/
"use strict";

const API_OPS = "/api/ops";
const API_META = "/api/meta";
const API_LIVE_STATUS = "/api/live/status";
const API_SEGMENTS = "/api/network/segments";
const API_SPEEDS = "/api/network/speeds";
const SAMPLE_WEEKDAY = "2026-10-27";
const SPEED_CLASSES_KMH = [8, 12, 18, 25];
const SPEED_COLORS = ["#D7263D", "#F26B38", "#F5A623", "#9BC53D", "#22A559"];
const DAY_COLORS = ["#2563EB", "#F59E0B", "#9CA3AF"];
const DAY_NAMES = ["Weekday", "Saturday", "Sunday / holiday"];
const CHART_W = 1200;
const CHART_H = 300;
const CHART_PAD = 44;
const CHART_PAD_LEFT = 84;
const DETAIL_W = 620;
const DETAIL_H = 240;
const MAP_CENTER = [28.6139, 77.2090];
const MAP_ZOOM = 11;
const NET_WEIGHT = 2;
const CHOKE_WEIGHT = 9;
const CHOKE_SHOW = 10;
const ROWS_SHOWN = 15;
const SLIDER_DEBOUNCE_MS = 150;

let opsData = null;
let opsMap = null;
let netLayer = null;
let chokeLayer = null;
let segments = null;
let hourTimer = null;

function byId(id) {
  return document.getElementById(id);
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) {
    node.className = className;
  }
  if (text !== undefined && text !== null) {
    node.textContent = text;
  }
  return node;
}

function getJson(url) {
  return fetch(url).then(function toJson(response) {
    return response.json();
  });
}

function speedColor(speed) {
  for (let i = 0; i < SPEED_CLASSES_KMH.length; i++) {
    if (speed < SPEED_CLASSES_KMH[i]) {
      return SPEED_COLORS[i];
    }
  }
  return SPEED_COLORS[SPEED_COLORS.length - 1];
}

function svgNode(tag, attrs) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  const names = Object.keys(attrs);
  for (let i = 0; i < names.length; i++) {
    node.setAttribute(names[i], attrs[names[i]]);
  }
  return node;
}

function bucketLabel(bucket) {
  const minutes = bucket * 30;
  return String(Math.floor(minutes / 60)).padStart(2, "0") + ":" + String(minutes % 60).padStart(2, "0");
}

// ---------------------------------------------------------------------------
// KPIs
// ---------------------------------------------------------------------------

function kpi(value, label) {
  const box = el("div", "kpi");
  box.appendChild(el("div", "kpi-value", value));
  box.appendChild(el("div", "kpi-label", label));
  return box;
}

function renderKpis(meta) {
  const box = byId("kpis");
  box.innerHTML = "";
  const weekday = opsData.city_speed[0];
  let fastB = -1;
  let slowB = -1;
  for (let b = 12; b <= 44; b++) {
    if (weekday[b] === null) {
      continue;
    }
    if (fastB < 0 || weekday[b] > weekday[fastB]) {
      fastB = b;
    }
    if (slowB < 0 || weekday[b] < weekday[slowB]) {
      slowB = b;
    }
  }
  const drop = Math.round((1 - weekday[slowB] / weekday[fastB]) * 100);
  box.appendChild(kpi(weekday[slowB].toFixed(1) + " km/h",
    "Slowest weekday bus speed (" + bucketLabel(slowB) + "), vs " + weekday[fastB].toFixed(1) +
    " at " + bucketLabel(fastB) + ": " + drop + "% slower"));
  let offCount = 0;
  for (let i = 0; i < opsData.audit.length; i++) {
    if (Math.abs(opsData.audit[i].peak_gap_pct) >= 20) {
      offCount += 1;
    }
  }
  box.appendChild(kpi(String(opsData.audit_count), "Routes audited against GPS; the worst " +
    offCount + " shown below are off by 20%+ at peak"));
  if (meta.validation) {
    box.appendChild(kpi(meta.validation.mae_model_min.toFixed(1) + " min",
      "Typical error of BusPulse trip times, vs " + meta.validation.mae_timetable_min.toFixed(1) +
      " min for the timetable (held-out week)"));
  }
  const chokes = opsData.chokepoints["18"];
  if (chokes && chokes.length > 0) {
    box.appendChild(kpi(Math.round(chokes[0].bus_minutes_lost_per_hour) + " bus-min/h",
      "Worst 18:00 chokepoint: " + chokes[0].from_stop + " to " + chokes[0].to_stop));
  }
}

function renderLiveChip(status) {
  const chip = byId("liveChip");
  if (status && status.enabled && status.ready) {
    chip.className = "chip chip-live";
    chip.textContent = "Live feed: " + status.vehicles + " buses reporting · updated " + status.updated_at;
  } else if (status && status.enabled && status.connected) {
    chip.className = "chip chip-live";
    chip.textContent = "Live feed: " + status.vehicles + " buses reporting · warming up";
  } else {
    chip.className = "chip chip-grey";
    chip.textContent = "Live feed: not connected";
  }
}

// ---------------------------------------------------------------------------
// Speed through the day (inline SVG line chart)
// ---------------------------------------------------------------------------

function renderSpeedChart() {
  const holder = byId("speedChart");
  holder.innerHTML = "";
  const svg = svgNode("svg", { viewBox: "0 0 " + CHART_W + " " + CHART_H });
  const series = opsData.city_speed;
  let maxV = 0;
  let minV = 999;
  for (let d = 0; d < series.length; d++) {
    for (let b = 10; b < 47; b++) {
      if (series[d][b] !== null) {
        maxV = Math.max(maxV, series[d][b]);
        minV = Math.min(minV, series[d][b]);
      }
    }
  }
  const lo = Math.floor(minV / 5) * 5;
  const hi = Math.ceil(maxV / 5) * 5;
  function xOf(b) {
    return CHART_PAD_LEFT + (b - 10) / 36 * (CHART_W - CHART_PAD_LEFT - CHART_PAD);
  }
  function yOf(v) {
    return CHART_H - CHART_PAD + 10 - (v - lo) / (hi - lo) * (CHART_H - 2 * CHART_PAD);
  }
  // Grid and axis labels.
  for (let v = lo; v <= hi; v += 5) {
    svg.appendChild(svgNode("line", { x1: CHART_PAD_LEFT, x2: CHART_W - CHART_PAD, y1: yOf(v), y2: yOf(v), stroke: "#F3F4F6" }));
    const label = svgNode("text", { x: CHART_PAD_LEFT - 10, y: yOf(v) + 4, "text-anchor": "end", "font-size": 12, fill: "#6B7280" });
    label.textContent = v + " km/h";
    svg.appendChild(label);
  }
  for (let b = 10; b <= 46; b += 4) {
    const label = svgNode("text", { x: xOf(b), y: CHART_H - 8, "text-anchor": "middle", "font-size": 12, fill: "#6B7280" });
    label.textContent = bucketLabel(b);
    svg.appendChild(label);
  }
  // One line per day type.
  for (let d = 0; d < series.length; d++) {
    let path = "";
    for (let b = 10; b < 47; b++) {
      if (series[d][b] === null) {
        continue;
      }
      let command = "L";
      if (path === "") {
        command = "M";
      }
      path += command + xOf(b).toFixed(1) + " " + yOf(series[d][b]).toFixed(1) + " ";
    }
    svg.appendChild(svgNode("path", { d: path, fill: "none", stroke: DAY_COLORS[d], "stroke-width": 3 }));
    const legend = svgNode("text", { x: CHART_W - CHART_PAD - 150, y: 22 + d * 18, "font-size": 13, fill: DAY_COLORS[d], "font-weight": 600 });
    legend.textContent = DAY_NAMES[d];
    svg.appendChild(legend);
  }
  holder.appendChild(svg);
}

// ---------------------------------------------------------------------------
// Speed map with chokepoints
// ---------------------------------------------------------------------------

function initMap() {
  opsMap = L.map("opsMap", { preferCanvas: true }).setView(MAP_CENTER, MAP_ZOOM);
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: "&copy; OpenStreetMap contributors",
  }).addTo(opsMap);
  netLayer = L.layerGroup().addTo(opsMap);
  chokeLayer = L.layerGroup().addTo(opsMap);
}

function renderHour(hour) {
  byId("hourValue").textContent = String(hour).padStart(2, "0") + ":00";
  const departAt = SAMPLE_WEEKDAY + "T" + String(hour).padStart(2, "0") + ":00";
  getJson(API_SPEEDS + "?depart_at=" + encodeURIComponent(departAt)).then(function draw(data) {
    netLayer.clearLayers();
    for (let g = 0; g < segments.length; g++) {
      if (!segments[g]) {
        continue;
      }
      L.polyline(segments[g], {
        color: speedColor(data.speeds_kmh[g]),
        weight: NET_WEIGHT,
        opacity: 0.75,
        interactive: false,
      }).addTo(netLayer);
    }
  });
  renderChokepoints(hour);
}

function renderChokepoints(hour) {
  chokeLayer.clearLayers();
  const rows = opsData.chokepoints[String(hour)] || [];
  const table = byId("chokeTable");
  table.innerHTML = "";
  const head = el("tr");
  const titles = ["#", "Stretch", "Speed", "Usual", "Bus-min lost / h"];
  for (let i = 0; i < titles.length; i++) {
    head.appendChild(el("th", "", titles[i]));
  }
  table.appendChild(head);
  for (let i = 0; i < rows.length; i++) {
    const row = rows[i];
    if (i < CHOKE_SHOW) {
      const line = L.polyline(row.coords, { color: "#111827", weight: CHOKE_WEIGHT, opacity: 0.85 });
      line.bindTooltip((i + 1) + ". " + row.from_stop + " to " + row.to_stop + ": " + row.speed_kmh +
        " km/h (usually " + row.quiet_speed_kmh + ")");
      line.addTo(chokeLayer);
      L.polyline(row.coords, { color: "#D7263D", weight: 5, opacity: 1, interactive: false }).addTo(chokeLayer);
    }
    const tr = el("tr", "click");
    tr.appendChild(el("td", "muted", String(i + 1)));
    const name = el("td");
    name.appendChild(el("div", "", row.from_stop));
    name.appendChild(el("div", "muted", "to " + row.to_stop + " · " + row.buses_per_hour + " buses/h"));
    tr.appendChild(name);
    tr.appendChild(el("td", "num bad", row.speed_kmh + " km/h"));
    tr.appendChild(el("td", "num", row.quiet_speed_kmh + " km/h"));
    tr.appendChild(el("td", "num", String(Math.round(row.bus_minutes_lost_per_hour))));
    tr.addEventListener("click", makeZoom(row.coords));
    table.appendChild(tr);
  }
}

function makeZoom(coords) {
  return function zoomTo() {
    opsMap.fitBounds(L.latLngBounds(coords), { padding: [80, 80], maxZoom: 16 });
  };
}

// ---------------------------------------------------------------------------
// Timetable audit
// ---------------------------------------------------------------------------

function renderAudit() {
  const table = byId("auditTable");
  table.innerHTML = "";
  const head = el("tr");
  const titles = ["Route", "Peak timetable", "Peak reality", "Gap"];
  for (let i = 0; i < titles.length; i++) {
    head.appendChild(el("th", "", titles[i]));
  }
  table.appendChild(head);
  for (let i = 0; i < opsData.audit.length; i++) {
    const row = opsData.audit[i];
    const peakIdx = [];
    for (let h = 0; h < row.hours.length; h++) {
      if ([8, 9, 10, 17, 18, 19].indexOf(row.hours[h]) >= 0) {
        peakIdx.push(h);
      }
    }
    let sched = 0;
    let real = 0;
    for (let k = 0; k < peakIdx.length; k++) {
      sched += row.timetable_min[peakIdx[k]];
      real += row.model_min[peakIdx[k]];
    }
    sched = sched / peakIdx.length;
    real = real / peakIdx.length;
    const tr = el("tr", "click");
    const name = el("td");
    name.appendChild(el("span", "route-chip", row.route));
    name.appendChild(el("div", "muted", row.from_stop + " to " + row.to_stop));
    tr.appendChild(name);
    tr.appendChild(el("td", "num", Math.round(sched) + " min"));
    tr.appendChild(el("td", "num", Math.round(real) + " min"));
    let gapClass = "num good";
    let sign = "";
    if (row.peak_gap_min > 0) {
      gapClass = "num bad";
      sign = "+";
    }
    tr.appendChild(el("td", gapClass, sign + Math.round(row.peak_gap_min) + " min (" + sign + Math.round(row.peak_gap_pct) + "%)"));
    tr.addEventListener("click", makeAuditHandler(i, tr));
    table.appendChild(tr);
  }
  if (opsData.audit.length > 0) {
    showAuditDetail(0, table.children[1]);
  }
}

function makeAuditHandler(index, tr) {
  return function onClick() {
    showAuditDetail(index, tr);
  };
}

function showAuditDetail(index, tr) {
  const rows = byId("auditTable").querySelectorAll("tr.click");
  for (let i = 0; i < rows.length; i++) {
    rows[i].classList.remove("active");
  }
  tr.classList.add("active");
  const row = opsData.audit[index];
  const box = byId("auditDetail");
  box.innerHTML = "";
  box.appendChild(el("h3", "", "Route " + row.route + ": " + row.from_stop + " to " + row.to_stop));
  box.appendChild(el("div", "muted", row.length_km + " km · " + row.trips_per_day + " scheduled trips a day · " +
    Math.round(row.gps_share * 100) + "% of stretches measured from GPS"));
  // Two lines: timetable and observed, per hour.
  const svg = svgNode("svg", { viewBox: "0 0 " + DETAIL_W + " " + DETAIL_H });
  let hi = 0;
  for (let h = 0; h < row.hours.length; h++) {
    hi = Math.max(hi, row.model_min[h], row.timetable_min[h]);
  }
  let step = 10;
  if (hi > 120) {
    step = 30;
  } else if (hi > 60) {
    step = 20;
  }
  hi = Math.ceil(hi / step) * step;
  function xOf(h) {
    return 40 + h / (row.hours.length - 1) * (DETAIL_W - 60);
  }
  function yOf(v) {
    return DETAIL_H - 30 - v / hi * (DETAIL_H - 50);
  }
  for (let v = 0; v <= hi; v += step) {
    svg.appendChild(svgNode("line", { x1: 40, x2: DETAIL_W - 20, y1: yOf(v), y2: yOf(v), stroke: "#F3F4F6" }));
    const label = svgNode("text", { x: 34, y: yOf(v) + 4, "text-anchor": "end", "font-size": 11, fill: "#6B7280" });
    label.textContent = String(v);
    svg.appendChild(label);
  }
  const lines = [["timetable_min", "#9CA3AF", "Timetable"], ["model_min", "#2563EB", "Observed (GPS)"]];
  for (let k = 0; k < lines.length; k++) {
    let path = "";
    for (let h = 0; h < row.hours.length; h++) {
      let command = "L";
      if (h === 0) {
        command = "M";
      }
      path += command + xOf(h).toFixed(1) + " " + yOf(row[lines[k][0]][h]).toFixed(1) + " ";
    }
    svg.appendChild(svgNode("path", { d: path, fill: "none", stroke: lines[k][1], "stroke-width": 3 }));
    const legend = svgNode("text", { x: 50, y: 16 + k * 16, "font-size": 12, fill: lines[k][1], "font-weight": 600 });
    legend.textContent = lines[k][2];
    svg.appendChild(legend);
  }
  for (let h = 0; h < row.hours.length; h += 3) {
    const label = svgNode("text", { x: xOf(h), y: DETAIL_H - 10, "text-anchor": "middle", "font-size": 11, fill: "#6B7280" });
    label.textContent = String(row.hours[h]).padStart(2, "0") + ":00";
    svg.appendChild(label);
  }
  box.appendChild(svg);
  const button = el("button", "download", "Download corrected run times (CSV)");
  button.type = "button";
  button.addEventListener("click", function onDownload() {
    downloadAudit(row);
  });
  box.appendChild(button);
}

function downloadAudit(row) {
  let csv = "route,from_stop,to_stop,hour,timetable_min,observed_min\n";
  for (let h = 0; h < row.hours.length; h++) {
    csv += '"' + row.route + '","' + row.from_stop + '","' + row.to_stop + '",' +
      String(row.hours[h]).padStart(2, "0") + ":00," + row.timetable_min[h] + "," + row.model_min[h] + "\n";
  }
  const blob = new Blob([csv], { type: "text/csv" });
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = "runtimes_" + row.route + ".csv";
  link.click();
}

// ---------------------------------------------------------------------------
// Reliability and bunching
// ---------------------------------------------------------------------------

function renderSimpleTable(id, titles, rows, cells) {
  const table = byId(id);
  table.innerHTML = "";
  const head = el("tr");
  for (let i = 0; i < titles.length; i++) {
    head.appendChild(el("th", "", titles[i]));
  }
  table.appendChild(head);
  const count = Math.min(rows.length, ROWS_SHOWN);
  for (let i = 0; i < count; i++) {
    table.appendChild(cells(rows[i]));
  }
}

function reliabilityRow(row) {
  const tr = el("tr");
  const name = el("td");
  name.appendChild(el("span", "route-chip", row.route));
  name.appendChild(el("div", "muted", "towards " + row.headsign));
  tr.appendChild(name);
  tr.appendChild(el("td", "num", Math.round(row.mean_trip_min) + " min"));
  tr.appendChild(el("td", "num bad", "+" + Math.round(row.late_min_on_bad_day) + " min"));
  tr.appendChild(el("td", "num", String(row.trips)));
  return tr;
}

function bunchingRow(row) {
  const tr = el("tr");
  const name = el("td");
  name.appendChild(el("span", "route-chip", row.route));
  name.appendChild(el("div", "muted", "towards " + row.headsign));
  tr.appendChild(name);
  tr.appendChild(el("td", "num", row.bunched_pct + "%"));
  tr.appendChild(el("td", "num bad", row.long_gap_pct + "%"));
  tr.appendChild(el("td", "num", String(row.gaps)));
  return tr;
}

// ---------------------------------------------------------------------------
// Start-up
// ---------------------------------------------------------------------------

function init() {
  initMap();
  Promise.all([getJson(API_OPS), getJson(API_META), getJson(API_SEGMENTS)]).then(function ready(results) {
    opsData = results[0];
    const meta = results[1];
    segments = results[2].segments;
    renderKpis(meta);
    renderSpeedChart();
    renderHour(Number(byId("hourSlider").value));
    renderAudit();
    renderSimpleTable("relTable", ["Route", "Usual trip", "Bad day", "Trips"], opsData.reliability, reliabilityRow);
    renderSimpleTable("bunchTable", ["Route", "Bunched", "Long gaps", "Gaps seen"], opsData.bunching, bunchingRow);
  });
  getJson(API_LIVE_STATUS).then(renderLiveChip);
  byId("hourSlider").addEventListener("input", function onHour() {
    const hour = Number(byId("hourSlider").value);
    byId("hourValue").textContent = String(hour).padStart(2, "0") + ":00";
    clearTimeout(hourTimer);
    hourTimer = setTimeout(function apply() {
      renderHour(hour);
    }, SLIDER_DEBOUNCE_MS);
  });
}

document.addEventListener("DOMContentLoaded", init);
