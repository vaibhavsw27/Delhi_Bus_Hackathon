/*
  web/app.js - all behavior of the BusPulse Delhi rider page.
  Inputs: /api/meta, /api/stops/search, /api/plan, /api/live/status, /api/live/vehicles,
  /api/network/segments, /api/network/speeds; Leaflet global L.
  Outputs: search form with autocomplete, pick mode and "my location"; leave-at /
  arrive-by; slider re-query; typical / live switch (real feed today, replay on 27 Oct);
  plain-language journey cards; speed-colored map, live bus dots, city traffic layer;
  English / Hindi.
*/
"use strict";

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------
const API_META = "/api/meta";
const API_SEARCH = "/api/stops/search";
const API_PLAN = "/api/plan";
const API_LIVE_STATUS = "/api/live/status";
const API_LIVE_VEHICLES = "/api/live/vehicles";
const API_SEGMENTS = "/api/network/segments";
const API_SPEEDS = "/api/network/speeds";
const MAP_CENTER = [28.6139, 77.2090];
const MAP_ZOOM = 11;
const FIT_PADDING_PX = 60;
const SEARCH_MIN_CHARS = 2;
const SEARCH_DEBOUNCE_MS = 150;
const SEARCH_LIMIT = 8;
const SLIDER_DEBOUNCE_MS = 120;
const SLIDER_MIN_MIN = 300;
const SLIDER_MAX_MIN = 1410;
const SLIDER_STEP_MIN = 30;
const TICK_HOURS = [6, 9, 12, 15, 18, 21];
const REPLAY_DATE_DEFAULT = "2026-10-27";
const DEFAULT_TIME = "08:30";
const DATE_DAYS_AHEAD = 90;
const TIME_ROUND_MIN = 5;
const LAST_TIME_MIN = 23 * 60 + 55;
const DEMO_DAY_START_MIN = 300;
const DEMO_DAY_END_MIN = 1320;
const DEMO_FALLBACK_TIME = "08:30";
const HOLIDAYS = ["2026-10-02", "2026-10-20"];
const SPEED_CLASSES_KMH = [8, 12, 18, 25];
const SPEED_COLORS = ["#D7263D", "#F26B38", "#F5A623", "#9BC53D", "#22A559"];
const LEVEL_SCORES = [0, 1.0, 0.8, 0.6, 0.4, 0.2];
const CONF_HIGH = 0.75;
const CONF_MEDIUM = 0.45;
const TRAFFIC_SHOW_MIN = 3;
const TRAFFIC_HEAVY_MIN = 10;
const LIVE_SHOW_MIN = 2;
const CASING_COLOR = "#111827";
const CASING_WEIGHT = 9;
const CASING_OPACITY = 0.85;
const HOP_WEIGHT = 5;
const WALK_COLOR = "#4B5563";
const WALK_DASH = "6 8";
const WALK_WEIGHT = 4;
const OTHER_COLOR = "#9CA3AF";
const OTHER_WEIGHT = 3;
const OTHER_OPACITY = 0.7;
const STOP_BORDER = "#111827";
const TRAFFIC_WEIGHT = 2.5;
const TRAFFIC_OPACITY = 0.8;
const BUS_DOT_RADIUS = 4;
const BUS_DOT_COLOR = "#2563EB";
const VEHICLE_REFRESH_MS = 15000;
const STATUS_REFRESH_MS = 30000;
const DEMO_FROM_QUERIES = ["kashmere gate", "kashmiri gate"];
const DEMO_TO_QUERY = "nehru place";
const DEMO_FROM_LATLON = [28.6677, 77.2285];
const DEMO_TO_LATLON = [28.5491, 77.2526];
const SKELETON_COUNT = 3;
const LANG_KEY = "buspulse.lang";
const SEEN_KEY = "buspulse.seenSplash";
const SPLASH_MIN_MS = 1100;
const SPLASH_REPEAT_MS = 450;
const SPLASH_MAX_MS = 2200;
const HOME_FADE_MS = 250;
const GO_TARGETS = ["plan", "live", "traffic", "demo"];
const PLACES_KEY = "buspulse.places";
const PLACE_KEYS = ["home", "work"];
const PLACE_ICONS = { home: "🏠", work: "💼" };
const VEHICLE_ANIM_TICK_MS = 100;
const HOME_DOT_RADIUS = 3;
const HOME_MAP_BOUNDS = [[28.42, 76.88], [28.86, 77.34]];
const HOME_TRAFFIC_WEIGHT = 2;
const HOME_TRAFFIC_OPACITY = 0.7;
const TOAST_MS = 2200;
const PROFILE_W = 320;
const PROFILE_H = 64;
const PROFILE_PAD = 8;
const PROFILE_TICK_HOURS = [6, 9, 12, 15, 18, 21];

// ---------------------------------------------------------------------------
// Words (English and Hindi)
// ---------------------------------------------------------------------------
const STRINGS = {
  en: {
    subtitle: "Bus times learned from real GPS, not the timetable",
    from: "From", to: "To", date: "Date", time: "Time",
    placeholder: "Stop name or tap the map",
    leaveAt: "Leave at", arriveBy: "Arrive by",
    plan: "Find buses", typical: "Usual day", live: "Live",
    departure: "Departure time", arrival: "Arrival time",
    weekday: "weekday", saturday: "Saturday", sunday: "Sunday / holiday",
    emptyTitle: "Where are you going?",
    emptySub: "Times come from how Delhi buses actually moved, hour by hour.",
    demo: "Try a sample trip: Kashmere Gate to Nehru Place",
    options: "{n} ways", option: "1 way", leaving: "leaving {t} {d}", arriving: "to arrive by {t} {d}",
    min: "{n} min", arrive: "Arrive {t}", leaveBy: "Leave by {t}",
    direct: "No change", oneChange: "1 change", changes: "{n} changes", walkOnly: "Walk",
    reliable: "Usually on time", variable: "Can run up to {n} min late", unreliable: "Often {n}+ min late",
    usuallyBy: "Usually there by {t}",
    trafficAdds: "Traffic adds {n} min", lightTraffic: "Light traffic",
    liveSlower: "Live: {n} min slower than usual", liveFaster: "Live: {n} min faster than usual",
    liveSame: "Live: running as usual",
    better: "Leave at {t} instead: {n} min faster",
    walkRow: "Walk {d} ({m} m) to {s}", waitRow: "Wait about {d} (bus every {h} min)",
    waitRowNoHead: "Wait about {d}",
    busRow: "Bus {r} towards {h}", busSub: "{d} · {n} stops · {k} km/h",
    boardAt: "Get on at {s}", getOff: "Get off at {s}", arriveRow: "Arrive",
    footer: "On the bus: we estimate {a}, the timetable says {b}.",
    confidence: "Estimate quality: {c}", high: "good", medium: "fair", low: "rough",
    noJourney: "No bus found for this time. Many routes stop late at night. Try an earlier time.",
    badgeFeed: "LIVE · real-time DTC feed · updated {t} · {n} buses",
    badgeReplay: "LIVE REPLAY · {d} · recorded GPS played back",
    feedWarming: "Live feed is warming up. Using the 27 Oct replay for now.",
    pickToast: "Tap the map to set {w}", origin: "the start", destination: "the destination",
    myLocation: "My location", traffic: "Traffic",
    legendWalk: "walk", busesNow: "{n} buses",
    origLabel: "Start", destLabel: "Destination",
    splashTag: "Bus times from real GPS",
    tilePlan: "Plan a trip", tilePlanSub: "Any two places, today or any day ahead",
    tileLive: "Live buses", tileLiveSub: "Real buses on the map right now",
    liveNow: "{n} buses on the road now",
    tileTraffic: "City traffic", tileTrafficSub: "How fast buses move on every road, by time of day",
    tileOps: "For operators", tileOpsSub: "Chokepoints and timetable audit",
    morning: "Good morning", afternoon: "Good afternoon", evening: "Good evening", night: "Travelling late?",
    proof: "Learned from a month of real DTC bus GPS · {p}% smaller error than the timetable",
    edit: "Edit", today: "Today", tomorrow: "Tomorrow",
    save: "☆ Save", saveAs: "Save as", placeHome: "Home", placeWork: "Work",
    savedToast: "Saved as {p}", fromPlace: "Plan from {p}", remove: "Remove",
    profileTitle: "On the bus through the day",
    profileRange: "{a}-{b} min · fastest around {t}", profileTap: "tap the graph to pick a time",
    profileGaps: "gaps: this bus does not run then",
    mapCredit: "Map © OpenStreetMap contributors",
  },
  hi: {
    subtitle: "असली GPS से सीखा बस का समय, टाइमटेबल नहीं",
    from: "कहाँ से", to: "कहाँ तक", date: "तारीख", time: "समय",
    placeholder: "स्टॉप का नाम या नक्शे पर टैप करें",
    leaveAt: "निकलना", arriveBy: "पहुँचना",
    plan: "बस खोजें", typical: "सामान्य दिन", live: "लाइव",
    departure: "निकलने का समय", arrival: "पहुँचने का समय",
    weekday: "कार्यदिवस", saturday: "शनिवार", sunday: "रविवार / छुट्टी",
    emptyTitle: "आप कहाँ जा रहे हैं?",
    emptySub: "समय दिल्ली की बसों की असली चाल से, हर घंटे के हिसाब से।",
    demo: "नमूना सफ़र: कश्मीरी गेट से नेहरू प्लेस",
    options: "{n} रास्ते", option: "1 रास्ता", leaving: "{t} {d} निकलना", arriving: "{t} {d} तक पहुँचना",
    min: "{n} मिनट", arrive: "पहुँचें {t}", leaveBy: "{t} तक निकलें",
    direct: "सीधी बस", oneChange: "1 बदलाव", changes: "{n} बदलाव", walkOnly: "पैदल",
    reliable: "आमतौर पर समय पर", variable: "{n} मिनट तक देर हो सकती है", unreliable: "अक्सर {n}+ मिनट देर",
    usuallyBy: "आमतौर पर {t} तक",
    trafficAdds: "ट्रैफ़िक से {n} मिनट ज़्यादा", lightTraffic: "हल्का ट्रैफ़िक",
    liveSlower: "लाइव: सामान्य से {n} मिनट धीमा", liveFaster: "लाइव: सामान्य से {n} मिनट तेज़",
    liveSame: "लाइव: सामान्य चल रहा है",
    better: "{t} पर निकलें: {n} मिनट जल्दी",
    walkRow: "{s} तक {d} पैदल ({m} मी)", waitRow: "लगभग {d} इंतज़ार (हर {h} मिनट बस)",
    waitRowNoHead: "लगभग {d} इंतज़ार",
    busRow: "बस {r}, {h} की ओर", busSub: "{d} · {n} स्टॉप · {k} किमी/घं",
    boardAt: "{s} से चढ़ें", getOff: "{s} पर उतरें", arriveRow: "पहुँचे",
    footer: "बस में: हमारा अनुमान {a}, टाइमटेबल कहता है {b}।",
    confidence: "अनुमान की गुणवत्ता: {c}", high: "अच्छी", medium: "ठीक", low: "मोटा",
    noJourney: "इस समय कोई बस नहीं मिली। देर रात कई रूट बंद रहते हैं। पहले का समय आज़माएँ।",
    badgeFeed: "लाइव · DTC रियल-टाइम फ़ीड · {t} अपडेट · {n} बसें",
    badgeReplay: "लाइव रीप्ले · {d} · रिकॉर्ड किया GPS",
    feedWarming: "लाइव फ़ीड तैयार हो रही है। अभी 27 अक्टूबर का रीप्ले।",
    pickToast: "{w} चुनने के लिए नक्शे पर टैप करें", origin: "शुरुआत", destination: "मंज़िल",
    myLocation: "मेरी जगह", traffic: "ट्रैफ़िक",
    legendWalk: "पैदल", busesNow: "{n} बसें",
    origLabel: "शुरुआत", destLabel: "मंज़िल",
    splashTag: "असली GPS से बस का समय",
    tilePlan: "सफ़र प्लान करें", tilePlanSub: "कोई भी दो जगह, आज या आगे किसी भी दिन",
    tileLive: "लाइव बसें", tileLiveSub: "अभी नक्शे पर असली बसें",
    liveNow: "अभी {n} बसें सड़क पर",
    tileTraffic: "शहर का ट्रैफ़िक", tileTrafficSub: "हर सड़क पर बसों की रफ़्तार, समय के हिसाब से",
    tileOps: "ऑपरेटर के लिए", tileOpsSub: "जाम वाली जगहें और टाइमटेबल जाँच",
    morning: "सुप्रभात", afternoon: "नमस्ते", evening: "शुभ संध्या", night: "देर रात का सफ़र?",
    proof: "DTC बसों के एक महीने के असली GPS से सीखा · टाइमटेबल से {p}% कम गलती",
    edit: "बदलें", today: "आज", tomorrow: "कल",
    save: "☆ सेव", saveAs: "इस रूप में सेव करें", placeHome: "घर", placeWork: "काम",
    savedToast: "{p} के रूप में सेव हुआ", fromPlace: "{p} से सफ़र", remove: "हटाएँ",
    profileTitle: "दिन भर बस में समय",
    profileRange: "{a}-{b} मिनट · सबसे जल्दी लगभग {t}", profileTap: "समय चुनने के लिए ग्राफ़ पर टैप करें",
    profileGaps: "खाली जगह: उस समय यह बस नहीं चलती",
    mapCredit: "नक्शा © OpenStreetMap contributors",
  },
};
const WEEKDAY_SHORT = {
  en: ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"],
  hi: ["रवि", "सोम", "मंगल", "बुध", "गुरु", "शुक्र", "शनि"],
};
const MONTH_SHORT = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const DAY_TYPE_KEYS = ["weekday", "saturday", "sunday"];

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------
const state = {
  lang: "en",
  points: { from: null, to: null },
  mode: "typical",
  timeMode: "depart",
  replayDate: REPLAY_DATE_DEFAULT,
  liveStatus: null,
  plan: null,
  selectedIndex: 0,
  selectedSignature: null,
  planController: null,
  pickField: null,
  sliderTimer: null,
  hasPlan: false,
  trafficOn: false,
  segments: null,
  vehicleTimer: null,
  meta: null,
  homeOpen: false,
  saveField: null,
  toastTimer: null,
};

let map = null;
let trafficLayer = null;
let optionLayer = null;
let markerLayer = null;
let mainFleet = null;
let homeMap = null;
let homeFleet = null;
let homeTrafficLayer = null;
let homeTimer = null;
const els = {};

// ---------------------------------------------------------------------------
// Small helpers
// ---------------------------------------------------------------------------

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

function t(key, vars) {
  let text = STRINGS[state.lang][key];
  if (text === undefined) {
    text = STRINGS.en[key];
  }
  if (vars) {
    const names = Object.keys(vars);
    for (let i = 0; i < names.length; i++) {
      text = text.split("{" + names[i] + "}").join(String(vars[names[i]]));
    }
  }
  return text;
}

function pad2(value) {
  return String(value).padStart(2, "0");
}

function minutesOf(seconds) {
  let minutes = Math.round(seconds / 60);
  if (minutes < 1) {
    minutes = 1;
  }
  return minutes;
}

function minText(seconds) {
  return t("min", { n: minutesOf(seconds) });
}

function hhmm(clock) {
  return clock.slice(0, 5);
}

function parseDate(dateText) {
  const parts = dateText.split("-");
  return new Date(Number(parts[0]), Number(parts[1]) - 1, Number(parts[2]));
}

function dateTextOf(date) {
  return date.getFullYear() + "-" + pad2(date.getMonth() + 1) + "-" + pad2(date.getDate());
}

function todayText() {
  return dateTextOf(new Date());
}

function addDaysText(days) {
  const date = new Date();
  date.setDate(date.getDate() + days);
  return dateTextOf(date);
}

function nowRoundedText() {
  // The current time rounded up to the next 5 minutes, so "now" is never in the past.
  const now = new Date();
  let minutes = now.getHours() * 60 + now.getMinutes();
  minutes = Math.ceil(minutes / TIME_ROUND_MIN) * TIME_ROUND_MIN;
  if (minutes > LAST_TIME_MIN) {
    minutes = LAST_TIME_MIN;
  }
  return timeOfMinutes(minutes);
}

function dayTypeOf(dateText) {
  if (HOLIDAYS.indexOf(dateText) >= 0) {
    return 2;
  }
  const weekday = parseDate(dateText).getDay();
  if (weekday === 0) {
    return 2;
  }
  if (weekday === 6) {
    return 1;
  }
  return 0;
}

function speedColor(speedKmh) {
  for (let i = 0; i < SPEED_CLASSES_KMH.length; i++) {
    if (speedKmh < SPEED_CLASSES_KMH[i]) {
      return SPEED_COLORS[i];
    }
  }
  return SPEED_COLORS[SPEED_COLORS.length - 1];
}

function confidenceLabel(score) {
  if (score >= CONF_HIGH) {
    return "high";
  }
  if (score >= CONF_MEDIUM) {
    return "medium";
  }
  return "low";
}

function minutesOfDay(timeText) {
  const parts = timeText.split(":");
  return Number(parts[0]) * 60 + Number(parts[1]);
}

function timeOfMinutes(total) {
  return pad2(Math.floor(total / 60)) + ":" + pad2(total % 60);
}

function safeStorageGet(key) {
  try {
    return window.localStorage.getItem(key);
  } catch (error) {
    return null;
  }
}

function safeStorageSet(key, value) {
  try {
    window.localStorage.setItem(key, value);
  } catch (error) {
    return;
  }
}

// ---------------------------------------------------------------------------
// Language
// ---------------------------------------------------------------------------

function applyLanguage() {
  document.documentElement.lang = state.lang;
  const nodes = document.querySelectorAll("[data-i18n]");
  for (let i = 0; i < nodes.length; i++) {
    nodes[i].textContent = t(nodes[i].getAttribute("data-i18n"));
  }
  const holders = document.querySelectorAll("[data-i18n-placeholder]");
  for (let i = 0; i < holders.length; i++) {
    holders[i].placeholder = t(holders[i].getAttribute("data-i18n-placeholder"));
  }
  els.langEn.classList.toggle("active", state.lang === "en");
  els.langHi.classList.toggle("active", state.lang === "hi");
  els.homeLangEn.classList.toggle("active", state.lang === "en");
  els.homeLangHi.classList.toggle("active", state.lang === "hi");
  renderHomeText();
  updateTimeLabels();
  updateDayChip();
  updateBadge();
  updateSummary();
  renderPlaces();
  if (state.plan) {
    renderResults();
  } else {
    showEmpty();
  }
}

function setLanguage(lang) {
  state.lang = lang;
  safeStorageSet(LANG_KEY, lang);
  applyLanguage();
}

// ---------------------------------------------------------------------------
// Map
// ---------------------------------------------------------------------------

function initMap() {
  map = L.map("map", { preferCanvas: true, zoomControl: false }).setView(MAP_CENTER, MAP_ZOOM);
  L.control.zoom({ position: "topright" }).addTo(map);
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: "&copy; OpenStreetMap contributors",
  }).addTo(map);
  trafficLayer = L.layerGroup().addTo(map);
  optionLayer = L.layerGroup().addTo(map);
  mainFleet = makeFleet(map, BUS_DOT_RADIUS, true);
  markerLayer = L.layerGroup().addTo(map);
  map.on("click", onMapClick);
}

function abIcon(letter, className) {
  return L.divIcon({
    className: "",
    html: '<div class="marker-ab ' + className + '">' + letter + "</div>",
    iconSize: [18, 18],
    iconAnchor: [9, 9],
  });
}

function legLatLngs(leg) {
  if (leg.type === "walk") {
    return leg.coords;
  }
  const points = [];
  for (let i = 0; i < leg.hops.length; i++) {
    const coords = leg.hops[i].coords;
    for (let j = 0; j < coords.length; j++) {
      points.push(coords[j]);
    }
  }
  return points;
}

function hopTooltip(leg, hopIndex) {
  const hop = leg.hops[hopIndex];
  const fromName = leg.stops[hopIndex].label;
  const toName = leg.stops[hopIndex + 1].label;
  const minutes = (hop.time_s / 60).toFixed(1);
  const label = confidenceLabel(LEVEL_SCORES[hop.level]);
  return fromName + " > " + toName + " - " + Math.round(hop.speed_kmh) + " km/h - " +
    minutes + " min - L" + hop.level + " " + label;
}

function drawPlanOnMap(fit) {
  optionLayer.clearLayers();
  markerLayer.clearLayers();
  const plan = state.plan;
  if (!plan || plan.options.length === 0) {
    return;
  }
  // Other options first so the selected one is drawn on top.
  for (let i = 0; i < plan.options.length; i++) {
    if (i === state.selectedIndex) {
      continue;
    }
    const option = plan.options[i];
    for (let j = 0; j < option.legs.length; j++) {
      const leg = option.legs[j];
      if (leg.type === "wait") {
        continue;
      }
      const line = L.polyline(legLatLngs(leg), {
        color: OTHER_COLOR,
        weight: OTHER_WEIGHT,
        opacity: OTHER_OPACITY,
      });
      line.on("click", makeSelectHandler(i));
      line.addTo(optionLayer);
    }
  }
  const selected = plan.options[state.selectedIndex];
  const bounds = L.latLngBounds([]);
  // Casings under every bus hop, then the speed-colored hops on top.
  for (let j = 0; j < selected.legs.length; j++) {
    const leg = selected.legs[j];
    if (leg.type !== "bus") {
      continue;
    }
    for (let h = 0; h < leg.hops.length; h++) {
      L.polyline(leg.hops[h].coords, {
        color: CASING_COLOR,
        weight: CASING_WEIGHT,
        opacity: CASING_OPACITY,
        interactive: false,
      }).addTo(optionLayer);
    }
  }
  for (let j = 0; j < selected.legs.length; j++) {
    const leg = selected.legs[j];
    if (leg.type === "walk") {
      L.polyline(leg.coords, {
        color: WALK_COLOR,
        weight: WALK_WEIGHT,
        dashArray: WALK_DASH,
      }).addTo(optionLayer);
      bounds.extend(leg.coords);
    } else if (leg.type === "bus") {
      for (let h = 0; h < leg.hops.length; h++) {
        const hop = leg.hops[h];
        const line = L.polyline(hop.coords, {
          color: speedColor(hop.speed_kmh),
          weight: HOP_WEIGHT,
          opacity: 1,
        });
        line.bindTooltip(hopTooltip(leg, h), { sticky: true, className: "hop-tip" });
        line.addTo(optionLayer);
        bounds.extend(hop.coords);
      }
    }
  }
  drawStopMarkers(selected);
  const origin = plan.query.origin;
  const destination = plan.query.destination;
  L.marker([origin.lat, origin.lon], { icon: abIcon("A", "marker-a"), interactive: false }).addTo(markerLayer);
  L.marker([destination.lat, destination.lon], { icon: abIcon("B", "marker-b"), interactive: false }).addTo(markerLayer);
  bounds.extend([origin.lat, origin.lon]);
  bounds.extend([destination.lat, destination.lon]);
  if (fit && bounds.isValid()) {
    map.fitBounds(bounds, { padding: [FIT_PADDING_PX, FIT_PADDING_PX] });
  }
}

function drawStopMarkers(option) {
  const labelled = {};
  for (let j = 0; j < option.legs.length; j++) {
    const leg = option.legs[j];
    if (leg.type !== "bus") {
      continue;
    }
    // Intermediate stops: small circles, no label.
    for (let s = 1; s < leg.stops.length - 1; s++) {
      const stop = leg.stops[s];
      L.circleMarker([stop.lat, stop.lon], {
        radius: 3,
        color: STOP_BORDER,
        weight: 1.5,
        fillColor: "#FFFFFF",
        fillOpacity: 1,
        interactive: false,
      }).addTo(markerLayer);
    }
    labelled[leg.board_stop.stop_id] = leg.board_stop;
    labelled[leg.alight_stop.stop_id] = leg.alight_stop;
  }
  // Boarding, alighting, and transfer stops: larger circles with a permanent label.
  const ids = Object.keys(labelled);
  for (let i = 0; i < ids.length; i++) {
    const stop = labelled[ids[i]];
    const marker = L.circleMarker([stop.lat, stop.lon], {
      radius: 8,
      color: STOP_BORDER,
      weight: 3,
      fillColor: "#FFFFFF",
      fillOpacity: 1,
    });
    marker.bindTooltip(stop.label, {
      permanent: true,
      direction: "right",
      offset: [10, 0],
      className: "stop-label",
    });
    marker.addTo(markerLayer);
  }
}

function makeSelectHandler(index) {
  return function onSelect() {
    selectOption(index);
  };
}

// ---------------------------------------------------------------------------
// City traffic layer and live bus dots
// ---------------------------------------------------------------------------

function currentDepartText() {
  // Any minute is accepted (for example 09:19); an empty date means today.
  let dateText = els.dateInput.value;
  if (!dateText) {
    dateText = todayText();
  }
  return dateText + "T" + (els.timeInput.value || DEFAULT_TIME);
}

function toggleTraffic() {
  state.trafficOn = !state.trafficOn;
  els.trafficButton.classList.toggle("active", state.trafficOn);
  if (!state.trafficOn) {
    trafficLayer.clearLayers();
    return;
  }
  refreshTraffic();
}

function loadSegments() {
  // The network geometry is large, so it is fetched once and reused.
  if (state.segments !== null) {
    return Promise.resolve(state.segments);
  }
  return fetch(API_SEGMENTS)
    .then(function toJson(response) {
      return response.json();
    })
    .then(function keep(data) {
      state.segments = data.segments;
      return state.segments;
    });
}

function refreshTraffic() {
  if (!state.trafficOn) {
    return;
  }
  const segmentsPromise = loadSegments();
  const url = API_SPEEDS + "?depart_at=" + encodeURIComponent(currentDepartText()) + "&mode=" + state.mode;
  Promise.all([segmentsPromise, fetch(url).then(function toJson(response) {
    return response.json();
  })]).then(function draw(results) {
    const segments = results[0];
    const speeds = results[1].speeds_kmh;
    trafficLayer.clearLayers();
    if (!state.trafficOn || !speeds) {
      return;
    }
    for (let g = 0; g < segments.length; g++) {
      if (!segments[g]) {
        continue;
      }
      L.polyline(segments[g], {
        color: speedColor(speeds[g]),
        weight: TRAFFIC_WEIGHT,
        opacity: TRAFFIC_OPACITY,
        interactive: false,
      }).addTo(trafficLayer);
    }
  });
}

// A fleet is a set of bus dots that glide from each reported position to the next one,
// over the time until the next update, instead of jumping.

function makeFleet(targetMap, radius, interactive) {
  return {
    layer: L.layerGroup().addTo(targetMap),
    radius: radius,
    interactive: interactive,
    entries: {},
    moving: [],
    startMs: 0,
    durationMs: 0,
    timer: null,
  };
}

function fleetStop(fleet) {
  if (fleet.timer !== null) {
    clearInterval(fleet.timer);
    fleet.timer = null;
  }
}

function fleetClear(fleet) {
  fleetStop(fleet);
  fleet.layer.clearLayers();
  fleet.entries = {};
  fleet.moving = [];
}

function fleetUpdate(fleet, vehicles, durationMs) {
  const seen = {};
  const moving = [];
  for (let i = 0; i < vehicles.length; i++) {
    const v = vehicles[i];
    seen[v.id] = true;
    let entry = fleet.entries[v.id];
    if (!entry) {
      // A newly seen bus appears where it is.
      const marker = L.circleMarker([v.lat, v.lon], {
        radius: fleet.radius,
        color: "#FFFFFF",
        weight: 1,
        fillColor: BUS_DOT_COLOR,
        fillOpacity: 0.9,
        interactive: fleet.interactive,
      });
      if (fleet.interactive) {
        marker.bindTooltip(v.route_name + " · " + v.id, { className: "hop-tip" });
      }
      marker.addTo(fleet.layer);
      fleet.entries[v.id] = { marker: marker, fromLat: v.lat, fromLon: v.lon, toLat: v.lat, toLon: v.lon };
      continue;
    }
    // A known bus starts from where its dot is now and heads to its new position.
    const here = entry.marker.getLatLng();
    entry.fromLat = here.lat;
    entry.fromLon = here.lng;
    entry.toLat = v.lat;
    entry.toLon = v.lon;
    if (durationMs <= 0) {
      entry.marker.setLatLng([v.lat, v.lon]);
    } else if (entry.fromLat !== entry.toLat || entry.fromLon !== entry.toLon) {
      moving.push(entry);
    }
  }
  // Buses that stopped reporting disappear.
  const ids = Object.keys(fleet.entries);
  for (let i = 0; i < ids.length; i++) {
    if (!seen[ids[i]]) {
      fleet.layer.removeLayer(fleet.entries[ids[i]].marker);
      delete fleet.entries[ids[i]];
    }
  }
  fleet.moving = moving;
  fleet.startMs = Date.now();
  fleet.durationMs = durationMs;
  if (durationMs > 0 && moving.length > 0 && fleet.timer === null) {
    fleet.timer = setInterval(function tick() {
      fleetTick(fleet);
    }, VEHICLE_ANIM_TICK_MS);
  }
}

function fleetTick(fleet) {
  let share = (Date.now() - fleet.startMs) / fleet.durationMs;
  if (share > 1) {
    share = 1;
  }
  for (let i = 0; i < fleet.moving.length; i++) {
    const e = fleet.moving[i];
    e.marker.setLatLng([e.fromLat + (e.toLat - e.fromLat) * share, e.fromLon + (e.toLon - e.fromLon) * share]);
  }
  if (share >= 1) {
    fleetStop(fleet);
  }
}

function refreshVehicles() {
  if (state.mode !== "live") {
    fleetClear(mainFleet);
    return;
  }
  // Feed dots glide between updates; replay dots jump to the chosen time.
  let url = API_LIVE_VEHICLES;
  let durationMs = VEHICLE_REFRESH_MS;
  if (liveSource() === "replay") {
    url = API_LIVE_VEHICLES + "?at=" + encodeURIComponent(els.timeInput.value || DEFAULT_TIME);
    durationMs = 0;
  }
  fetch(url)
    .then(function toJson(response) {
      return response.json();
    })
    .then(function draw(data) {
      if (state.mode !== "live" || !data.vehicles) {
        fleetClear(mainFleet);
        return;
      }
      fleetUpdate(mainFleet, data.vehicles, durationMs);
    });
}

function scheduleVehicles() {
  clearInterval(state.vehicleTimer);
  state.vehicleTimer = null;
  refreshVehicles();
  if (state.mode === "live" && liveSource() === "feed") {
    state.vehicleTimer = setInterval(refreshVehicles, VEHICLE_REFRESH_MS);
  }
}

// ---------------------------------------------------------------------------
// Pick mode and my location
// ---------------------------------------------------------------------------

function armPick(field) {
  state.pickField = field;
  byId("map").classList.add("picking");
  els.fromPin.classList.toggle("armed", field === "from");
  els.toPin.classList.toggle("armed", field === "to");
  let target = t("destination");
  if (field === "from") {
    target = t("origin");
  }
  els.toast.textContent = t("pickToast", { w: target });
  els.toast.classList.remove("hidden");
}

function showToast(text) {
  els.toast.textContent = text;
  els.toast.classList.remove("hidden");
  clearTimeout(state.toastTimer);
  state.toastTimer = setTimeout(function hideToast() {
    if (state.pickField === null) {
      els.toast.classList.add("hidden");
    }
  }, TOAST_MS);
}

function cancelPick() {
  state.pickField = null;
  byId("map").classList.remove("picking");
  els.fromPin.classList.remove("armed");
  els.toPin.classList.remove("armed");
  els.toast.classList.add("hidden");
}

function setPin(field, lat, lon, label) {
  state.points[field] = { name: label, lat: lat, lon: lon, source: "pin" };
  inputOf(field).value = label;
  updatePlanButton();
}

function onMapClick(event) {
  if (state.pickField === null) {
    return;
  }
  const field = state.pickField;
  const lat = event.latlng.lat;
  const lon = event.latlng.lng;
  setPin(field, lat, lon, "Pin " + lat.toFixed(5) + ", " + lon.toFixed(5));
  cancelPick();
}

function useMyLocation() {
  if (!navigator.geolocation) {
    return;
  }
  navigator.geolocation.getCurrentPosition(function gotPosition(position) {
    const lat = position.coords.latitude;
    const lon = position.coords.longitude;
    setPin("from", lat, lon, t("myLocation"));
    map.setView([lat, lon], 15);
  }, function noPosition() {
    els.toast.textContent = "Location not available";
    els.toast.classList.remove("hidden");
    setTimeout(function hideToast() {
      els.toast.classList.add("hidden");
    }, 2500);
  }, { enableHighAccuracy: true, timeout: 10000 });
}

// ---------------------------------------------------------------------------
// Autocomplete
// ---------------------------------------------------------------------------

function inputOf(field) {
  if (field === "from") {
    return els.fromInput;
  }
  return els.toInput;
}

function listOf(field) {
  if (field === "from") {
    return els.fromList;
  }
  return els.toList;
}

const searchState = {
  from: { timer: null, items: [], active: -1, controller: null },
  to: { timer: null, items: [], active: -1, controller: null },
};

function setupAutocomplete(field) {
  const input = inputOf(field);
  const list = listOf(field);
  input.addEventListener("input", function onInput() {
    // Typing replaces any previously chosen point.
    state.points[field] = null;
    updatePlanButton();
    const s = searchState[field];
    clearTimeout(s.timer);
    const query = input.value.trim();
    if (query.length < SEARCH_MIN_CHARS) {
      closeList(field);
      return;
    }
    s.timer = setTimeout(function runSearch() {
      fetchSuggestions(field, query);
    }, SEARCH_DEBOUNCE_MS);
  });
  input.addEventListener("keydown", function onKey(event) {
    const s = searchState[field];
    if (list.classList.contains("hidden")) {
      return;
    }
    if (event.key === "ArrowDown") {
      event.preventDefault();
      setActive(field, Math.min(s.active + 1, s.items.length - 1));
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      setActive(field, Math.max(s.active - 1, 0));
    } else if (event.key === "Enter") {
      event.preventDefault();
      if (s.active >= 0) {
        chooseSuggestion(field, s.active);
      }
    } else if (event.key === "Escape") {
      closeList(field);
    }
  });
  input.addEventListener("blur", function onBlur() {
    setTimeout(function closeLater() {
      closeList(field);
    }, 150);
  });
}

function fetchSuggestions(field, query) {
  const s = searchState[field];
  if (s.controller) {
    s.controller.abort();
  }
  s.controller = new AbortController();
  const url = API_SEARCH + "?q=" + encodeURIComponent(query) + "&limit=" + SEARCH_LIMIT;
  fetch(url, { signal: s.controller.signal })
    .then(function toJson(response) {
      return response.json();
    })
    .then(function show(data) {
      if (!data.results) {
        closeList(field);
        return;
      }
      s.items = data.results;
      s.active = -1;
      renderList(field);
    })
    .catch(function ignoreAbort(error) {
      if (error.name !== "AbortError") {
        closeList(field);
      }
    });
}

function renderList(field) {
  const list = listOf(field);
  const s = searchState[field];
  list.innerHTML = "";
  if (s.items.length === 0) {
    list.classList.add("hidden");
    return;
  }
  for (let i = 0; i < s.items.length; i++) {
    const item = s.items[i];
    const row = el("li", "suggestion");
    row.appendChild(el("span", "suggestion-name", item.name));
    row.appendChild(el("span", "suggestion-meta", item.n_patterns + " buses"));
    row.addEventListener("mousedown", makeChooseHandler(field, i));
    list.appendChild(row);
  }
  list.classList.remove("hidden");
}

function makeChooseHandler(field, index) {
  return function onChoose(event) {
    event.preventDefault();
    chooseSuggestion(field, index);
  };
}

function setActive(field, index) {
  const s = searchState[field];
  s.active = index;
  const rows = listOf(field).children;
  for (let i = 0; i < rows.length; i++) {
    rows[i].classList.toggle("active", i === index);
  }
}

function chooseSuggestion(field, index) {
  const item = searchState[field].items[index];
  state.points[field] = { name: item.name, lat: item.lat, lon: item.lon, source: "search" };
  inputOf(field).value = item.name;
  closeList(field);
  updatePlanButton();
}

function closeList(field) {
  const list = listOf(field);
  list.classList.add("hidden");
  list.innerHTML = "";
  searchState[field].items = [];
  searchState[field].active = -1;
}

// ---------------------------------------------------------------------------
// Date, time, slider, mode
// ---------------------------------------------------------------------------

function updateDayChip() {
  const dateText = els.dateInput.value;
  if (!dateText) {
    els.dayChip.textContent = "-";
    return;
  }
  const weekday = WEEKDAY_SHORT[state.lang][parseDate(dateText).getDay()];
  els.dayChip.textContent = weekday + " · " + t(DAY_TYPE_KEYS[dayTypeOf(dateText)]);
}

function updateTimeLabels() {
  let key = "departure";
  if (state.timeMode === "arrive") {
    key = "arrival";
  }
  els.sliderLabel.textContent = t(key);
  els.timeLeave.classList.toggle("active", state.timeMode === "depart");
  els.timeArrive.classList.toggle("active", state.timeMode === "arrive");
}

function setTimeMode(mode) {
  state.timeMode = mode;
  updateTimeLabels();
  if (state.hasPlan) {
    planJourney(false);
  }
}

function placeTicks() {
  const spans = els.ticks.children;
  for (let i = 0; i < spans.length; i++) {
    const minutes = TICK_HOURS[i] * 60;
    const share = (minutes - SLIDER_MIN_MIN) / (SLIDER_MAX_MIN - SLIDER_MIN_MIN);
    spans[i].style.left = (share * 100).toFixed(2) + "%";
  }
}

function paintSlider() {
  const value = Number(els.slider.value);
  const share = (value - SLIDER_MIN_MIN) / (SLIDER_MAX_MIN - SLIDER_MIN_MIN) * 100;
  els.slider.style.background = "linear-gradient(90deg, #2563EB 0%, #2563EB " + share +
    "%, #E5E7EB " + share + "%, #E5E7EB 100%)";
  els.sliderValue.textContent = timeOfMinutes(value);
}

function syncSliderFromTime() {
  // The slider shows the time input rounded down to 30 minutes, within its range.
  let minutes = minutesOfDay(els.timeInput.value || DEFAULT_TIME);
  minutes = Math.floor(minutes / SLIDER_STEP_MIN) * SLIDER_STEP_MIN;
  if (minutes < SLIDER_MIN_MIN) {
    minutes = SLIDER_MIN_MIN;
  }
  if (minutes > SLIDER_MAX_MIN) {
    minutes = SLIDER_MAX_MIN;
  }
  els.slider.value = String(minutes);
  paintSlider();
  els.sliderValue.textContent = els.timeInput.value || DEFAULT_TIME;
}

function onSliderInput() {
  els.timeInput.value = timeOfMinutes(Number(els.slider.value));
  paintSlider();
  clearTimeout(state.sliderTimer);
  state.sliderTimer = setTimeout(function requery() {
    refreshTraffic();
    if (state.mode === "live" && liveSource() === "replay") {
      refreshVehicles();
    }
    if (state.hasPlan) {
      planJourney(true);
    }
  }, SLIDER_DEBOUNCE_MS);
}

function liveSource() {
  // Real feed when it is ready, otherwise the recorded replay day.
  if (state.liveStatus && state.liveStatus.enabled && state.liveStatus.ready) {
    return "feed";
  }
  return "replay";
}

function updateBadge() {
  if (state.mode !== "live") {
    els.liveBadge.classList.add("hidden");
    return;
  }
  els.liveBadge.classList.remove("hidden");
  if (liveSource() === "feed") {
    els.liveBadge.classList.add("feed");
    els.liveBadgeText.textContent = t("badgeFeed", {
      t: state.liveStatus.updated_at || "-",
      n: state.liveStatus.vehicles,
    });
    return;
  }
  els.liveBadge.classList.remove("feed");
  const parts = state.replayDate.split("-");
  const date = parseDate(state.replayDate);
  const dateText = WEEKDAY_SHORT[state.lang][date.getDay()] + " " + Number(parts[2]) + " " +
    MONTH_SHORT[date.getMonth()] + " " + parts[0];
  let text = t("badgeReplay", { d: dateText });
  if (state.liveStatus && state.liveStatus.enabled && !state.liveStatus.ready) {
    text = text + " · " + t("feedWarming");
  }
  els.liveBadgeText.textContent = text;
}

function setMode(mode) {
  state.mode = mode;
  els.modeTypical.classList.toggle("active", mode === "typical");
  els.modeLive.classList.toggle("active", mode === "live");
  if (mode === "live") {
    // Live means today with the real feed, or the recorded replay day otherwise.
    if (liveSource() === "feed") {
      els.dateInput.value = state.liveStatus.today;
      const now = new Date();
      els.timeInput.value = pad2(now.getHours()) + ":" + pad2(now.getMinutes());
      syncSliderFromTime();
    } else {
      els.dateInput.value = state.replayDate;
    }
    els.dateInput.disabled = true;
  } else {
    els.dateInput.disabled = false;
    if (els.dateInput.value < todayText()) {
      els.dateInput.value = todayText();
      els.timeInput.value = nowRoundedText();
      syncSliderFromTime();
    }
  }
  updateBadge();
  updateDayChip();
  scheduleVehicles();
  refreshTraffic();
  if (state.hasPlan) {
    planJourney(true);
  }
}

function loadLiveStatus() {
  return fetch(API_LIVE_STATUS)
    .then(function toJson(response) {
      return response.json();
    })
    .then(function keep(status) {
      state.liveStatus = status;
      updateBadge();
      renderHomeText();
      return status;
    })
    .catch(function ignore() {
      return null;
    });
}

function updatePlanButton() {
  const ready = state.points.from !== null && state.points.to !== null;
  els.planButton.disabled = !ready;
  updateSaveLinks();
}

// ---------------------------------------------------------------------------
// Planning
// ---------------------------------------------------------------------------

function pointParam(point) {
  if (point.source === "search") {
    return point.name;
  }
  return point.lat.toFixed(5) + "," + point.lon.toFixed(5);
}

function planUrl() {
  return API_PLAN + "?origin=" + encodeURIComponent(pointParam(state.points.from)) +
    "&destination=" + encodeURIComponent(pointParam(state.points.to)) +
    "&depart_at=" + encodeURIComponent(currentDepartText()) +
    "&mode=" + state.mode + "&time_mode=" + state.timeMode;
}

function planJourney(isRequery) {
  if (state.points.from === null || state.points.to === null) {
    return;
  }
  if (state.planController) {
    state.planController.abort();
  }
  const controller = new AbortController();
  state.planController = controller;
  els.progress.classList.remove("hidden");
  if (isRequery && state.hasPlan) {
    els.results.classList.add("stale");
  } else {
    showSkeletons();
  }
  fetch(planUrl(), { signal: controller.signal })
    .then(function readBody(response) {
      return response.json().then(function pair(body) {
        return { ok: response.ok, body: body };
      });
    })
    .then(function handle(result) {
      if (state.planController !== controller) {
        return;
      }
      els.progress.classList.add("hidden");
      els.results.classList.remove("stale");
      if (!result.ok) {
        showError(result.body.error.message);
        return;
      }
      receivePlan(result.body, isRequery);
    })
    .catch(function onFail(error) {
      if (error.name === "AbortError") {
        return;
      }
      els.progress.classList.add("hidden");
      els.results.classList.remove("stale");
      showError("Could not reach the server: " + error.message);
    });
}

function receivePlan(plan, isRequery) {
  state.plan = plan;
  state.hasPlan = true;
  let index = 0;
  if (isRequery && state.selectedSignature !== null) {
    // Keep the same journey selected after a re-query when it still exists.
    for (let i = 0; i < plan.options.length; i++) {
      if (plan.options[i].signature === state.selectedSignature) {
        index = i;
        break;
      }
    }
  }
  state.selectedIndex = index;
  if (plan.options.length > 0) {
    state.selectedSignature = plan.options[index].signature;
  }
  renderResults();
  drawPlanOnMap(!isRequery);
  updateSummary();
  if (!isRequery) {
    collapseForm();
  }
  if (els.body.classList.contains("mobile")) {
    els.results.scrollIntoView({ behavior: "smooth", block: "start" });
  }
}

function friendlyDate(dateText) {
  if (dateText === todayText()) {
    return t("today");
  }
  if (dateText === addDaysText(1)) {
    return t("tomorrow");
  }
  const date = parseDate(dateText);
  return WEEKDAY_SHORT[state.lang][date.getDay()] + " " + date.getDate() + " " +
    MONTH_SHORT[date.getMonth()];
}

function updateSummary() {
  if (!els.summaryRoute || state.points.from === null || state.points.to === null) {
    return;
  }
  els.summaryRoute.textContent = state.points.from.name + " → " + state.points.to.name;
  let modeKey = "leaveAt";
  if (state.timeMode === "arrive") {
    modeKey = "arriveBy";
  }
  els.summaryWhen.textContent = friendlyDate(currentDepartText().slice(0, 10)) + " · " +
    t(modeKey) + " " + (els.timeInput.value || DEFAULT_TIME);
}

function collapseForm() {
  updateSummary();
  els.panel.classList.add("collapsed");
  els.tripSummary.classList.remove("hidden");
  els.panel.scrollTop = 0;
}

function expandForm() {
  els.panel.classList.remove("collapsed");
  els.tripSummary.classList.add("hidden");
  els.panel.scrollTop = 0;
}

function selectOption(index) {
  if (!state.plan || index === state.selectedIndex) {
    return;
  }
  state.selectedIndex = index;
  state.selectedSignature = state.plan.options[index].signature;
  renderResults();
  drawPlanOnMap(false);
}

function useBetterTime(timeText) {
  els.timeInput.value = timeText;
  syncSliderFromTime();
  refreshTraffic();
  planJourney(true);
}

// ---------------------------------------------------------------------------
// Results rendering
// ---------------------------------------------------------------------------

function showSkeletons() {
  els.results.innerHTML = "";
  for (let i = 0; i < SKELETON_COUNT; i++) {
    els.results.appendChild(el("div", "skeleton"));
  }
}

function showEmpty() {
  els.results.innerHTML = "";
  const box = el("div", "empty");
  box.appendChild(el("div", "empty-title", t("emptyTitle")));
  box.appendChild(el("div", "empty-sub", t("emptySub")));
  const button = el("button", "ghost-button", t("demo"));
  button.type = "button";
  button.addEventListener("click", runDemo);
  box.appendChild(button);
  els.results.appendChild(box);
}

function showError(message) {
  state.hasPlan = false;
  state.plan = null;
  optionLayer.clearLayers();
  markerLayer.clearLayers();
  els.results.innerHTML = "";
  els.results.appendChild(el("div", "card error-card", message));
}

function showNoJourney() {
  els.results.innerHTML = "";
  const card = el("div", "card message-card");
  const icon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  icon.setAttribute("viewBox", "0 0 24 24");
  icon.setAttribute("width", "28");
  icon.setAttribute("height", "28");
  icon.innerHTML = '<path fill="#9CA3AF" d="M4 16c0 .88.39 1.67 1 2.22V20a1 1 0 0 0 1 1h1a1 1 0 0 0 1-1v-1h8v1a1 1 0 0 0 1 1h1a1 1 0 0 0 1-1v-1.78c.61-.55 1-1.34 1-2.22V6c0-3.5-3.58-4-8-4S4 2.5 4 6v10zm3.5 1a1.5 1.5 0 1 1 0-3 1.5 1.5 0 0 1 0 3zm9 0a1.5 1.5 0 1 1 0-3 1.5 1.5 0 0 1 0 3zM18 11H6V6h12v5z"/>';
  card.appendChild(icon);
  card.appendChild(el("div", "", t("noJourney")));
  els.results.appendChild(card);
}

function renderResults() {
  const plan = state.plan;
  if (!plan) {
    return;
  }
  if (plan.options.length === 0) {
    showNoJourney();
    optionLayer.clearLayers();
    markerLayer.clearLayers();
    return;
  }
  els.results.innerHTML = "";
  const queryDate = plan.query.depart_at.slice(0, 10);
  const weekday = WEEKDAY_SHORT[state.lang][parseDate(queryDate).getDay()];
  let countText = t("options", { n: plan.options.length });
  if (plan.options.length === 1) {
    countText = t("option");
  }
  let whenText = t("leaving", { t: plan.query.depart_at.slice(11, 16), d: weekday });
  if (plan.query.arrive_by) {
    whenText = t("arriving", { t: plan.query.arrive_by.slice(11, 16), d: weekday });
  }
  els.results.appendChild(el("div", "result-head", countText + " · " + whenText));
  for (let i = 0; i < plan.options.length; i++) {
    els.results.appendChild(renderCard(plan.options[i], i, i === state.selectedIndex));
  }
}

function reliabilityLine(option) {
  const rel = option.reliability;
  const line = el("div", "status-line");
  const dot = el("span", "status-dot dot-" + rel.label);
  line.appendChild(dot);
  let text = t("reliable");
  const lateMin = Math.max(1, Math.round(rel.late_s / 60));
  if (rel.label === "variable") {
    text = t("variable", { n: lateMin });
  } else if (rel.label === "unreliable") {
    text = t("unreliable", { n: lateMin });
  }
  line.appendChild(el("span", "status-text", text));
  return line;
}

function infoChips(option) {
  const chips = el("div", "chip-row");
  // Traffic, in plain words.
  const trafficMin = Math.round(option.quiet_hour.delta_s / 60);
  if (option.transfers >= 0 && option.in_bus_s > 0) {
    if (trafficMin >= TRAFFIC_HEAVY_MIN) {
      chips.appendChild(el("span", "chip chip-low", t("trafficAdds", { n: trafficMin })));
    } else if (trafficMin >= TRAFFIC_SHOW_MIN) {
      chips.appendChild(el("span", "chip chip-medium", t("trafficAdds", { n: trafficMin })));
    } else {
      chips.appendChild(el("span", "chip chip-grey", t("lightTraffic")));
    }
  }
  // Live difference against a usual day.
  if (option.live) {
    const liveMin = Math.round(option.live.delta_s / 60);
    if (liveMin >= LIVE_SHOW_MIN) {
      chips.appendChild(el("span", "chip chip-low", t("liveSlower", { n: liveMin })));
    } else if (liveMin <= -LIVE_SHOW_MIN) {
      chips.appendChild(el("span", "chip chip-high", t("liveFaster", { n: -liveMin })));
    } else {
      chips.appendChild(el("span", "chip chip-grey", t("liveSame")));
    }
  }
  return chips;
}

function betterTimeTip(option) {
  if (!option.best_departure || state.timeMode !== "depart") {
    return null;
  }
  const best = option.best_departure;
  const tip = el("button", "tip", "💡 " + t("better", { t: best.depart_at, n: Math.round(best.saves_s / 60) }));
  tip.type = "button";
  tip.addEventListener("click", function onTip(event) {
    event.stopPropagation();
    useBetterTime(best.depart_at);
  });
  return tip;
}

function renderCard(option, index, selected) {
  let cardClass = "card";
  if (selected) {
    cardClass = "card selected";
  }
  const card = el("div", cardClass);
  card.addEventListener("click", makeSelectHandler(index));

  // 1) The one number people want, and when they arrive (or must leave).
  const top = el("div", "card-top");
  top.appendChild(el("div", "total", minText(option.total_s)));
  const right = el("div", "arrive-box");
  if (option.leave_by) {
    right.appendChild(el("div", "arrive leave", t("leaveBy", { t: option.leave_by })));
  } else {
    right.appendChild(el("div", "arrive", t("arrive", { t: option.arrive_at.slice(11, 16) })));
  }
  right.appendChild(el("div", "arrive-sub", t("usuallyBy", { t: option.reliability.safe_arrive_at })));
  top.appendChild(right);
  card.appendChild(top);

  // 2) Which buses.
  const routes = el("div", "route-row");
  let busCount = 0;
  for (let j = 0; j < option.legs.length; j++) {
    const leg = option.legs[j];
    if (leg.type !== "bus") {
      continue;
    }
    if (busCount > 0) {
      routes.appendChild(el("span", "route-sep", "›"));
    }
    routes.appendChild(el("span", "chip chip-route", leg.route_name));
    busCount += 1;
  }
  let transferText = t("direct");
  if (busCount === 0) {
    routes.appendChild(el("span", "chip chip-neutral", t("walkOnly")));
    transferText = "";
  } else if (option.transfers === 1) {
    transferText = t("oneChange");
  } else if (option.transfers > 1) {
    transferText = t("changes", { n: option.transfers });
  }
  routes.appendChild(el("span", "route-transfers", transferText));
  card.appendChild(routes);

  // 3) Will it be on time? One dot, one sentence.
  card.appendChild(reliabilityLine(option));

  // 4) Traffic and live chips, and a better-time tip.
  card.appendChild(infoChips(option));
  const tip = betterTimeTip(option);
  if (tip !== null) {
    card.appendChild(tip);
  }

  // 5) Proportional bar.
  const bar = el("div", "bar");
  for (let j = 0; j < option.legs.length; j++) {
    const leg = option.legs[j];
    if (leg.duration_s <= 0) {
      continue;
    }
    const piece = el("div", "bar-" + leg.type);
    piece.style.flexGrow = String(leg.duration_s);
    bar.appendChild(piece);
  }
  card.appendChild(bar);

  if (selected) {
    const profile = renderProfile(option);
    if (profile !== null) {
      card.appendChild(profile);
    }
    card.appendChild(renderTimeline(option));
    card.appendChild(renderFooter(option));
  }
  return card;
}

function svgEl(tag, attrs) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  const names = Object.keys(attrs);
  for (let i = 0; i < names.length; i++) {
    node.setAttribute(names[i], attrs[names[i]]);
  }
  return node;
}

function renderProfile(option) {
  // A small graph of this trip's time on the bus at every half hour of the day.
  const profile = option.day_profile;
  if (!profile) {
    return null;
  }
  const values = profile.in_bus_min;
  const first = profile.first_bucket;
  let lo = null;
  let hi = null;
  let bestIndex = -1;
  for (let i = 0; i < values.length; i++) {
    if (values[i] === null) {
      continue;
    }
    if (lo === null || values[i] < lo) {
      lo = values[i];
      bestIndex = i;
    }
    if (hi === null || values[i] > hi) {
      hi = values[i];
    }
  }
  if (lo === null) {
    return null;
  }
  let span = hi - lo;
  if (span < 1) {
    span = 1;
  }
  const last = values.length - 1;
  function xOf(i) {
    return PROFILE_PAD + i / last * (PROFILE_W - 2 * PROFILE_PAD);
  }
  function yOf(v) {
    return PROFILE_H - 18 - (v - lo) / span * (PROFILE_H - 18 - PROFILE_PAD);
  }
  const box = el("div", "profile");
  box.appendChild(el("div", "profile-title", t("profileTitle")));
  const svg = svgEl("svg", { viewBox: "0 0 " + PROFILE_W + " " + PROFILE_H, class: "profile-svg" });
  // The line, broken where the bus does not run.
  let path = "";
  let penDown = false;
  for (let i = 0; i < values.length; i++) {
    if (values[i] === null) {
      penDown = false;
      continue;
    }
    let command = "L";
    if (!penDown) {
      command = "M";
    }
    path += command + xOf(i).toFixed(1) + " " + yOf(values[i]).toFixed(1) + " ";
    penDown = true;
  }
  svg.appendChild(svgEl("path", { d: path, fill: "none", stroke: "#2563EB", "stroke-width": 2.5, "stroke-linejoin": "round" }));
  // Hour labels.
  for (let k = 0; k < PROFILE_TICK_HOURS.length; k++) {
    const index = PROFILE_TICK_HOURS[k] * 2 - first;
    if (index < 0 || index > last) {
      continue;
    }
    const label = svgEl("text", { x: xOf(index), y: PROFILE_H - 3, "text-anchor": "middle", class: "profile-tick" });
    label.textContent = pad2(PROFILE_TICK_HOURS[k]);
    svg.appendChild(label);
  }
  // A dot at this trip's departure time.
  const departMin = minutesOfDay(option.depart_at.slice(11, 16));
  const nowIndex = Math.floor(departMin / 30) - first;
  if (nowIndex >= 0 && nowIndex <= last && values[nowIndex] !== null) {
    svg.appendChild(svgEl("line", { x1: xOf(nowIndex), x2: xOf(nowIndex), y1: PROFILE_PAD, y2: PROFILE_H - 18, stroke: "#93C5FD", "stroke-dasharray": "3 3" }));
    svg.appendChild(svgEl("circle", { cx: xOf(nowIndex), cy: yOf(values[nowIndex]), r: 5, fill: "#2563EB", stroke: "#FFFFFF", "stroke-width": 2 }));
  }
  // Tapping the graph re-plans at that time (leave-at mode only).
  if (state.timeMode === "depart") {
    svg.classList.add("clickable");
    svg.addEventListener("click", function onPick(event) {
      event.stopPropagation();
      const rect = svg.getBoundingClientRect();
      const share = (event.clientX - rect.left) / rect.width;
      let index = Math.round((share * PROFILE_W - PROFILE_PAD) / (PROFILE_W - 2 * PROFILE_PAD) * last);
      if (index < 0) {
        index = 0;
      }
      if (index > last) {
        index = last;
      }
      useBetterTime(timeOfMinutes((first + index) * 30));
    });
  }
  box.appendChild(svg);
  let caption = t("profileRange", { a: Math.round(lo), b: Math.round(hi), t: timeOfMinutes((first + bestIndex) * 30) });
  if (state.timeMode === "depart") {
    caption = caption + " · " + t("profileTap");
  }
  // Explain breaks in the line, so they do not look like a fault.
  if (values.indexOf(null) >= 0) {
    caption = caption + " · " + t("profileGaps");
  }
  box.appendChild(el("div", "profile-caption", caption));
  return box;
}

function timelineRow(timeText, nodeClass, mainText, subLines) {
  const row = el("div", "tl-row");
  row.appendChild(el("div", "tl-time", timeText));
  const rail = el("div", "tl-rail");
  rail.appendChild(el("div", "tl-node " + nodeClass));
  row.appendChild(rail);
  const text = el("div", "tl-text");
  text.appendChild(el("div", "tl-main", mainText));
  for (let i = 0; i < subLines.length; i++) {
    text.appendChild(el("div", "tl-sub", subLines[i]));
  }
  row.appendChild(text);
  return row;
}

function placeLabel(ref) {
  if (ref.label === "Origin") {
    return state.plan.query.origin.label;
  }
  if (ref.label === "Destination") {
    return state.plan.query.destination.label;
  }
  return ref.label;
}

function renderTimeline(option) {
  const box = el("div", "timeline");
  for (let j = 0; j < option.legs.length; j++) {
    const leg = option.legs[j];
    const start = hhmm(leg.start);
    if (leg.type === "walk") {
      if (leg.distance_m < 10) {
        continue;
      }
      const metres = Math.round(leg.distance_m / 10) * 10;
      box.appendChild(timelineRow(start, "", t("walkRow", {
        d: minText(leg.duration_s), m: metres, s: placeLabel(leg.to),
      }), []));
    } else if (leg.type === "wait") {
      let text = t("waitRowNoHead", { d: minText(leg.duration_s) });
      if (leg.headway_s !== null) {
        text = t("waitRow", { d: minText(leg.duration_s), h: Math.round(leg.headway_s / 60) });
      }
      box.appendChild(timelineRow(start, "", text, []));
    } else {
      const main = t("busRow", { r: leg.route_name, h: leg.headsign });
      const sub = t("busSub", { d: minText(leg.duration_s), n: leg.n_stops, k: Math.round(leg.avg_speed_kmh) });
      box.appendChild(timelineRow(start, "bus", main,
        [sub, t("boardAt", { s: leg.board_stop.label }), t("getOff", { s: leg.alight_stop.label })]));
    }
  }
  box.appendChild(timelineRow(option.arrive_at.slice(11, 16), "end", t("arriveRow"), []));
  return box;
}

function renderFooter(option) {
  const footer = el("div", "card-footer");
  if (option.in_bus_s > 0) {
    footer.appendChild(el("div", "", t("footer", {
      a: minText(option.in_bus_s), b: minText(option.scheduled_in_bus_s),
    })));
  }
  footer.appendChild(el("div", "", t("confidence", { c: t(option.confidence.label) })));
  return footer;
}

// ---------------------------------------------------------------------------
// Demo shortcut (empty state button)
// ---------------------------------------------------------------------------

function firstResult(query) {
  const url = API_SEARCH + "?q=" + encodeURIComponent(query) + "&limit=1";
  return fetch(url)
    .then(function toJson(response) {
      return response.json();
    })
    .then(function pick(data) {
      if (data.results && data.results.length > 0) {
        return data.results[0];
      }
      return null;
    });
}

function runDemo() {
  firstResult(DEMO_FROM_QUERIES[0])
    .then(function maybeSecond(found) {
      if (found !== null) {
        return found;
      }
      return firstResult(DEMO_FROM_QUERIES[1]);
    })
    .then(function setFrom(found) {
      if (found !== null) {
        state.points.from = { name: found.name, lat: found.lat, lon: found.lon, source: "search" };
      } else {
        state.points.from = { name: "Kashmere Gate ISBT", lat: DEMO_FROM_LATLON[0], lon: DEMO_FROM_LATLON[1], source: "pin" };
      }
      els.fromInput.value = state.points.from.name;
      return firstResult(DEMO_TO_QUERY);
    })
    .then(function setTo(found) {
      if (found !== null) {
        state.points.to = { name: found.name, lat: found.lat, lon: found.lon, source: "search" };
      } else {
        state.points.to = { name: "Nehru Place", lat: DEMO_TO_LATLON[0], lon: DEMO_TO_LATLON[1], source: "pin" };
      }
      els.toInput.value = state.points.to.name;
      if (state.mode !== "live") {
        const now = new Date();
        const nowMin = now.getHours() * 60 + now.getMinutes();
        if (nowMin < DEMO_DAY_START_MIN) {
          els.dateInput.value = todayText();
          els.timeInput.value = DEMO_FALLBACK_TIME;
        } else if (nowMin > DEMO_DAY_END_MIN) {
          els.dateInput.value = addDaysText(1);
          els.timeInput.value = DEMO_FALLBACK_TIME;
        } else {
          els.dateInput.value = todayText();
          els.timeInput.value = nowRoundedText();
        }
      }
      updateDayChip();
      syncSliderFromTime();
      updatePlanButton();
      planJourney(false);
    });
}

// ---------------------------------------------------------------------------
// Opening: splash and home screen
// ---------------------------------------------------------------------------

function greetingKey() {
  const hour = new Date().getHours();
  if (hour < 5 || hour >= 22) {
    return "night";
  }
  if (hour < 12) {
    return "morning";
  }
  if (hour < 17) {
    return "afternoon";
  }
  return "evening";
}

function renderHomeText() {
  if (!els.homeGreeting) {
    return;
  }
  els.homeGreeting.textContent = t(greetingKey());
  const status = state.liveStatus;
  if (status && status.enabled && status.connected && status.vehicles > 0) {
    els.homeLiveSub.textContent = t("liveNow", { n: status.vehicles });
  } else {
    els.homeLiveSub.textContent = t("tileLiveSub");
  }
  els.homeProof.textContent = "";
  if (state.meta && state.meta.validation) {
    const v = state.meta.validation;
    const pct = Math.round((1 - v.mae_model_min / v.mae_timetable_min) * 100);
    els.homeProof.textContent = t("proof", { p: pct });
  }
}

function openHome() {
  state.homeOpen = true;
  renderHomeText();
  renderPlaces();
  els.home.classList.remove("hidden");
  els.home.classList.remove("leaving");
  startHomeMap();
}

function closeHome(then) {
  state.homeOpen = false;
  stopHomeMap();
  els.home.classList.add("leaving");
  setTimeout(function afterFade() {
    els.home.classList.add("hidden");
    els.home.classList.remove("leaving");
    // Leaflet must re-measure the map once it becomes visible.
    map.invalidateSize();
    if (then) {
      then();
    }
  }, HOME_FADE_MS);
}

function goTo(target) {
  // Each home choice lands in the matching part of the app.
  if (target === "plan") {
    expandForm();
    els.fromInput.focus();
    els.fromInput.scrollIntoView({ behavior: "smooth", block: "center" });
  } else if (target === "live") {
    loadLiveStatus().then(function thenLive() {
      setMode("live");
      map.setView(MAP_CENTER, MAP_ZOOM);
    });
  } else if (target === "traffic") {
    if (!state.trafficOn) {
      toggleTraffic();
    }
  } else if (target === "demo") {
    runDemo();
  }
}

function chooseFromHome(target) {
  closeHome(function after() {
    goTo(target);
  });
}

function finishSplash(goTarget) {
  els.splash.classList.add("gone");
  if (goTarget !== null) {
    goTo(goTarget);
  } else {
    openHome();
  }
  setTimeout(function removeSplash() {
    els.splash.classList.add("hidden");
  }, 400);
}

function startOpening() {
  // A deep link such as /?go=live (used by app shortcuts) skips the home screen.
  const params = new URLSearchParams(window.location.search);
  let goTarget = params.get("go");
  if (GO_TARGETS.indexOf(goTarget) < 0) {
    goTarget = null;
  }
  // The splash is short, and shorter still on a repeat visit in the same session.
  let minimum = SPLASH_MIN_MS;
  let seen = null;
  try {
    seen = window.sessionStorage.getItem(SEEN_KEY);
    window.sessionStorage.setItem(SEEN_KEY, "1");
  } catch (error) {
    seen = null;
  }
  if (seen === "1") {
    minimum = SPLASH_REPEAT_MS;
  }
  const started = Date.now();
  let done = false;
  function finishOnce() {
    if (done) {
      return;
    }
    done = true;
    finishSplash(goTarget);
  }
  // Leave once the app data has arrived and the minimum time has passed.
  Promise.all([loadMeta(), loadLiveStatus()]).then(function ready() {
    const waitMore = Math.max(0, minimum - (Date.now() - started));
    setTimeout(finishOnce, waitMore);
  });
  setTimeout(finishOnce, SPLASH_MAX_MS);
}

// ---------------------------------------------------------------------------
// Saved places (Home / Work), kept only on this device
// ---------------------------------------------------------------------------

function loadPlaces() {
  const raw = safeStorageGet(PLACES_KEY);
  if (raw === null) {
    return {};
  }
  try {
    return JSON.parse(raw) || {};
  } catch (error) {
    return {};
  }
}

function storePlaces(places) {
  safeStorageSet(PLACES_KEY, JSON.stringify(places));
}

function savedPlaceLabel(key) {
  if (key === "home") {
    return t("placeHome");
  }
  return t("placeWork");
}

function setPointFromPlace(field, key, place) {
  state.points[field] = { name: place.name, lat: place.lat, lon: place.lon, source: place.source };
  // A stop keeps its name (the server looks it up); a pin shows the place label.
  if (place.source === "search") {
    inputOf(field).value = place.name;
  } else {
    inputOf(field).value = PLACE_ICONS[key] + " " + savedPlaceLabel(key);
  }
  updatePlanButton();
}

function usePlace(key) {
  const place = loadPlaces()[key];
  if (!place) {
    return;
  }
  let field = "to";
  if (state.points.from === null) {
    field = "from";
  }
  setPointFromPlace(field, key, place);
}

function quickTrip(fromKey, toKey) {
  const places = loadPlaces();
  if (!places[fromKey] || !places[toKey]) {
    return;
  }
  setPointFromPlace("from", fromKey, places[fromKey]);
  setPointFromPlace("to", toKey, places[toKey]);
  // A quick trip leaves now when the chosen time has already passed today.
  if (els.dateInput.value === todayText() && (els.timeInput.value || DEFAULT_TIME) < nowRoundedText()) {
    els.timeInput.value = nowRoundedText();
    syncSliderFromTime();
  }
  planJourney(false);
}

function removePlace(key) {
  const places = loadPlaces();
  delete places[key];
  storePlaces(places);
  renderPlaces();
}

function makeChip(text, className, onClick) {
  const chip = el("button", className, text);
  chip.type = "button";
  chip.addEventListener("click", onClick);
  return chip;
}

function renderPlaces() {
  const places = loadPlaces();
  const saved = [];
  for (let i = 0; i < PLACE_KEYS.length; i++) {
    if (places[PLACE_KEYS[i]]) {
      saved.push(PLACE_KEYS[i]);
    }
  }
  // Planner row: a chip per saved place (fills the next empty box), plus quick trips.
  els.savedRow.innerHTML = "";
  for (let i = 0; i < saved.length; i++) {
    const key = saved[i];
    const group = el("span", "place-chip");
    group.appendChild(makeChip(PLACE_ICONS[key] + " " + savedPlaceLabel(key), "place-use", function onUse() {
      usePlace(key);
    }));
    const removeButton = makeChip("×", "place-remove", function onRemove() {
      removePlace(key);
    });
    removeButton.title = t("remove");
    group.appendChild(removeButton);
    els.savedRow.appendChild(group);
  }
  if (saved.length === 2) {
    els.savedRow.appendChild(makeChip("🏠 → 💼", "quick-chip", function onHomeWork() {
      quickTrip("home", "work");
    }));
    els.savedRow.appendChild(makeChip("💼 → 🏠", "quick-chip", function onWorkHome() {
      quickTrip("work", "home");
    }));
  }
  els.savedRow.classList.toggle("hidden", saved.length === 0);

  // Home screen: one-tap trips between saved places.
  els.homeQuick.innerHTML = "";
  if (saved.length === 2) {
    els.homeQuick.appendChild(makeChip("🏠 " + t("placeHome") + " → 💼 " + t("placeWork"), "home-quick-chip", function onHw() {
      closeHome(function after() {
        quickTrip("home", "work");
      });
    }));
    els.homeQuick.appendChild(makeChip("💼 " + t("placeWork") + " → 🏠 " + t("placeHome"), "home-quick-chip", function onWh() {
      closeHome(function after() {
        quickTrip("work", "home");
      });
    }));
  } else if (saved.length === 1) {
    const key = saved[0];
    els.homeQuick.appendChild(makeChip(PLACE_ICONS[key] + " " + t("fromPlace", { p: savedPlaceLabel(key) }), "home-quick-chip", function onFrom() {
      closeHome(function after() {
        expandForm();
        setPointFromPlace("from", key, loadPlaces()[key]);
        els.toInput.focus();
      });
    }));
  }
  els.homeQuick.classList.toggle("hidden", saved.length === 0);
}

function updateSaveLinks() {
  if (!els.fromSave) {
    return;
  }
  els.fromSave.classList.toggle("hidden", state.points.from === null);
  els.toSave.classList.toggle("hidden", state.points.to === null);
}

function openSavePopover(field) {
  // The one popover moves under whichever box is being saved.
  state.saveField = field;
  inputOf(field).closest(".field").appendChild(els.savePopover);
  els.savePopover.classList.remove("hidden");
}

function savePlace(key) {
  const point = state.points[state.saveField];
  els.savePopover.classList.add("hidden");
  if (!point) {
    return;
  }
  const places = loadPlaces();
  places[key] = { name: point.name, lat: point.lat, lon: point.lon, source: point.source };
  storePlaces(places);
  renderPlaces();
  showToast(t("savedToast", { p: savedPlaceLabel(key) }));
}

// ---------------------------------------------------------------------------
// Home screen background map (live buses, or city speeds without a feed)
// ---------------------------------------------------------------------------

function initHomeMap() {
  if (homeMap !== null) {
    homeMap.invalidateSize();
    return;
  }
  homeMap = L.map("homeMap", {
    preferCanvas: true, zoomControl: false, attributionControl: false, dragging: false,
    scrollWheelZoom: false, doubleClickZoom: false, boxZoom: false, keyboard: false,
    touchZoom: false,
  });
  homeMap.fitBounds(HOME_MAP_BOUNDS);
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", { maxZoom: 19 }).addTo(homeMap);
  homeTrafficLayer = L.layerGroup().addTo(homeMap);
  homeFleet = makeFleet(homeMap, HOME_DOT_RADIUS, false);
}

function drawHomeTraffic() {
  // Without a live feed, the home map shows usual bus speeds at this time of day.
  const now = new Date();
  const departAt = todayText() + "T" + pad2(now.getHours()) + ":" + pad2(now.getMinutes());
  Promise.all([loadSegments(), fetch(API_SPEEDS + "?depart_at=" + encodeURIComponent(departAt))
    .then(function toJson(response) {
      return response.json();
    })]).then(function draw(results) {
    homeTrafficLayer.clearLayers();
    const segments = results[0];
    const speeds = results[1].speeds_kmh;
    for (let g = 0; g < segments.length; g++) {
      if (!segments[g]) {
        continue;
      }
      L.polyline(segments[g], {
        color: speedColor(speeds[g]),
        weight: HOME_TRAFFIC_WEIGHT,
        opacity: HOME_TRAFFIC_OPACITY,
        interactive: false,
      }).addTo(homeTrafficLayer);
    }
  });
}

function refreshHomeVehicles() {
  const status = state.liveStatus;
  if (!status || !status.enabled || !status.connected) {
    fleetClear(homeFleet);
    if (homeTrafficLayer.getLayers().length === 0) {
      drawHomeTraffic();
    }
    return;
  }
  fetch(API_LIVE_VEHICLES)
    .then(function toJson(response) {
      return response.json();
    })
    .then(function draw(data) {
      if (!state.homeOpen || !data.vehicles) {
        return;
      }
      homeTrafficLayer.clearLayers();
      fleetUpdate(homeFleet, data.vehicles, VEHICLE_REFRESH_MS);
    });
}

function startHomeMap() {
  initHomeMap();
  refreshHomeVehicles();
  clearInterval(homeTimer);
  homeTimer = setInterval(refreshHomeVehicles, VEHICLE_REFRESH_MS);
}

function stopHomeMap() {
  // The home map rests while the home screen is hidden.
  clearInterval(homeTimer);
  homeTimer = null;
  if (homeFleet !== null) {
    fleetStop(homeFleet);
  }
}

// ---------------------------------------------------------------------------
// Start-up
// ---------------------------------------------------------------------------

function loadMeta() {
  return fetch(API_META)
    .then(function toJson(response) {
      return response.json();
    })
    .then(function apply(meta) {
      state.meta = meta;
      state.replayDate = meta.replay_date;
      updateBadge();
      renderHomeText();
    })
    .catch(function ignore() {
      return null;
    });
}

function applyLayoutClass() {
  els.body.classList.toggle("mobile", window.innerWidth < 820);
}

function registerServiceWorker() {
  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/sw.js").catch(function ignore() {
      return null;
    });
  }
}

function init() {
  const ids = ["fromInput", "toInput", "fromList", "toList", "fromPin", "toPin", "dateInput",
    "timeInput", "dayChip", "planButton", "planForm", "modeTypical", "modeLive", "liveBadge",
    "liveBadgeText", "timeSlider", "sliderValue", "sliderLabel", "results", "progress", "toast",
    "langEn", "langHi", "timeLeave", "timeArrive", "trafficButton", "locateButton",
    "splash", "home", "homeGreeting", "homePlan", "homeLive", "homeTraffic",
    "homeLiveSub", "homeProof", "homeLangEn", "homeLangHi", "homeButton",
    "panel", "tripSummary", "summaryRoute", "summaryWhen", "editTrip",
    "savedRow", "fromSave", "toSave", "savePopover", "saveHome", "saveWork", "homeQuick"];
  for (let i = 0; i < ids.length; i++) {
    els[ids[i]] = byId(ids[i]);
  }
  els.body = document.body;
  els.slider = els.timeSlider;
  els.ticks = document.querySelector(".ticks");
  const savedLang = safeStorageGet(LANG_KEY);
  if (savedLang === "hi" || savedLang === "en") {
    state.lang = savedLang;
  }
  applyLayoutClass();
  window.addEventListener("resize", applyLayoutClass);
  initMap();
  setupAutocomplete("from");
  setupAutocomplete("to");
  els.fromPin.addEventListener("click", function pickFrom() {
    armPick("from");
  });
  els.toPin.addEventListener("click", function pickTo() {
    armPick("to");
  });
  els.locateButton.addEventListener("click", useMyLocation);
  document.addEventListener("keydown", function onEscape(event) {
    if (event.key === "Escape" && state.pickField !== null) {
      cancelPick();
    }
  });
  els.dateInput.addEventListener("change", function onDate() {
    updateDayChip();
    refreshTraffic();
  });
  els.timeInput.addEventListener("change", syncSliderFromTime);
  els.timeInput.addEventListener("input", syncSliderFromTime);
  els.slider.addEventListener("input", onSliderInput);
  els.modeTypical.addEventListener("click", function chooseTypical() {
    setMode("typical");
  });
  els.modeLive.addEventListener("click", function chooseLive() {
    loadLiveStatus().then(function thenSet() {
      setMode("live");
    });
  });
  els.timeLeave.addEventListener("click", function chooseLeave() {
    setTimeMode("depart");
  });
  els.timeArrive.addEventListener("click", function chooseArrive() {
    setTimeMode("arrive");
  });
  els.trafficButton.addEventListener("click", toggleTraffic);
  els.langEn.addEventListener("click", function chooseEn() {
    setLanguage("en");
  });
  els.langHi.addEventListener("click", function chooseHi() {
    setLanguage("hi");
  });
  els.homeLangEn.addEventListener("click", function homeEn() {
    setLanguage("en");
  });
  els.homeLangHi.addEventListener("click", function homeHi() {
    setLanguage("hi");
  });
  els.homePlan.addEventListener("click", function homePlan() {
    chooseFromHome("plan");
  });
  els.homeLive.addEventListener("click", function homeLive() {
    chooseFromHome("live");
  });
  els.homeTraffic.addEventListener("click", function homeTraffic() {
    chooseFromHome("traffic");
  });
  els.editTrip.addEventListener("click", expandForm);
  els.fromSave.addEventListener("click", function saveFrom() {
    openSavePopover("from");
  });
  els.toSave.addEventListener("click", function saveTo() {
    openSavePopover("to");
  });
  els.saveHome.addEventListener("click", function saveAsHome() {
    savePlace("home");
  });
  els.saveWork.addEventListener("click", function saveAsWork() {
    savePlace("work");
  });
  els.homeButton.addEventListener("click", openHome);
  els.planForm.addEventListener("submit", function onSubmit(event) {
    event.preventDefault();
    planJourney(false);
  });
  // A rider app plans from today onwards: today is the default, up to 90 days ahead.
  els.dateInput.min = todayText();
  els.dateInput.max = addDaysText(DATE_DAYS_AHEAD);
  els.dateInput.value = todayText();
  els.timeInput.value = nowRoundedText();
  placeTicks();
  syncSliderFromTime();
  updatePlanButton();
  applyLanguage();
  startOpening();
  setInterval(function refreshStatus() {
    loadLiveStatus();
  }, STATUS_REFRESH_MS);
  registerServiceWorker();
}

document.addEventListener("DOMContentLoaded", init);
