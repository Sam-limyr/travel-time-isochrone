"use strict";

// Colour schemes and ramp sampling live in palettes.js (loaded first).
// "Colour up to" sets where colouring stops in both styles; band width only sets
// the step, so the last band is narrower when the cut-off isn't a multiple of it.
const BAND_WIDTHS = [5, 10, 15, 20, 30];

const ONEMAP_ATTR = '<img src="https://www.onemap.gov.sg/web-assets/images/logo/om_logo.png" alt="" style="height:16px;width:16px;"/> ' +
  '<a href="https://www.onemap.gov.sg/" target="_blank" rel="noopener noreferrer">OneMap</a> &copy; contributors | ' +
  '<a href="https://www.sla.gov.sg/" target="_blank" rel="noopener noreferrer">Singapore Land Authority</a>';
const ESRI_ATTR = 'Grey canvas &copy; <a href="https://www.esri.com/" target="_blank" rel="noopener noreferrer">Esri</a>, HERE, Garmin, OpenStreetMap contributors';
const esriTiles = (layer) => [`https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/${layer}/MapServer/tile/{z}/{y}/{x}`];
const DATA_ATTR = 'Travel data: LTA DataMall, HDB, URA (Singapore Open Data Licence) | ' +
  '<a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener noreferrer">&copy; OpenStreetMap contributors</a>';
const SG_BOUNDS = [103.6, 1.16, 104.09, 1.47];
const DEFAULT_ORIGIN = { lon: 103.8515, lat: 1.2840 };  // Raffles Place
const LINE_NAMES = {
  NSL: "North–South Line", EWL: "East–West Line", NEL: "North East Line", CCL: "Circle Line",
  DTL: "Downtown Line", TEL: "Thomson–East Coast Line", BPLRT: "Bukit Panjang LRT",
  SKLRT: "Sengkang LRT", PGLRT: "Punggol LRT",
};
const NO_DATA = 65535, UNREACHED = 65534;
const MAX_PLACES = 5;
const BLANK_IMAGE = (() => {  // fully transparent heatmap placeholder
  const c = document.createElement("canvas");
  c.width = c.height = 1;
  return c.toDataURL();
})();

const $ = (sel) => document.querySelector(sel);
const el = (tag, props = {}, ...children) => {
  const node = Object.assign(document.createElement(tag), props);
  for (const c of children) node.append(c);
  return node;
};

const state = {
  dir: "from",  // "from" one start point, or "to" several places (their weighted average)
  origin: null, dest: null,
  places: [],   // [{ lat, lon, w, label }], up to MAX_PLACES
  tripFrom: null,  // spot whose trips to the places are shown
  mode: "transit", band: "am_peak", wait: "avg", walk: 4.8, res: "med",
  bus: true, rail: true, voiddeck: true, parking: 0,
  style: "smooth", maxMin: 90, bandSize: 15, palette: "blues", reverse: false, opacity: 0.7,
  ovMrt: true, ovBus: false, ovStops: false,
  profile: null,  // key-destinations profile (meta.key_destinations[].key)
  theme: "auto", basemap: "onemap",
  view: null,  // [lat, lon, zoom] restored from the URL
};

let meta = null, map = null, grid = null, lastResult = null;
let reqSeq = 0, routeSeq = 0, busyTimer = null;
let originMarker = null, destMarker = null, tripMarker = null, lastGoodOrigin = null;
let placeMarkers = [], placeParts = [];  // per place: its map pin, and its fetched grid
const placeGrids = new Map();            // per place and settings: promise of its grid
let lineColours = {};
const loadedOverlays = new Set();

/* --- utilities -------------------------------------------------------------- */

const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const isDark = () => state.theme === "dark" || (state.theme === "auto" && matchMedia("(prefers-color-scheme: dark)").matches);
const fmtMin = (m) => (m < 10 ? m.toFixed(1) : Math.round(m).toString());
const bandLabel = (key) => meta.bands.find((b) => b.key === key).label;

function debounce(fn, ms) {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}

const writeHashSoon = debounce(() => writeHash(), 400);  // while typing names and weights

function showStatus(text, isError = false, ms = 0) {
  const s = $("#status");
  s.textContent = text;
  s.classList.toggle("error", isError);
  s.hidden = !text;
  if (ms) setTimeout(() => { if (s.textContent === text) s.hidden = true; }, ms);
}

/* --- URL hash state --------------------------------------------------------- */

function writeHash() {
  const p = new URLSearchParams();
  p.set("dir", state.dir);
  if (state.origin) p.set("o", `${state.origin.lat.toFixed(5)},${state.origin.lon.toFixed(5)}`);
  if (state.dest) p.set("d", `${state.dest.lat.toFixed(5)},${state.dest.lon.toFixed(5)}`);
  if (state.places.length) {
    p.set("pl", state.places.map((q) => [q.lat.toFixed(5), q.lon.toFixed(5), q.w, encodeURIComponent(q.label)].join(",")).join(";"));
  }
  if (state.tripFrom) p.set("t", `${state.tripFrom.lat.toFixed(5)},${state.tripFrom.lon.toFixed(5)}`);
  p.set("mode", state.mode); p.set("band", state.band); p.set("wait", state.wait);
  p.set("walk", state.walk); p.set("res", state.res); p.set("park", state.parking);
  p.set("use", (state.bus ? "b" : "") + (state.rail ? "r" : "") + (state.voiddeck ? "v" : ""));
  p.set("style", state.style); p.set("max", state.maxMin); p.set("bw", state.bandSize);
  p.set("pal", state.palette);
  if (state.reverse) p.set("rev", "1");
  p.set("ov", (state.ovMrt ? "m" : "") + (state.ovBus ? "b" : "") + (state.ovStops ? "s" : ""));
  if (state.profile) p.set("kp", state.profile);
  if (map) {
    const c = map.getCenter();
    p.set("v", `${c.lat.toFixed(4)},${c.lng.toFixed(4)},${map.getZoom().toFixed(2)}`);
  }
  history.replaceState(null, "", "#" + p.toString());
}

function readHash() {
  const p = new URLSearchParams(location.hash.slice(1));
  const pt = (v) => { const [lat, lon] = (v || "").split(",").map(Number); return Number.isFinite(lat) && Number.isFinite(lon) ? { lat, lon } : null; };
  state.origin = pt(p.get("o"));
  const view = (p.get("v") || "").split(",").map(Number);
  state.view = view.length === 3 && view.every(Number.isFinite) ? view : null;
  state.dest = pt(p.get("d"));
  state.places = parsePlaces(p.get("pl") || "");
  state.tripFrom = pt(p.get("t"));
  const pick = (k, allowed) => (allowed.includes(p.get(k)) ? p.get(k) : null);
  state.dir = pick("dir", ["from", "to"]) || state.dir;
  state.mode = pick("mode", ["transit", "car"]) || state.mode;
  state.band = pick("band", meta.bands.map((b) => b.key)) || state.band;
  state.wait = pick("wait", meta.wait_modes) || state.wait;
  state.res = pick("res", meta.resolutions.map((r) => r.key)) || state.res;
  state.style = pick("style", ["smooth", "bands"]) || state.style;
  state.palette = pick("pal", PALETTES.map((q) => q.key)) || state.palette;
  state.profile = pick("kp", meta.key_destinations.map((q) => q.key)) || meta.key_destinations[0]?.key || null;
  state.reverse = p.get("rev") === "1";
  const num = (k, lo, hi, dflt) => { const v = Number(p.get(k)); return p.has(k) && v >= lo && v <= hi ? v : dflt; };
  state.walk = num("walk", meta.walk_kmh.min, meta.walk_kmh.max, meta.walk_kmh.default);
  state.parking = meta.parking_min.includes(num("park", 0, 30, -1)) ? num("park", 0, 30, 0) : 0;
  state.maxMin = num("max", 30, 150, state.maxMin);
  state.bandSize = BAND_WIDTHS.includes(num("bw", 5, 30, -1)) ? num("bw", 5, 30, -1) : state.bandSize;
  if (p.has("use")) { const u = p.get("use"); state.bus = u.includes("b"); state.rail = u.includes("r"); state.voiddeck = u.includes("v"); }
  if (p.has("ov")) { const o = p.get("ov"); state.ovMrt = o.includes("m"); state.ovBus = o.includes("b"); state.ovStops = o.includes("s"); }
  try {
    state.theme = localStorage.getItem("theme") || "auto";
    state.basemap = localStorage.getItem("basemap") === "esri" ? "esri" : "onemap";
  } catch { /* storage unavailable: keep defaults */ }
}

/** "lat,lon,weight,label;..." as written by writeHash; malformed entries are dropped. */
function parsePlaces(text) {
  const places = [];
  for (const part of text.split(";").filter(Boolean)) {
    const [lat, lon, w, label = ""] = part.split(",");
    let name = "";
    try { name = decodeURIComponent(label).slice(0, 30); } catch { /* keep the default */ }
    const q = { lat: Number(lat), lon: Number(lon), w: Number(w), label: name };
    if (!Number.isFinite(q.lat) || !Number.isFinite(q.lon) || places.length >= MAX_PLACES) continue;
    if (!(q.w >= 0 && q.w <= 99)) q.w = 1;
    q.label = q.label || `Place ${places.length + 1}`;
    places.push(q);
  }
  return places;
}

/* --- controls --------------------------------------------------------------- */

function segOption(name, value, label, small) {
  const input = el("input", { type: "radio", name, value });
  const span = el("span", {}, label);
  if (small) span.append(el("small", {}, small));
  return el("label", {}, input, span);
}

function buildControls() {
  const bands = $("#band-group");
  for (const b of meta.bands) bands.append(segOption("band", b.key, b.label, b.hours));
  const parking = $("#parking-group");
  for (const m of meta.parking_min) parking.append(segOption("parking", String(m), m ? `${m} min` : "None"));
  const res = $("#res-group");
  const resLabel = { low: "Low", med: "Medium", high: "High" };
  for (const r of meta.resolutions) res.append(segOption("res", r.key, resLabel[r.key] || r.key, `${r.res_m} m grid`));
  const bw = $("#bandsize-group");
  for (const m of BAND_WIDTHS) bw.append(segOption("bandSize", String(m), String(m)));
  const palettes = $("#palette-group");
  for (const q of PALETTES) {
    const input = el("input", { type: "radio", name: "palette", value: q.key });
    input.setAttribute("aria-label", q.name);
    palettes.append(el("label", { title: q.name }, input, el("span", { className: "ramp" })));
  }

  const profiles = $("#key-profile");
  for (const q of meta.key_destinations) profiles.append(el("option", { value: q.key }, q.name));

  const walk = $("#walk");
  walk.min = meta.walk_kmh.min; walk.max = meta.walk_kmh.max;
  const ticks = [
    [3.5, "Slow"], [meta.walk_kmh.leisurely, "Leisurely"], [meta.walk_kmh.default, "Average"], [5.6, "Brisk"],
  ];
  const labels = $("#walk-tick-labels");
  for (const [v, name] of ticks) {
    $("#walk-ticks").append(el("option", { value: v }));
    const pct = ((v - meta.walk_kmh.min) / (meta.walk_kmh.max - meta.walk_kmh.min)) * 100;
    const tick = el("span", {}, name, el("br"), `${v}`);
    tick.style.left = `calc(${pct}% + ${8 - pct * 0.16}px)`;
    labels.append(tick);
  }

  // reflect state into the controls
  const check = (name, value) => { const i = document.querySelector(`input[name="${name}"][value="${value}"]`); if (i) i.checked = true; };
  check("dir", state.dir);
  check("mode", state.mode); check("band", state.band); check("wait", state.wait); check("res", state.res);
  check("parking", String(state.parking)); check("style", state.style); check("bandSize", String(state.bandSize));
  check("palette", state.palette);
  walk.value = state.walk;
  $("#max-min").value = state.maxMin;
  $("#palette-rev").checked = state.reverse;
  $("#opacity").value = state.opacity;
  $("#use-bus").checked = state.bus; $("#use-rail").checked = state.rail; $("#use-voiddeck").checked = state.voiddeck;
  $("#ov-mrt").checked = state.ovMrt; $("#ov-bus").checked = state.ovBus; $("#ov-stops").checked = state.ovStops;
  $("#theme").value = state.theme;
  if (state.profile) profiles.value = state.profile;
  syncControlVisibility();
  paintPaletteSwatches();

  // events
  const recompute = () => { writeHash(); compute(); };
  const recomputeSlow = debounce(recompute, 250);
  const redraw = () => { writeHash(); render(); renderLegend(); };
  document.querySelectorAll('input[type="radio"]').forEach((input) => input.addEventListener("change", () => {
    const { name, value } = input;
    if (name === "parking" || name === "bandSize") state[name] = Number(value); else state[name] = value;
    if (name === "dir") applyDir();
    syncControlVisibility();
    if (["style", "bandSize", "palette"].includes(name)) redraw(); else recompute();
  }));
  walk.addEventListener("input", () => { state.walk = Number(walk.value); updateWalkOut(); recomputeSlow(); });
  $("#max-min").addEventListener("input", (e) => { state.maxMin = Number(e.target.value); updateMaxOut(); updateBandNote(); redraw(); });
  $("#palette-rev").addEventListener("change", (e) => { state.reverse = e.target.checked; paintPaletteSwatches(); redraw(); });
  $("#opacity").addEventListener("input", (e) => { state.opacity = Number(e.target.value); applyHeatOpacity(); });
  for (const [id, key] of [["#use-bus", "bus"], ["#use-rail", "rail"], ["#use-voiddeck", "voiddeck"]]) {
    $(id).addEventListener("change", (e) => { state[key] = e.target.checked; recompute(); });
  }
  for (const [id, key] of [["#ov-mrt", "ovMrt"], ["#ov-bus", "ovBus"], ["#ov-stops", "ovStops"]]) {
    $(id).addEventListener("change", (e) => { state[key] = e.target.checked; writeHash(); applyOverlays(); });
  }
  $("#theme").addEventListener("change", (e) => {
    state.theme = e.target.value;
    try { localStorage.setItem("theme", state.theme); } catch { /* storage unavailable */ }
    applyTheme();
  });
  $("#basemap").value = state.basemap;
  $("#basemap").addEventListener("change", (e) => {
    state.basemap = e.target.value;
    try { localStorage.setItem("basemap", state.basemap); } catch { /* storage unavailable */ }
    applyTheme();
  });
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => { if (state.theme === "auto") applyTheme(); });
  $("#route-clear").addEventListener("click", clearRoute);
  document.addEventListener("keydown", (e) => {  // Esc closes the route or trips
    const typing = e.target.matches("input[type=text], input[type=number], textarea");
    if (e.key === "Escape" && !$("#route-section").hidden && !typing) clearRoute();
  });
  $("#key-score").addEventListener("click", showKeySection);
  profiles.addEventListener("change", () => { state.profile = profiles.value; writeHash(); renderKeyDestinations(); });
  $("#my-places-example").addEventListener("click", loadExample);
  $("#panel-toggle").addEventListener("click", () => {
    const panel = $("#panel");
    const collapsed = !panel.classList.contains("collapsed");
    panel.classList.toggle("collapsed", collapsed);
    $("#panel-body").hidden = collapsed;
    $("#panel-toggle").setAttribute("aria-expanded", String(!collapsed));
    $("#panel-toggle").textContent = collapsed ? "▴" : "▾";
    $("#panel-toggle").title = collapsed ? "Show controls" : "Hide controls";
  });
  updateWalkOut(); updateMaxOut();
}

function syncControlVisibility() {
  const transit = state.mode === "transit", to = state.dir === "to";
  document.querySelectorAll(".transit-only").forEach((n) => (n.hidden = !transit));
  document.querySelectorAll(".car-only").forEach((n) => (n.hidden = transit));
  document.querySelectorAll(".to-only").forEach((n) => (n.hidden = !to));
  $("#route-section").hidden = to ? !(state.tripFrom && lastTrips && state.places.length) : !lastRoute;
  $("#hint").textContent = to
    ? `Click the map to add up to ${MAX_PLACES} places; it shows the average trip time from everywhere to them. Right-click (Ctrl-click on a Mac) a spot for its trips.`
    : "Click the map to set a starting point. Right-click (Ctrl-click on a Mac) for the route to a point.";
  $("#area-title").textContent = to ? "Area by average trip time" : "Reachable area";
  $("#landmarks-title").textContent = to ? "Average trip time from landmarks" : "Travel time to places";
  $("#bandsize-row").hidden = state.style !== "bands";
  $("#band-note").hidden = state.style !== "bands";
  updateBandNote();
  const palette = PALETTES.find((q) => q.key === state.palette);
  $("#palette-name").textContent = palette.name;
  $("#palette-note").hidden = !palette.cvdWarning;
  const notes = {
    best: "No waiting at all: every bus and train arrives as you reach the stop.",
    avg: "Expected wait for someone who turns up at a random time.",
    worst: "You just missed each bus and train: the longest scheduled gap.",
  };
  $("#wait-note").textContent = notes[state.wait];
}

const updateWalkOut = () => { $("#walk-out").textContent = `${state.walk.toFixed(1)} km/h`; };
const updateMaxOut = () => { $("#max-out").textContent = `${state.maxMin} min`; };

function updateBandNote() {
  const edges = bandEdges(), n = edges.length - 1, last = edges[n] - edges[n - 1];
  let text = last === state.bandSize
    ? `${n} band${n === 1 ? "" : "s"} of ${state.bandSize} min, up to ${state.maxMin} min.`
    : `${n} bands up to ${state.maxMin} min; the last covers ${edges[n - 1]}–${state.maxMin}.`;
  if (n > 8) text += " With this many, neighbouring bands are hard to tell apart; hover for exact times.";
  $("#band-note").textContent = text;
}

function paintPaletteSwatches() {
  document.querySelectorAll('#palette-group input[name="palette"]').forEach((input) => {
    const stops = rampStops(input.value, isDark(), state.reverse).map(rgbCss);
    input.nextElementSibling.style.background = `linear-gradient(to right, ${stops.join(", ")})`;
  });
}

/* --- map -------------------------------------------------------------------- */

const onemapTiles = () => [`https://www.onemap.gov.sg/maps/tiles/${isDark() ? "Night" : "Grey"}/{z}/{x}/{y}.png`];
const esriBaseTiles = () => esriTiles(isDark() ? "World_Dark_Gray_Base" : "World_Light_Gray_Base");
const esriLabelTiles = () => esriTiles(isDark() ? "World_Dark_Gray_Reference" : "World_Light_Gray_Reference");

function mapPadding() {
  // keep the island clear of the control panel (left) or bottom sheet (narrow screens)
  return innerWidth > 700 ? { top: 70, bottom: 30, left: 370, right: 30 } : { top: 90, bottom: innerHeight * 0.45, left: 10, right: 10 };
}

function initMap() {
  const onemap = state.basemap === "onemap";
  map = new maplibregl.Map({
    container: "map",
    style: {
      version: 8,
      sources: {
        onemap: { type: "raster", tiles: onemapTiles(), tileSize: 256, minzoom: 10, maxzoom: 19, attribution: ONEMAP_ATTR },
        "esri-base": { type: "raster", tiles: esriBaseTiles(), tileSize: 256, maxzoom: 16, attribution: ESRI_ATTR },
        "esri-labels": { type: "raster", tiles: esriLabelTiles(), tileSize: 256, maxzoom: 16 },
      },
      layers: [
        { id: "background", type: "background", paint: { "background-color": isDark() ? "#0d0d0d" : "#f2f1ee" } },
        { id: "basemap-onemap", type: "raster", source: "onemap", layout: { visibility: onemap ? "visible" : "none" } },
        { id: "basemap-esri", type: "raster", source: "esri-base", layout: { visibility: onemap ? "none" : "visible" } },
      ],
    },
    ...(state.view ? { center: [state.view[1], state.view[0]], zoom: state.view[2] }
      : { bounds: [[SG_BOUNDS[0], SG_BOUNDS[1]], [SG_BOUNDS[2], SG_BOUNDS[3]]], fitBoundsOptions: { padding: mapPadding() } }),
    minZoom: 10.5, maxZoom: 18.5,
    maxBounds: [[103.35, 1.05], [104.35, 1.6]],
    attributionControl: false,
    dragRotate: false, pitchWithRotate: false, touchPitch: false,
  });
  map.touchZoomRotate.disableRotation();
  map.addControl(new maplibregl.AttributionControl({ compact: true, customAttribution: DATA_ATTR }), "bottom-right");
  map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "bottom-right");
  map.addControl(new maplibregl.ScaleControl({ unit: "metric" }), "bottom-left");

  map.once("style.load", async () => {
    addLayers();
    applyTheme();
    await applyOverlays();
    if (!state.origin) state.origin = { ...DEFAULT_ORIGIN };
    setOriginMarker();
    if (state.dest) setDestMarker();
    if (state.tripFrom) setTripMarker();
    applyDir();
    writeHash();
    compute();
  });
  map.on("click", (e) => {
    if (state.dir === "to") { addPlace(e.lngLat); return; }
    state.origin = { lon: e.lngLat.lng, lat: e.lngLat.lat };
    setOriginMarker(); writeHash(); compute();
  });
  map.on("contextmenu", (e) => {
    e.originalEvent.preventDefault();
    if (state.dir === "to") { showTripsFrom(e.lngLat); return; }
    state.dest = { lon: e.lngLat.lng, lat: e.lngLat.lat };
    setDestMarker(); writeHash(); fetchRoute();
  });
  map.on("mousemove", onHover);
  map.on("moveend", debounce(writeHash, 300));
  map.getCanvas().addEventListener("mouseleave", hideHover);
}

const SG_CORNERS = [[SG_BOUNDS[0], SG_BOUNDS[3]], [SG_BOUNDS[2], SG_BOUNDS[3]], [SG_BOUNDS[2], SG_BOUNDS[1]], [SG_BOUNDS[0], SG_BOUNDS[1]]];

function addLayers() {
  // transparent until the first result arrives
  map.addSource("heat", { type: "image", url: BLANK_IMAGE, coordinates: SG_CORNERS });
  map.addLayer({ id: "heat", type: "raster", source: "heat", paint: { "raster-opacity": state.opacity, "raster-resampling": "linear", "raster-fade-duration": 0 } });

  map.addSource("bus-routes", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
  map.addLayer({ id: "bus-routes", type: "line", source: "bus-routes", layout: { "line-join": "round", "line-cap": "round" },
    paint: { "line-width": ["interpolate", ["linear"], ["zoom"], 11, 0.6, 15, 1.4], "line-opacity": 0.45 } });
  map.addLayer({ id: "bus-routes-hover", type: "line", source: "bus-routes", filter: ["in", ["get", "service"], ["literal", []]],
    layout: { "line-join": "round", "line-cap": "round" }, paint: { "line-width": 3 } });

  // place names sit above the heatmap so they stay readable (Esri only; OneMap has no label layer)
  map.addLayer({ id: "labels-esri", type: "raster", source: "esri-labels",
    layout: { visibility: state.basemap === "onemap" ? "none" : "visible" } });

  map.addSource("mrt-lines", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
  map.addLayer({ id: "mrt-casing", type: "line", source: "mrt-lines", layout: { "line-join": "round", "line-cap": "round" },
    paint: { "line-width": ["interpolate", ["linear"], ["zoom"], 10.5, 5, 15, 9] } });
  map.addLayer({ id: "mrt-lines", type: "line", source: "mrt-lines", layout: { "line-join": "round", "line-cap": "round" },
    paint: { "line-color": ["get", "colour"], "line-width": ["interpolate", ["linear"], ["zoom"], 10.5, 3, 15, 5] } });

  map.addSource("route", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
  // the route gets an ink outline so it stands out from the MRT overlay it runs along
  map.addLayer({ id: "route-casing", type: "line", source: "route", filter: ["!=", ["get", "type"], "walk"],
    layout: { "line-join": "round", "line-cap": "round" }, paint: { "line-width": 10 } });
  map.addLayer({ id: "route-line", type: "line", source: "route", filter: ["!=", ["get", "type"], "walk"],
    layout: { "line-join": "round", "line-cap": "round" }, paint: { "line-color": ["get", "colour"], "line-width": 6 } });
  map.addLayer({ id: "route-walk", type: "line", source: "route", filter: ["==", ["get", "type"], "walk"],
    layout: { "line-join": "round", "line-cap": "round" }, paint: { "line-width": 4, "line-dasharray": [0.1, 1.8] } });

  map.addSource("mrt-stations", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
  map.addLayer({ id: "mrt-stations", type: "circle", source: "mrt-stations", minzoom: 11.5,
    paint: { "circle-radius": ["interpolate", ["linear"], ["zoom"], 11.5, 2.5, 15, 5.5], "circle-stroke-width": 1.5 } });
  map.addSource("bus-stops", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
  map.addLayer({ id: "bus-stops", type: "circle", source: "bus-stops", minzoom: 14.5,
    paint: { "circle-radius": ["interpolate", ["linear"], ["zoom"], 14.5, 2, 17, 4], "circle-stroke-width": 1 } });

  map.addSource("hover-cell", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
  map.addLayer({ id: "hover-cell", type: "line", source: "hover-cell", paint: { "line-width": 1.5 } });
}

function applyTheme() {
  const dark = isDark();
  document.documentElement.toggleAttribute("data-theme", state.theme !== "auto");
  if (state.theme !== "auto") document.documentElement.setAttribute("data-theme", state.theme);
  paintPaletteSwatches();  // one-way ramps start from the other end in the dark theme
  if (!map || !map.getLayer("heat")) return;
  map.getSource("onemap").setTiles(onemapTiles());
  map.getSource("esri-base").setTiles(esriBaseTiles());
  map.getSource("esri-labels").setTiles(esriLabelTiles());
  const onemap = state.basemap === "onemap";
  map.setLayoutProperty("basemap-onemap", "visibility", onemap ? "visible" : "none");
  map.setLayoutProperty("basemap-esri", "visibility", onemap ? "none" : "visible");
  map.setLayoutProperty("labels-esri", "visibility", onemap ? "none" : "visible");
  map.setPaintProperty("background", "background-color", dark ? "#0d0d0d" : "#f2f1ee");
  const surface = cssVar("--surface"), ink = cssVar("--ink"), ink2 = cssVar("--ink-2"), muted = cssVar("--muted");
  map.setPaintProperty("bus-routes", "line-color", ink2);
  map.setPaintProperty("bus-routes-hover", "line-color", ink);
  map.setPaintProperty("mrt-casing", "line-color", surface);
  map.setPaintProperty("route-casing", "line-color", ink);
  map.setPaintProperty("route-walk", "line-color", ink);
  map.setPaintProperty("mrt-stations", "circle-color", surface);
  map.setPaintProperty("mrt-stations", "circle-stroke-color", ink);
  map.setPaintProperty("bus-stops", "circle-color", muted);
  map.setPaintProperty("bus-stops", "circle-stroke-color", surface);
  map.setPaintProperty("hover-cell", "line-color", ink);
  if (lastRoute) drawRoute(lastRoute);
  render(); renderLegend();
}

async function loadOverlay(name, source) {
  if (loadedOverlays.has(name)) return;
  loadedOverlays.add(name);
  const res = await fetch(`/api/overlays/${name}`);
  if (!res.ok) { loadedOverlays.delete(name); throw new Error(`overlay ${name}: HTTP ${res.status}`); }
  const data = await res.json();
  if (name === "mrt_lines") for (const f of data.features) lineColours[f.properties.line] = f.properties.colour;
  map.getSource(source).setData(data);
}

async function applyOverlays() {
  try {
    if (state.ovMrt) { await loadOverlay("mrt_lines", "mrt-lines"); await loadOverlay("mrt_stations", "mrt-stations"); }
    if (state.ovBus) await loadOverlay("bus_routes", "bus-routes");
    if (state.ovStops) await loadOverlay("bus_stops", "bus-stops");
  } catch (err) { showStatus(`Could not load overlay (${err.message})`, true, 4000); }
  // line colours are also needed for route legs even when the MRT overlay is off
  if (!loadedOverlays.has("mrt_lines")) loadOverlay("mrt_lines", "mrt-lines").catch(() => {});
  const vis = (on) => (on ? "visible" : "none");
  for (const id of ["mrt-casing", "mrt-lines", "mrt-stations"]) map.setLayoutProperty(id, "visibility", vis(state.ovMrt));
  for (const id of ["bus-routes", "bus-routes-hover"]) map.setLayoutProperty(id, "visibility", vis(state.ovBus));
  map.setLayoutProperty("bus-stops", "visibility", vis(state.ovStops));
}

function setOriginMarker() {
  if (!originMarker) {
    originMarker = new maplibregl.Marker({ element: el("div", { className: "pin origin", title: "Start (drag to move)" }), draggable: true })
      .setLngLat([state.origin.lon, state.origin.lat]).addTo(map);
    originMarker.on("dragend", () => {
      const p = originMarker.getLngLat();
      state.origin = { lon: p.lng, lat: p.lat }; writeHash(); compute();
    });
  }
  originMarker.setLngLat([state.origin.lon, state.origin.lat]);
}

function setDestMarker() {
  if (!destMarker) {
    destMarker = new maplibregl.Marker({ element: el("div", { className: "pin dest", title: "Destination (drag to move)" }), draggable: true })
      .setLngLat([state.dest.lon, state.dest.lat]).addTo(map);
    destMarker.on("dragend", () => {
      const p = destMarker.getLngLat();
      state.dest = { lon: p.lng, lat: p.lat }; writeHash(); fetchRoute();
    });
  }
  destMarker.setLngLat([state.dest.lon, state.dest.lat]);
}

/* --- isochrone -------------------------------------------------------------- */

function apiParams(point, extra = {}) {
  return new URLSearchParams({
    lat: point.lat, lon: point.lon, mode: state.mode, band: state.band, wait: state.wait,
    walk_kmh: state.walk, res: state.res, bus: state.bus, rail: state.rail, voiddeck: state.voiddeck,
    parking: state.parking, ...extra,
  });
}

// dim the heatmap and say so if a result takes more than a moment
function startBusy(seq) {
  clearTimeout(busyTimer);
  busyTimer = setTimeout(() => { if (seq === reqSeq) { applyHeatOpacity(true); showStatus("Computing travel times…"); } }, 150);
}

function endBusy(seq) {
  if (seq === reqSeq) { clearTimeout(busyTimer); applyHeatOpacity(false); }
}

function compute() {
  return state.dir === "to" ? computePlaces() : computeFrom();
}

function clearResult() {
  grid = null; lastResult = null;
  if (map && map.getSource("heat")) map.getSource("heat").updateImage({ url: BLANK_IMAGE, coordinates: SG_CORNERS });
  renderLegend(); updateTables(); renderKeyDestinations();
}

async function computeFrom() {
  if (!state.origin || !map) return;
  const seq = ++reqSeq;
  startBusy(seq);
  try {
    const res = await fetch(`/api/isochrone?${apiParams(state.origin)}`);
    const body = await res.json();
    if (seq !== reqSeq) return;
    if (!res.ok) throw new Error(body.detail || `HTTP ${res.status}`);
    lastResult = body;
    lastGoodOrigin = { ...state.origin };
    grid = decodeGrid(body.grid);
    showStatus("");
    render(); renderLegend(); updateTables(); renderKeyDestinations();
    if (state.dest) fetchRoute();
  } catch (err) {
    if (seq === reqSeq) {
      showStatus(err.message, true, 6000);
      if (lastGoodOrigin) {  // keep the pin on the point the heatmap was computed from
        state.origin = { ...lastGoodOrigin };
        setOriginMarker(); writeHash();
      }
    }
  } finally {
    endBusy(seq);
  }
}

/* --- several places: the weighted average of travel times to each ---------- */

// A place's grid depends on where it is and on every travel setting, but not on the display.
const settingsKey = () => [state.mode, state.band, state.wait, state.walk, state.res, state.bus, state.rail,
  state.voiddeck, state.parking].join("|");

/** Promise of the grid of travel times from everywhere to place q (cached). */
function placeGrid(q) {
  const key = `${q.lat.toFixed(5)},${q.lon.toFixed(5)}|${settingsKey()}`;
  if (!placeGrids.has(key)) {
    const promise = fetch(`/api/isochrone?${apiParams(q, { direction: "to" })}`).then(async (res) => {
      const body = await res.json();
      if (!res.ok) throw Object.assign(new Error(body.detail || `HTTP ${res.status}`), { status: res.status });
      return decodeGrid(body.grid);
    });
    promise.catch(() => placeGrids.delete(key));
    placeGrids.set(key, promise);
    while (placeGrids.size > 3 * MAX_PLACES) placeGrids.delete(placeGrids.keys().next().value);
  }
  return placeGrids.get(key);
}

async function computePlaces() {
  if (!map) return;
  const seq = ++reqSeq;
  syncPlaceMarkers(); renderPlacesList();
  if (!state.places.length) { placeParts = []; clearResult(); refreshTrips(); return; }
  startBusy(seq);
  try {
    const settled = await Promise.allSettled(state.places.map(placeGrid));
    if (seq !== reqSeq) return;
    // a place off the network (at sea, in Johor) can't be used: drop it and say why
    const bad = settled.findIndex((s) => s.status === "rejected");
    if (bad >= 0) {
      const err = settled[bad].reason;
      if (err.status !== 400) throw err;
      showStatus(`${state.places[bad].label}: ${err.message}`, true, 6000);
      state.places.splice(bad, 1);
      writeHash();
      computePlaces();
      return;
    }
    placeParts = settled.map((s) => s.value);
    if (!$("#status").classList.contains("error")) showStatus("");
    combinePlaces();
  } catch (err) {
    if (seq === reqSeq) showStatus(err.message, true, 6000);
  } finally {
    endBusy(seq);
  }
}

const NO_WEIGHT = "Give at least one place a weight above 0.";

/** Each cell's weighted average of its travel times to the places; instant, so weights apply live. */
function combinePlaces() {
  if (!placeParts.length || placeParts.length !== state.places.length) return;
  const weights = state.places.map((q) => q.w), total = weights.reduce((a, b) => a + b, 0);
  if (!(total > 0)) {
    clearResult();
    showStatus(NO_WEIGHT, true, 5000);
    return;
  }
  if ($("#status").textContent === NO_WEIGHT) showStatus("");
  const n = placeParts[0].values.length, out = new Uint16Array(n);
  for (let i = 0; i < n; i++) {
    let sum = 0, code = 0;
    for (let k = 0; k < placeParts.length; k++) {
      if (!weights[k]) continue;
      const v = placeParts[k].values[i];
      if (v >= UNREACHED) code = Math.max(code, v);  // no data outranks unreachable
      else sum += weights[k] * v;
    }
    out[i] = code || Math.round(sum / total);
  }
  grid = { ...placeParts[0], values: out };
  lastResult = { key_destinations: null };
  render(); renderLegend(); updateTables(); renderKeyDestinations();
  refreshTrips();
}

/** Whether the places' weights differ (the average is then a weighted one). */
function placesWeighted() {
  return state.places.some((q) => q.w !== state.places[0].w);
}

function nextPlaceLabel() {
  for (let n = 1; ; n++) if (!state.places.some((q) => q.label === `Place ${n}`)) return `Place ${n}`;
}

function addPlace(lngLat) {
  if (state.places.length >= MAX_PLACES) {
    showStatus(`That's ${MAX_PLACES} places already: remove one to add another.`, true, 4000);
    return;
  }
  state.places.push({ lat: lngLat.lat, lon: lngLat.lng, w: 1, label: nextPlaceLabel() });
  writeHash(); compute();
}

function removePlace(i) {
  state.places.splice(i, 1);
  writeHash(); compute();
}

/** One numbered, draggable pin per place, shown only in the several-places view. */
function syncPlaceMarkers() {
  if (!map) return;
  while (placeMarkers.length > state.places.length) placeMarkers.pop().remove();
  state.places.forEach((q, i) => {
    if (!placeMarkers[i]) {
      const m = new maplibregl.Marker({ element: el("div", { className: "pin place" }), draggable: true })
        .setLngLat([q.lon, q.lat]).addTo(map);
      m.on("dragend", () => {
        const p = m.getLngLat();
        Object.assign(state.places[placeMarkers.indexOf(m)], { lon: p.lng, lat: p.lat });
        writeHash(); compute();
      });
      placeMarkers[i] = m;
    }
    const node = placeMarkers[i].setLngLat([q.lon, q.lat]).getElement();
    node.textContent = String(i + 1);
    node.title = `${q.label} (drag to move)`;
    node.hidden = state.dir !== "to";
    node.classList.toggle("off", q.w === 0);  // a zero weight leaves the place out of the average
  });
}

// the example from the brief: two people's workplaces and a place they go together
const EXAMPLE_PLACES = [
  { label: "Work (you)", lat: 1.28400, lon: 103.85150, w: 5 },      // Raffles Place
  { label: "Work (partner)", lat: 1.29950, lon: 103.78750, w: 5 },  // one-north
  { label: "Weekend park", lat: 1.28160, lon: 103.86360, w: 2 },    // Gardens by the Bay
];

let listedPlaces = [];  // the place objects the list's rows were built for

/** The places list: a name and a weight per place, both editable in place. */
function renderPlacesList() {
  const same = listedPlaces.length === state.places.length && listedPlaces.every((q, i) => q === state.places[i]);
  if (!same) {  // rebuild only when places come or go, so typing keeps its focus
    listedPlaces = [...state.places];
    $("#my-places-list").replaceChildren(...state.places.map((q, i) => {
      const name = el("input", { type: "text", className: "place-name", value: q.label, maxLength: 30 });
      name.setAttribute("aria-label", `Name of place ${i + 1}`);
      name.addEventListener("input", () => { q.label = name.value.trim() || `Place ${i + 1}`; syncPlaceMarkers(); writeHashSoon(); });
      const weight = el("input", { type: "number", className: "place-weight", value: q.w, min: 0, max: 99, step: 0.5 });
      weight.setAttribute("aria-label", `Weight of place ${i + 1}`);
      weight.addEventListener("input", () => {
        const w = weight.value === "" ? NaN : Number(weight.value);
        if (!(w >= 0 && w <= 99)) return;  // mid-edit or out of range: keep the last good weight
        q.w = w;
        syncPlaceMarkers(); updatePlaceShares(); combinePlaces(); writeHashSoon();
      });
      const remove = el("button", { type: "button", className: "icon-btn small", title: "Remove this place" }, "×");
      remove.setAttribute("aria-label", `Remove place ${i + 1}`);
      remove.addEventListener("click", () => removePlace(i));
      return el("li", {}, el("span", { className: "pin-num" }, String(i + 1)), name,
        el("label", { className: "weight", title: "Weight: how much this place counts" }, "×", weight),
        el("span", { className: "place-share" }), remove);
    }));
  }
  updatePlaceShares();
  $("#my-places-empty").hidden = state.places.length > 0;
  $("#my-places-weights").hidden = state.places.length < 2;
  $("#my-places-full").hidden = state.places.length < MAX_PLACES;
}

/** Each place's share of the total weight, i.e. its pull on the average. */
function updatePlaceShares() {
  const total = state.places.reduce((s, q) => s + q.w, 0);
  $("#my-places-list").querySelectorAll("li").forEach((li, i) => {
    const q = state.places[i];
    li.classList.toggle("off", q.w === 0);
    li.querySelector(".place-share").textContent = total > 0 ? `${Math.round((100 * q.w) / total)}%` : "–";
  });
}

function loadExample() {
  state.places = EXAMPLE_PLACES.map((q) => ({ ...q }));
  writeHash(); compute();
}

/** Show the markers, route and panel sections of the current view. */
function applyDir() {
  const to = state.dir === "to";
  if (originMarker) originMarker.getElement().hidden = to;
  if (destMarker) destMarker.getElement().hidden = to;
  if (tripMarker) tripMarker.getElement().hidden = !to;
  if (map && map.getSource("route")) {
    // the route layers carry this view's route, or its trips
    map.getSource("route").setData({ type: "FeatureCollection", features: [] });
    dimOverlays(false);
    if (!to && lastRoute) drawRoute(lastRoute);
    if (to) refreshTrips();
    hideHover();
  }
  syncPlaceMarkers();
  renderPlacesList();
}

function decodeGrid(g) {
  const bin = atob(g.data);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return { ...g, values: new Uint16Array(bytes.buffer), data: undefined };
}

const currentRamp = () => rampStops(state.palette, isDark(), state.reverse);

/** Band boundaries in minutes: 0, width, 2 x width, ... and the cut-off. */
function bandEdges() {
  const edges = [];
  for (let m = 0; m < state.maxMin; m += state.bandSize) edges.push(m);
  return [...edges, state.maxMin];
}

const currentBandColours = () => bandColours(currentRamp(), bandEdges().length - 1, hexRgb(cssVar("--surface")));

function render() {
  if (!grid || !map || !map.getSource("heat")) return;
  // lookup table from tenths of a minute to an RGBA pixel
  const cap = state.maxMin, lut = new Uint32Array(cap * 10 + 1);
  const pack = ([r, g, b]) => ((255 << 24) | (b << 16) | (g << 8) | r) >>> 0;
  if (state.style === "bands") {
    const colours = currentBandColours().map(pack), step = state.bandSize * 10;
    for (let v = 0; v < lut.length; v++) lut[v] = colours[Math.min(colours.length - 1, Math.floor(v / step))];
  } else {
    const stops = currentRamp();
    for (let v = 0; v < lut.length; v++) lut[v] = pack(sampleRamp(stops, v / (cap * 10)));
  }
  const canvas = $("#heat-canvas");
  canvas.width = grid.nx; canvas.height = grid.ny;
  const ctx = canvas.getContext("2d");
  const img = ctx.createImageData(grid.nx, grid.ny);
  const px = new Uint32Array(img.data.buffer);
  const vals = grid.values;
  for (let i = 0; i < vals.length; i++) { const v = vals[i]; px[i] = v < lut.length ? lut[v] : 0; }
  ctx.putImageData(img, 0, 0);
  const [w, s, e, n] = grid.bounds;
  map.getSource("heat").updateImage({ url: canvas.toDataURL(), coordinates: [[w, n], [e, n], [e, s], [w, s]] });
}

function applyHeatOpacity(busy = false) {
  if (map && map.getLayer("heat")) map.setPaintProperty("heat", "raster-opacity", busy ? state.opacity * 0.45 : state.opacity);
}

function addTick(axis, label, frac) {
  const t = el("span", {}, label);
  t.style.left = `${frac * 100}%`;
  t.style.transform = frac <= 0.02 ? "none" : frac >= 0.98 ? "translateX(-100%)" : "translateX(-50%)";
  axis.append(t);
}

function renderLegend() {
  const box = $("#legend");
  box.replaceChildren();
  const cap = state.maxMin;
  const axis = el("div", { className: "axis" });
  if (state.style === "smooth") {
    const bar = el("div", { className: "bar" });
    bar.style.background = `linear-gradient(to right, ${currentRamp().map(rgbCss).join(", ")})`;
    const step = smoothStep(cap);
    for (let m = 0; m <= cap; m += step) addTick(axis, `${m}`, m / cap);
    box.append(bar, axis);
  } else {
    // swatch widths follow the minutes each band covers, so a short last band looks short
    const edges = bandEdges(), sw = el("div", { className: "swatches" });
    sw.style.gridTemplateColumns = edges.slice(1).map((m, i) => `${m - edges[i]}fr`).join(" ");
    for (const c of currentBandColours()) { const d = el("div"); d.style.background = rgbCss(c); sw.append(d); }
    // label every band edge while they fit, then every 2nd, 3rd...; the cut-off is always labelled
    const every = Math.ceil((0.1 * cap) / state.bandSize);
    edges.forEach((m, i) => {
      if (m === cap || (i % every === 0 && cap - m >= 0.08 * cap)) addTick(axis, `${m}`, m / cap);
    });
    box.append(sw, axis);
  }
  const to = state.dir === "to";
  const caption = !to ? "Minutes from the start · " : placesWeighted() ? "Weighted average minutes to your places · " : "Average minutes to your places · ";
  box.append(el("div", { className: "caption" }, caption,
    el("span", { className: "nodata" }), `over ${cap}`));
  const how = state.mode === "car" ? "by car" : "by public transport";
  $("#legend-title").textContent = meta ? `${to ? "Average trip time" : "Travel time"} ${how} · ${bandLabel(state.band)}` : "Travel time";
  renderCoverage();
}

/* --- share of the island in each band -------------------------------------- */

const smoothStep = (cap) => (cap <= 45 ? 10 : cap <= 100 ? 15 : 30);
const fmtPct = (p) => (p === 0 ? "0%" : p < 0.1 ? "<0.1%" : `${p.toFixed(1)}%`);
const fmtKm2 = (a) => (a === 0 ? "0" : a < 0.1 ? "<0.1" : a < 10 ? a.toFixed(1) : Math.round(a).toString());

// Rows follow the scale's steps: the bands, or in smooth mode the legend's labelled intervals.
function coverageEdges() {
  if (state.style === "bands") return bandEdges();
  const edges = [];
  for (let m = 0; m < state.maxMin; m += smoothStep(state.maxMin)) edges.push(m);
  return [...edges, state.maxMin];
}

function renderCoverage() {
  const box = $("#coverage");
  box.hidden = !grid;
  if (!grid) return;
  const edges = coverageEdges(), step = (edges[1] - edges[0]) * 10, cap = state.maxMin * 10;
  const counts = new Array(edges.length - 1).fill(0), vals = grid.values;
  let mapped = 0, over = 0;
  for (let i = 0; i < vals.length; i++) {
    const v = vals[i];
    if (v === NO_DATA) continue;
    mapped++;
    if (v > cap) over++;
    else counts[Math.min(counts.length - 1, Math.floor(v / step))]++;
  }
  const ramp = currentRamp();
  const colours = state.style === "bands" ? currentBandColours()
    : counts.map((_, i) => sampleRamp(ramp, (edges[i] + edges[i + 1]) / 2 / state.maxMin));
  const rows = counts.map((n, i) => ({ n, label: `${edges[i]}–${edges[i + 1]} min`, fill: rgbCss(colours[i]) }));
  rows.push({ n: over, label: `Over ${state.maxMin} min`, cls: "over" });
  rows.push({ n: Math.max(0, grid.land_cells - mapped), label: "No footpath nearby", cls: "none",
    title: "Land more than 400 m from any footpath: forest, reservoirs, airfields, military and industrial islands. It isn't coloured on the map." });

  const land = grid.land_cells, cellKm2 = (grid.res_m / 1000) ** 2;
  $("#coverage-total").textContent = `${fmtKm2(land * cellKm2)} km²`;
  const swatch = (r) => { const s = el("span", { className: `sw ${r.cls || ""}` }); if (r.fill) s.style.background = r.fill; return s; };
  $("#coverage-stack").replaceChildren(...rows.filter((r) => r.n > 0).map((r) => {
    const seg = el("span", { className: r.cls || "" });
    if (r.fill) seg.style.background = r.fill;
    seg.style.flexGrow = r.n;
    return seg;
  }));
  $("#coverage-rows").replaceChildren(...rows.map((r) => el("tr", { title: r.title || "" },
    el("td", {}, swatch(r), r.label),
    el("td", { className: "num" }, fmtPct((100 * r.n) / land)),
    el("td", { className: "num km" }, `${fmtKm2(r.n * cellKm2)} km²`))));
}

/* --- hover ------------------------------------------------------------------ */

function cellAt(lng, lat) {
  if (!grid) return null;
  const [w, s, e, n] = grid.bounds;
  const col = Math.floor(((lng - w) / (e - w)) * grid.nx);
  const row = Math.floor(((n - lat) / (n - s)) * grid.ny);
  if (col < 0 || row < 0 || col >= grid.nx || row >= grid.ny) return null;
  const dLon = (e - w) / grid.nx, dLat = (n - s) / grid.ny;
  const x0 = w + col * dLon, y0 = n - row * dLat, i = row * grid.nx + col;
  return { i, v: grid.values[i], ring: [[x0, y0], [x0 + dLon, y0], [x0 + dLon, y0 - dLat], [x0, y0 - dLat], [x0, y0]] };
}

function onHover(e) {
  const tip = $("#tooltip");
  const cell = cellAt(e.lngLat.lng, e.lngLat.lat);
  const layers = ["mrt-stations", "bus-stops", "bus-routes"].filter((id) => map.getLayer(id) && map.getLayoutProperty(id, "visibility") !== "none");
  const box = [[e.point.x - 4, e.point.y - 4], [e.point.x + 4, e.point.y + 4]];
  const feats = layers.length ? map.queryRenderedFeatures(box, { layers }) : [];
  if (!cell || cell.v === NO_DATA) { if (!feats.length) return hideHover(); }

  tip.replaceChildren();
  if (cell && cell.v !== NO_DATA) {
    const mins = (v) => (v >= UNREACHED ? `over ${meta.max_minutes}` : fmtMin(v / 10));
    const several = state.dir === "to" && state.places.length > 1, weighted = placesWeighted();
    tip.append(el("strong", {}, `${cell.v === UNREACHED ? "Over " + meta.max_minutes : fmtMin(cell.v / 10)} min${several ? (weighted ? " weighted average" : " on average") : ""}`));
    tip.append(el("span", { className: "k" }, `${state.mode === "car" ? "by car" : "by public transport"}, ${bandLabel(state.band)}`));
    if (state.dir === "to" && placeParts.length === state.places.length) {
      // each place's own trip time from here
      placeParts.forEach((part, k) => {
        const q = state.places[k];
        tip.append(el("div", { className: q.w === 0 ? "trip off" : "trip" }, el("span", { className: "pin-num" }, String(k + 1)),
          `${q.label}${weighted ? ` ×${q.w}` : ""}: ${mins(part.values[cell.i])} min`));
      });
    }
    map.getSource("hover-cell").setData({ type: "Feature", geometry: { type: "Polygon", coordinates: [cell.ring] }, properties: {} });
  } else {
    map.getSource("hover-cell").setData({ type: "FeatureCollection", features: [] });
  }
  const station = feats.find((f) => f.layer.id === "mrt-stations");
  const stop = feats.find((f) => f.layer.id === "bus-stops");
  const services = [...new Set(feats.filter((f) => f.layer.id === "bus-routes").map((f) => f.properties.service))];
  if (station) tip.append(el("div", {}, `${station.properties.name} (${station.properties.codes})`));
  else if (stop) tip.append(el("div", {}, `Bus stop ${stop.properties.code} · ${stop.properties.name}`));
  if (services.length) {
    services.sort((a, b) => a.localeCompare(b, "en", { numeric: true }));
    tip.append(el("div", { className: "k" }, `Bus ${services.slice(0, 12).join(", ")}${services.length > 12 ? "…" : ""}`));
  }
  map.setFilter("bus-routes-hover", ["in", ["get", "service"], ["literal", services]]);
  tip.hidden = false;
  const pad = 14, rect = tip.getBoundingClientRect();
  let x = e.originalEvent.clientX + pad, y = e.originalEvent.clientY + pad;
  if (x + rect.width > innerWidth - 4) x = e.originalEvent.clientX - rect.width - pad;
  if (y + rect.height > innerHeight - 4) y = e.originalEvent.clientY - rect.height - pad;
  tip.style.left = `${x}px`; tip.style.top = `${y}px`;
  map.getCanvas().style.cursor = station || stop ? "pointer" : "crosshair";
}

function hideHover() {
  $("#tooltip").hidden = true;
  if (map.getSource("hover-cell")) map.getSource("hover-cell").setData({ type: "FeatureCollection", features: [] });
  if (map.getLayer("bus-routes-hover")) map.setFilter("bus-routes-hover", ["in", ["get", "service"], ["literal", []]]);
}

/* --- routes ----------------------------------------------------------------- */

let lastRoute = null;  // "from one point": the route from the start to state.dest
let lastTrips = null;  // "to several places": { key, routes } from state.tripFrom to each place

async function fetchRoute() {
  if (!state.origin || !state.dest) return;
  const seq = ++routeSeq;
  try {
    const res = await fetch(`/api/route?${apiParams(state.origin, { to_lat: state.dest.lat, to_lon: state.dest.lon })}`);
    const body = await res.json();
    if (seq !== routeSeq) return;
    if (!res.ok) throw new Error(body.detail || `HTTP ${res.status}`);
    lastRoute = body;
    drawRoute(body);
  } catch (err) {
    if (seq === routeSeq) showStatus(`Route: ${err.message}`, true, 5000);
  }
}

function legColour(leg) {
  if (leg.type === "train") return lineColours[leg.line] || cssVar("--ink-2");
  if (leg.type === "bus" || leg.type === "drive") return cssVar("--ink");
  return cssVar("--ink-2");
}

const routeFeatures = (route) => (route.reachable ? route.legs.filter((l) => l.coords && l.coords.length > 1).map((l) => ({
  type: "Feature", properties: { type: l.type === "transfer" ? "walk" : l.type, colour: legColour(l) },
  geometry: { type: "LineString", coordinates: l.coords },
})) : []);

function drawRoute(route) {
  const features = routeFeatures(route);
  map.getSource("route").setData({ type: "FeatureCollection", features });
  dimOverlays(features.length > 0);

  const box = $("#route");
  box.replaceChildren();
  showRouteSection("Route");
  if (!route.reachable) {
    box.append(el("p", { className: "note" }, `Not reachable within ${meta.max_minutes} minutes with these settings.`));
    return;
  }
  const how = state.mode === "car" ? "by car" : "by public transport";
  box.append(el("p", { className: "total" }, `${fmtMin(route.total_s / 60)} min`, el("small", {}, `${how}, ${bandLabel(state.band)}`)),
    legList(route));
}

/** A route's legs as a list: mode badge, what happens, minutes. */
function legList(route) {
  const list = el("ol");
  for (const leg of route.legs) {
    let badge, detail;
    if (leg.type === "walk") {
      badge = el("span", { className: "badge walk" }, "Walk");
      detail = el("span", { className: "detail" }, "On foot");
    } else if (leg.type === "bus") {
      badge = el("span", { className: "badge bus" }, leg.service);
      detail = el("span", { className: "detail" }, el("b", {}, leg.from), " → ", el("b", {}, leg.to),
        ` · ${leg.stops} stop${leg.stops > 1 ? "s" : ""}, wait ${fmtMin(leg.wait_s / 60)} min`);
    } else if (leg.type === "train") {
      badge = el("span", { className: "badge" }, leg.line);
      badge.style.background = lineColours[leg.line] || "";
      badge.title = LINE_NAMES[leg.line] || leg.line;
      detail = el("span", { className: "detail" }, el("b", {}, leg.from), " → ", el("b", {}, leg.to),
        ` · ${leg.stops} stop${leg.stops > 1 ? "s" : ""}, wait ${fmtMin(leg.wait_s / 60)} min`);
    } else if (leg.type === "transfer") {
      badge = el("span", { className: "badge walk" }, "Change");
      detail = el("span", { className: "detail" }, `Interchange walk at ${leg.at}`);
    } else if (leg.type === "drive") {
      badge = el("span", { className: "badge bus" }, "Drive");
      detail = el("span", { className: "detail" }, "Typical traffic for this time of day");
    } else {
      badge = el("span", { className: "badge walk" }, "Park");
      detail = el("span", { className: "detail" }, "Parking allowance");
    }
    list.append(el("li", {}, badge, detail, el("span", { className: "mins" }, `${fmtMin(leg.seconds / 60)}′`)));
  }
  return list;
}

/* Trips: in the several-places view, a right-click shows the itinerary from that
   spot to each place, and their weighted average (the heatmap's value there). */

const tripsKey = () => JSON.stringify([state.tripFrom, state.places.map((q) => [q.lat, q.lon]), settingsKey()]);

async function fetchTrips() {
  if (!state.tripFrom) return;
  if (!state.places.length) { showStatus("Add a place first: click the map.", true, 4000); return; }
  const seq = ++routeSeq, key = tripsKey(), from = state.tripFrom;
  try {
    // one search from the spot serves every trip: the server reuses it
    const routes = await Promise.all(state.places.map(async (q) => {
      const res = await fetch(`/api/route?${apiParams(from, { to_lat: q.lat, to_lon: q.lon })}`);
      const body = await res.json();
      if (!res.ok) throw new Error(body.detail || `HTTP ${res.status}`);
      return body;
    }));
    if (seq !== routeSeq) return;
    lastTrips = { key, routes };
    drawTrips();
  } catch (err) {
    if (seq === routeSeq) showStatus(`Trips: ${err.message}`, true, 5000);
  }
}

/** Redraw the trips if they still match the places, or fetch them again. */
function refreshTrips() {
  if (state.dir !== "to" || !state.tripFrom) return;
  if (!state.places.length) {
    map.getSource("route").setData({ type: "FeatureCollection", features: [] });
    dimOverlays(false);
    $("#route-section").hidden = true;
  } else if (lastTrips && lastTrips.key === tripsKey()) {
    drawTrips();
  } else {
    fetchTrips();
  }
}

function drawTrips() {
  const { routes } = lastTrips;
  const features = routes.flatMap(routeFeatures);
  map.getSource("route").setData({ type: "FeatureCollection", features });
  dimOverlays(features.length > 0);
  const weights = state.places.map((q) => q.w), total = weights.reduce((a, b) => a + b, 0);
  const complete = routes.every((r, k) => r.reachable || !weights[k]);
  const avg = total > 0 && complete ? routes.reduce((s, r, k) => s + (weights[k] ? weights[k] * r.total_s : 0), 0) / total / 60 : null;
  const how = state.mode === "car" ? "by car" : "by public transport", weighted = placesWeighted();
  showRouteSection("Trips from here");
  $("#route").replaceChildren(
    el("p", { className: "total" }, avg === null ? "—" : `${fmtMin(avg)} min`,
      el("small", {}, `${weighted ? "weighted average" : "average"}, ${how}, ${bandLabel(state.band)}`)),
    ...routes.map((r, k) => {
      const q = state.places[k];
      const trip = el("details", { className: q.w === 0 ? "trip off" : "trip" }, el("summary", {},
        el("span", { className: "pin-num" }, String(k + 1)),
        el("span", { className: "trip-name" }, `${q.label}${weighted ? ` ×${q.w}` : ""}`),
        el("span", { className: "mins" }, r.reachable ? `${fmtMin(r.total_s / 60)} min` : "not reachable")));
      if (r.reachable) trip.append(legList(r));
      return trip;
    }));
}

function setTripMarker() {
  if (!tripMarker) {
    tripMarker = new maplibregl.Marker({ element: el("div", { className: "pin dest", title: "Trips from here (drag to move)" }), draggable: true })
      .setLngLat([state.tripFrom.lon, state.tripFrom.lat]).addTo(map);
    tripMarker.on("dragend", () => {
      const p = tripMarker.getLngLat();
      state.tripFrom = { lon: p.lng, lat: p.lat }; writeHash(); fetchTrips();
    });
  }
  tripMarker.setLngLat([state.tripFrom.lon, state.tripFrom.lat]);
}

function showTripsFrom(lngLat) {
  state.tripFrom = { lon: lngLat.lng, lat: lngLat.lat };
  setTripMarker(); writeHash(); fetchTrips();
}

function dimOverlays(dim) {
  map.setPaintProperty("mrt-lines", "line-opacity", dim ? 0.35 : 1);
  map.setPaintProperty("mrt-casing", "line-opacity", dim ? 0.35 : 1);
  map.setPaintProperty("mrt-stations", "circle-opacity", dim ? 0.4 : 1);
  map.setPaintProperty("mrt-stations", "circle-stroke-opacity", dim ? 0.4 : 1);
}

/** Show the route/trips section; bring it into view when it first opens, so its Close button is seen. */
function showRouteSection(title) {
  const section = $("#route-section"), opening = section.hidden;
  $("#route-title").textContent = title;
  section.hidden = false;
  if (opening && !$("#panel").classList.contains("collapsed")) section.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function clearRoute() {
  routeSeq++;  // a route or trips still loading must not reappear
  if (state.dir === "to") {
    state.tripFrom = null; lastTrips = null;
    if (tripMarker) { tripMarker.remove(); tripMarker = null; }
  } else {
    state.dest = null; lastRoute = null;
    if (destMarker) { destMarker.remove(); destMarker = null; }
  }
  map.getSource("route").setData({ type: "FeatureCollection", features: [] });
  dimOverlays(false);
  $("#route-section").hidden = true;
  writeHash();
}

/* --- tables (the text view of the map) ------------------------------------- */

function updateTables() {
  if (!lastResult || !grid) {
    $("#area-table tbody").replaceChildren(); $("#places-table tbody").replaceChildren(); $("#timing").textContent = "";
    return;
  }
  // counted from the grid on screen, so it also covers averaged grids
  const thresholds = [15, 30, 45, 60, 90], within = thresholds.map(() => 0), vals = grid.values;
  for (let i = 0; i < vals.length; i++) {
    for (let k = 0; k < thresholds.length; k++) if (vals[i] <= thresholds[k] * 10) within[k]++;
  }
  const cellKm2 = (grid.res_m / 1000) ** 2;
  $("#area-table tbody").replaceChildren(...thresholds.map((m, k) => el("tr", {},
    el("td", {}, `${m} min`),
    el("td", { className: "num" }, `${fmtKm2(within[k] * cellKm2)} km²`),
    el("td", { className: "num" }, fmtPct((100 * within[k]) / grid.land_cells)))));
  const t = lastResult.timing_ms, n = state.places.length;
  $("#timing").textContent = t
    ? `Computed in ${t.search + t.grid} ms${t.cached ? " (reused search)" : ""} · ${grid.res_m} m grid`
    : `Weighted average of ${n} place${n === 1 ? "" : "s"} · ${grid.res_m} m grid`;

  const rows = meta.places.map((p) => {
    const cell = cellAt(p.lon, p.lat);
    const v = cell ? cell.v : NO_DATA;
    return { ...p, minutes: v >= UNREACHED ? null : v / 10 };
  }).sort((a, b) => (a.minutes ?? 1e9) - (b.minutes ?? 1e9));
  const button = (label, title, onClick) => {
    const b = el("button", { type: "button", className: "link-btn", title }, label);
    b.addEventListener("click", onClick);
    return b;
  };
  $("#places-table tbody").replaceChildren(...rows.map((p) => {
    const actions = state.dir === "to"
      ? [button("Trips", `Trips from ${p.name} to your places`, () => showTripsFrom({ lng: p.lon, lat: p.lat })),
        button("Add", `Add ${p.name} as a place`, () => addPlace({ lng: p.lon, lat: p.lat }))]
      : [button("Route", `Route to ${p.name}`, () => { state.dest = { lon: p.lon, lat: p.lat }; setDestMarker(); writeHash(); fetchRoute(); }),
        button("Start", `Start from ${p.name}`, () => {
          state.origin = { lon: p.lon, lat: p.lat }; setOriginMarker(); writeHash(); compute();
          map.easeTo({ center: [p.lon, p.lat] });
        })];
    return el("tr", {},
      el("td", {}, p.name),
      el("td", { className: p.minutes === null ? "num muted" : "num" }, p.minutes === null ? "—" : fmtMin(p.minutes)),
      el("td", { className: "actions" }, ...actions));
  }));
}

/* --- key destinations ------------------------------------------------------ */

function renderKeyDestinations() {
  // every profile's summary comes with each result, so switching profile needs no new search
  const k = lastResult && lastResult.key_destinations && lastResult.key_destinations[state.profile];
  const show = !!(k && k.weighted_min !== null);
  $("#key-section").hidden = !show;
  $("#key-score").hidden = !show;
  if (!show) return;
  const profile = meta.key_destinations.find((q) => q.key === state.profile);
  $("#key-profile-desc").textContent = profile.description;
  $("#key-score-profile").textContent = profile.name;
  // weights are shown as shares, whatever scale the file uses
  const total = k.groups.reduce((s, g) => s + g.weight, 0);
  const share = (w) => `${+((100 * w) / total).toFixed(1)}%`;
  const mins = (m) => (m === null ? "—" : fmtMin(m));
  $("#key-score-value").textContent = `${fmtMin(k.weighted_min)} min`;
  $("#key-total").textContent = `${fmtMin(k.weighted_min)} min`;
  $("#key-groups tbody").replaceChildren(...k.groups.map((g) => el("tr", {},
    el("td", {}, g.name), el("td", { className: "num" }, share(g.weight)), el("td", { className: "num" }, mins(g.minutes)))));
  const items = [...k.items].sort((a, b) => b.weight - a.weight || a.name.localeCompare(b.name));
  $("#key-items tbody").replaceChildren(...items.map((d) => {
    const route = el("button", { type: "button", className: "link-btn", title: `Route to ${d.name}` }, "Route");
    route.addEventListener("click", () => { state.dest = { lon: d.lon, lat: d.lat }; setDestMarker(); writeHash(); fetchRoute(); });
    return el("tr", {},
      el("td", {}, d.name, el("small", {}, d.group)),
      el("td", { className: "num" }, share(d.weight)),
      el("td", { className: d.minutes === null ? "num muted" : "num" }, mins(d.minutes)),
      el("td", { className: "actions" }, route));
  }));
  $("#key-count").textContent = `All ${k.items.length} destinations`;
  const unreached = $("#key-unreached");
  unreached.hidden = !k.unreachable;
  unreached.textContent = `${k.unreachable} of them can't be reached within ${meta.max_minutes} minutes, and count as ${meta.max_minutes}.`;
  const skipped = profile.skipped;
  $("#key-skipped").hidden = !skipped.length;
  $("#key-skipped").textContent = `Skipped, as no station has this name and no coordinates were given: ${skipped.join(", ")}.`;
}

function showKeySection() {
  const panel = $("#panel");
  if (panel.classList.contains("collapsed")) $("#panel-toggle").click();
  $("#key-section").scrollIntoView({ behavior: "smooth", block: "start" });
}

function buildAbout() {
  const s = meta.sources, box = $("#about");
  const date = (iso) => (iso ? new Date(iso).toLocaleDateString("en-SG", { day: "numeric", month: "short", year: "numeric" }) : "?");
  const sources = el("ul", {},
    el("li", {}, `Buses: LTA DataMall stops, services, routes and headways (fetched ${date(s.lta_datamall?.fetched_at)}).`),
    el("li", {}, `Trains: LTA's official GTFS timetable (published ${date(s.lta_datamall?.train_gtfs_timestamp)}), service day ${meta.service_date}.`),
    el("li", {}, `Walking & roads: OpenStreetMap extract (${s.openstreetmap?.last_modified ? date(s.openstreetmap.last_modified) : "?"}).`),
    el("li", {}, `HDB void decks: HDB building footprints (data.gov.sg); land outline: URA Master Plan 2019.`),
    el("li", {}, `Bus route lines for the overlay: busrouter.sg (mirror of LTA data, ${date(s.busrouter?.last_updated)}).`),
  );
  const model = el("ul", {},
    el("li", {}, "Public transport: walk along real footpaths (and through HDB void decks), wait, ride, change. One shortest-path search per click."),
    el("li", {}, "Bus waits come from LTA's published frequency for each service and time band (most run more often in the peaks); running times from LTA's scheduled times, adjusted for peak traffic."),
    el("li", {}, "Train waits come from each line's published peak and off-peak frequency (below): half the gap between trains on average, the longest gap at worst. Running times come from the official timetable, calibrated per line as the feed rounds them up to whole minutes. Interchange walks use the Reddit-measured timings, scaled by walking speed."),
    el("li", {}, "Getting between the street and a platform takes a time per station (below), scaled by walking speed: deeper stations take longer. Entrances well away from the platform add the extra walk."),
    el("li", {}, "Car: typical-congestion speeds by road class for the time band, with peaks calibrated to LTA's measured peak-hour averages (no live traffic). The start and end are joined to the road network on foot."),
    el("li", {}, "Limitations: no real-time data; boarding the first of several buses that go your way is not modelled (waits can be pessimistic at busy stops); cross-border and ferry services are excluded."),
  );
  box.append(el("h3", {}, "Data"), sources, el("h3", {}, "Model"), model);
  if (meta.train_frequencies.length) {
    // the published frequencies behind train waits (data/manual/train_frequencies.csv)
    const rows = meta.train_frequencies.map((f) => el("tr", {},
      el("td", {}, f.section === "whole line" ? f.line : `${f.line}: ${f.section}`),
      el("td", { className: "num" }, f.peak), el("td", { className: "num" }, f.offpeak)));
    box.append(el("h3", {}, "Minutes between trains"),
      el("table", { className: "data" },
        el("thead", {}, el("tr", {}, el("th", {}, "Line"), el("th", { className: "num" }, "Peak"), el("th", { className: "num" }, "Off-peak"))),
        el("tbody", {}, ...rows)),
      el("p", { className: "note" }, "Operators' published frequencies (peak 07:30–09:30 and 17:30–19:30), as listed on SGWiki; LTA quotes 2–3 minutes at peak and 5–7 off-peak network-wide. The Bukit Panjang LRT publishes none, so it uses the timetable's gaps."));
  }
  const access = meta.station_access.rows;
  if (access.length) {
    // street-to-platform times (data/manual/station_access.csv), at the default walking pace
    const minutes = (s) => +(s / 60).toFixed(1);
    const fmtS = (s) => (s < 90 ? `${s} s` : `${minutes(s)} min`);
    const depths = access.filter((r) => r.depth_m !== null);
    const rows = access.filter((r) => r.depth_m === null).sort((a, b) => a.seconds - b.seconds).map((r) => el("tr", {},
      el("td", {}, r.label), el("td", { className: "num" }, fmtS(r.seconds))));
    if (depths.length) {
      const [lo, hi] = [depths.reduce((a, b) => (b.depth_m < a.depth_m ? b : a)), depths.reduce((a, b) => (b.depth_m > a.depth_m ? b : a))];
      rows.push(el("tr", {},
        el("td", {}, `${depths.length} stations with a published depth, from ${lo.label} (${lo.depth_m} m) to ${hi.label} (${hi.depth_m} m)`),
        el("td", { className: "num" }, `${minutes(lo.seconds)}–${minutes(hi.seconds)} min`)));
    }
    box.append(el("h3", {}, "Street to platform"),
      el("table", { className: "data" },
        el("thead", {}, el("tr", {}, el("th", {}, "Stations"), el("th", { className: "num" }, `At ${meta.walk_kmh.default} km/h`))),
        el("tbody", {}, ...rows)),
      el("p", { className: "note" }, `Escalators, stairs, fare gates and corridors, either way. A published depth counts ${meta.station_access.s_per_m} s per metre: escalators at 0.75 m/s up a 30° slope, plus the walks between flights. Other stations take a default by line.`));
  }
}

/* --- start ------------------------------------------------------------------ */

async function main() {
  try {
    const res = await fetch("/api/meta");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    meta = await res.json();
  } catch (err) {
    showStatus(`Could not reach the travel-time server (${err.message}). Is it running?`, true);
    return;
  }
  readHash();
  applyTheme();
  buildControls();
  buildAbout();
  if (innerWidth <= 700) $("#coverage").open = false;  // keep the map visible on phones
  renderLegend();
  initMap();
}

main();
