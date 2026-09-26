"use strict";

/* ---------------------------------------------------------------------------
 * Colour: one-hue sequential blue (dataviz reference ramp, steps 100-700).
 * The encoded quantity is how quickly a place is reached: the start point is
 * the strong end and colour fades toward the basemap as travel time grows, so
 * the shading dissolves at the cut-off instead of ending on its strongest
 * colour. Light theme: quick = dark, slow = light; the dark theme flips the
 * anchor so the slow end recedes toward the dark basemap. Band mode uses five
 * steps that pass the ordinal checks (monotone lightness, visible gaps).
 * ------------------------------------------------------------------------- */
const BLUE = {
  100: "#cde2fb", 150: "#b7d3f6", 200: "#9ec5f4", 250: "#86b6ef", 300: "#6da7ec", 350: "#5598e7",
  400: "#3987e5", 450: "#2a78d6", 500: "#256abf", 550: "#1c5cab", 600: "#184f95", 650: "#104281", 700: "#0d366b",
};
const LIGHT_TO_DARK = [100, 150, 200, 250, 300, 350, 400, 450, 500, 550, 600, 650, 700];
// ramps ordered from 0 minutes to the cut-off
const SMOOTH_STEPS = { light: [...LIGHT_TO_DARK].reverse(), dark: LIGHT_TO_DARK };
const BAND_STEPS = { light: [700, 550, 450, 350, 250], dark: [100, 200, 300, 450, 600] };
const BAND_COUNT = 5;

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

const $ = (sel) => document.querySelector(sel);
const el = (tag, props = {}, ...children) => {
  const node = Object.assign(document.createElement(tag), props);
  for (const c of children) node.append(c);
  return node;
};

const state = {
  origin: null, dest: null,
  mode: "transit", band: "am_peak", wait: "avg", walk: 4.8, res: "med",
  bus: true, rail: true, voiddeck: true, parking: 0,
  style: "smooth", maxMin: 90, bandSize: 10, opacity: 0.7,
  ovMrt: true, ovBus: false, ovStops: false,
  theme: "auto", basemap: "onemap",
  view: null,  // [lat, lon, zoom] restored from the URL
};

let meta = null, map = null, grid = null, lastResult = null;
let reqSeq = 0, routeSeq = 0, busyTimer = null;
let originMarker = null, destMarker = null, lastGoodOrigin = null;
let lineColours = {};
const loadedOverlays = new Set();

/* --- utilities -------------------------------------------------------------- */

const hexRgb = (h) => { const n = parseInt(h.slice(1), 16); return [(n >> 16) & 255, (n >> 8) & 255, n & 255]; };
const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const isDark = () => state.theme === "dark" || (state.theme === "auto" && matchMedia("(prefers-color-scheme: dark)").matches);
const fmtMin = (m) => (m < 10 ? m.toFixed(1) : Math.round(m).toString());
const bandLabel = (key) => meta.bands.find((b) => b.key === key).label;

function debounce(fn, ms) {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}

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
  if (state.origin) p.set("o", `${state.origin.lat.toFixed(5)},${state.origin.lon.toFixed(5)}`);
  if (state.dest) p.set("d", `${state.dest.lat.toFixed(5)},${state.dest.lon.toFixed(5)}`);
  p.set("mode", state.mode); p.set("band", state.band); p.set("wait", state.wait);
  p.set("walk", state.walk); p.set("res", state.res); p.set("park", state.parking);
  p.set("use", (state.bus ? "b" : "") + (state.rail ? "r" : "") + (state.voiddeck ? "v" : ""));
  p.set("style", state.style); p.set("max", state.maxMin); p.set("bw", state.bandSize);
  p.set("ov", (state.ovMrt ? "m" : "") + (state.ovBus ? "b" : "") + (state.ovStops ? "s" : ""));
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
  const pick = (k, allowed) => (allowed.includes(p.get(k)) ? p.get(k) : null);
  state.mode = pick("mode", ["transit", "car"]) || state.mode;
  state.band = pick("band", meta.bands.map((b) => b.key)) || state.band;
  state.wait = pick("wait", meta.wait_modes) || state.wait;
  state.res = pick("res", meta.resolutions.map((r) => r.key)) || state.res;
  state.style = pick("style", ["smooth", "bands"]) || state.style;
  const num = (k, lo, hi, dflt) => { const v = Number(p.get(k)); return p.has(k) && v >= lo && v <= hi ? v : dflt; };
  state.walk = num("walk", meta.walk_kmh.min, meta.walk_kmh.max, meta.walk_kmh.default);
  state.parking = meta.parking_min.includes(num("park", 0, 30, -1)) ? num("park", 0, 30, 0) : 0;
  state.maxMin = num("max", 30, 150, state.maxMin);
  state.bandSize = [5, 10, 15, 20].includes(num("bw", 5, 20, -1)) ? num("bw", 5, 20, 10) : state.bandSize;
  if (p.has("use")) { const u = p.get("use"); state.bus = u.includes("b"); state.rail = u.includes("r"); state.voiddeck = u.includes("v"); }
  if (p.has("ov")) { const o = p.get("ov"); state.ovMrt = o.includes("m"); state.ovBus = o.includes("b"); state.ovStops = o.includes("s"); }
  try {
    state.theme = localStorage.getItem("theme") || "auto";
    state.basemap = localStorage.getItem("basemap") === "esri" ? "esri" : "onemap";
  } catch { /* storage unavailable: keep defaults */ }
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
  for (const m of [5, 10, 15, 20]) bw.append(segOption("bandSize", String(m), `${m} min`));

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
  check("mode", state.mode); check("band", state.band); check("wait", state.wait); check("res", state.res);
  check("parking", String(state.parking)); check("style", state.style); check("bandSize", String(state.bandSize));
  walk.value = state.walk;
  $("#max-min").value = state.maxMin;
  $("#opacity").value = state.opacity;
  $("#use-bus").checked = state.bus; $("#use-rail").checked = state.rail; $("#use-voiddeck").checked = state.voiddeck;
  $("#ov-mrt").checked = state.ovMrt; $("#ov-bus").checked = state.ovBus; $("#ov-stops").checked = state.ovStops;
  $("#theme").value = state.theme;
  syncControlVisibility();

  // events
  const recompute = () => { writeHash(); compute(); };
  const recomputeSlow = debounce(recompute, 250);
  document.querySelectorAll('input[type="radio"]').forEach((input) => input.addEventListener("change", () => {
    const { name, value } = input;
    if (name === "parking" || name === "bandSize") state[name] = Number(value); else state[name] = value;
    syncControlVisibility();
    if (["style", "bandSize"].includes(name)) { writeHash(); render(); renderLegend(); updateTables(); }
    else recompute();
  }));
  walk.addEventListener("input", () => { state.walk = Number(walk.value); updateWalkOut(); recomputeSlow(); });
  $("#max-min").addEventListener("input", (e) => { state.maxMin = Number(e.target.value); updateMaxOut(); writeHash(); render(); renderLegend(); });
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
  const transit = state.mode === "transit";
  document.querySelectorAll(".transit-only").forEach((n) => (n.hidden = !transit));
  document.querySelectorAll(".car-only").forEach((n) => (n.hidden = transit));
  $("#max-row").hidden = state.style !== "smooth";
  $("#bandsize-row").hidden = state.style !== "bands";
  const notes = {
    best: "No waiting at all: every bus and train arrives as you reach the stop.",
    avg: "Expected wait for someone who turns up at a random time.",
    worst: "You just missed each bus and train: the longest scheduled gap.",
  };
  $("#wait-note").textContent = notes[state.wait];
}

const updateWalkOut = () => { $("#walk-out").textContent = `${state.walk.toFixed(1)} km/h`; };
const updateMaxOut = () => { $("#max-out").textContent = `${state.maxMin} min`; };

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
    writeHash();
    compute();
  });
  map.on("click", (e) => {
    state.origin = { lon: e.lngLat.lng, lat: e.lngLat.lat };
    setOriginMarker(); writeHash(); compute();
  });
  map.on("contextmenu", (e) => {
    e.originalEvent.preventDefault();
    state.dest = { lon: e.lngLat.lng, lat: e.lngLat.lat };
    setDestMarker(); writeHash(); fetchRoute();
  });
  map.on("mousemove", onHover);
  map.on("moveend", debounce(writeHash, 300));
  map.getCanvas().addEventListener("mouseleave", hideHover);
}

function addLayers() {
  const blank = document.createElement("canvas");
  blank.width = blank.height = 1;  // fully transparent until the first result arrives
  map.addSource("heat", {
    type: "image", url: blank.toDataURL(),
    coordinates: [[SG_BOUNDS[0], SG_BOUNDS[3]], [SG_BOUNDS[2], SG_BOUNDS[3]], [SG_BOUNDS[2], SG_BOUNDS[1]], [SG_BOUNDS[0], SG_BOUNDS[1]]],
  });
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

function apiParams(extra = {}) {
  return new URLSearchParams({
    lat: state.origin.lat, lon: state.origin.lon, mode: state.mode, band: state.band, wait: state.wait,
    walk_kmh: state.walk, res: state.res, bus: state.bus, rail: state.rail, voiddeck: state.voiddeck,
    parking: state.parking, ...extra,
  });
}

async function compute() {
  if (!state.origin || !map) return;
  const seq = ++reqSeq;
  clearTimeout(busyTimer);
  busyTimer = setTimeout(() => { if (seq === reqSeq) { applyHeatOpacity(true); showStatus("Computing travel times…"); } }, 150);
  try {
    const res = await fetch(`/api/isochrone?${apiParams()}`);
    const body = await res.json();
    if (seq !== reqSeq) return;
    if (!res.ok) throw new Error(body.detail || `HTTP ${res.status}`);
    lastResult = body;
    lastGoodOrigin = { ...state.origin };
    grid = decodeGrid(body.grid);
    showStatus("");
    render(); renderLegend(); updateTables();
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
    if (seq === reqSeq) { clearTimeout(busyTimer); applyHeatOpacity(false); }
  }
}

function decodeGrid(g) {
  const bin = atob(g.data);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return { ...g, values: new Uint16Array(bytes.buffer), data: undefined };
}

function capMinutes() { return state.style === "smooth" ? state.maxMin : state.bandSize * BAND_COUNT; }

function colourFor(minutes, cap) {
  const theme = isDark() ? "dark" : "light";
  if (state.style === "bands") {
    const steps = BAND_STEPS[theme];
    return hexRgb(BLUE[steps[Math.min(BAND_COUNT - 1, Math.floor(minutes / state.bandSize))]]);
  }
  const steps = SMOOTH_STEPS[theme].map((s) => hexRgb(BLUE[s]));
  const x = Math.min(1, Math.max(0, minutes / cap)) * (steps.length - 1);
  const i = Math.min(Math.floor(x), steps.length - 2), t = x - i;
  return steps[i].map((c, k) => Math.round(c + (steps[i + 1][k] - c) * t));
}

function render() {
  if (!grid || !map || !map.getSource("heat")) return;
  const cap = capMinutes();
  const lut = new Uint32Array(cap * 10 + 1);
  for (let v = 0; v < lut.length; v++) {
    const [r, g, b] = colourFor(v / 10, cap);
    lut[v] = ((255 << 24) | (b << 16) | (g << 8) | r) >>> 0;
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
  const cap = capMinutes();
  if (state.style === "smooth") {
    const steps = SMOOTH_STEPS[isDark() ? "dark" : "light"];
    const bar = el("div", { className: "bar" });
    bar.style.background = `linear-gradient(to right, ${steps.map((s, i) => `${BLUE[s]} ${(i / (steps.length - 1)) * 100}%`).join(", ")})`;
    const axis = el("div", { className: "axis" });
    const step = cap <= 45 ? 10 : cap <= 100 ? 15 : 30;
    for (let m = 0; m <= cap; m += step) addTick(axis, `${m}`, m / cap);
    box.append(bar, axis);
  } else {
    const steps = BAND_STEPS[isDark() ? "dark" : "light"];
    const sw = el("div", { className: "swatches" });
    for (const s of steps) { const d = el("div"); d.style.background = BLUE[s]; sw.append(d); }
    const axis = el("div", { className: "axis" });
    for (let i = 0; i <= BAND_COUNT; i++) addTick(axis, `${i * state.bandSize}`, i / BAND_COUNT);
    box.append(sw, axis);
  }
  box.append(el("div", { className: "caption" }, "Minutes from the start · ", el("span", { className: "nodata" }),
    `over ${cap}`));
  const how = state.mode === "car" ? "by car" : "by public transport";
  $("#legend-title").textContent = meta ? `Travel time ${how} · ${bandLabel(state.band)}` : "Travel time";
}

/* --- hover ------------------------------------------------------------------ */

function cellAt(lng, lat) {
  if (!grid) return null;
  const [w, s, e, n] = grid.bounds;
  const col = Math.floor(((lng - w) / (e - w)) * grid.nx);
  const row = Math.floor(((n - lat) / (n - s)) * grid.ny);
  if (col < 0 || row < 0 || col >= grid.nx || row >= grid.ny) return null;
  const dLon = (e - w) / grid.nx, dLat = (n - s) / grid.ny;
  const x0 = w + col * dLon, y0 = n - row * dLat;
  return { v: grid.values[row * grid.nx + col], ring: [[x0, y0], [x0 + dLon, y0], [x0 + dLon, y0 - dLat], [x0, y0 - dLat], [x0, y0]] };
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
    if (cell.v === UNREACHED) tip.append(el("strong", {}, `Over ${lastResult ? meta.max_minutes : ""} min`));
    else tip.append(el("strong", {}, `${fmtMin(cell.v / 10)} min`));
    tip.append(el("span", { className: "k" }, `${state.mode === "car" ? "by car" : "by public transport"}, ${bandLabel(state.band)}`));
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

let lastRoute = null;

async function fetchRoute() {
  if (!state.origin || !state.dest) return;
  const seq = ++routeSeq;
  try {
    const res = await fetch(`/api/route?${apiParams({ to_lat: state.dest.lat, to_lon: state.dest.lon })}`);
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

function drawRoute(route) {
  const features = route.reachable ? route.legs.filter((l) => l.coords && l.coords.length > 1).map((l) => ({
    type: "Feature", properties: { type: l.type === "transfer" ? "walk" : l.type, colour: legColour(l) },
    geometry: { type: "LineString", coordinates: l.coords },
  })) : [];
  map.getSource("route").setData({ type: "FeatureCollection", features });
  dimOverlays(features.length > 0);

  const box = $("#route");
  box.replaceChildren();
  $("#route-section").hidden = false;
  if (!route.reachable) {
    box.append(el("p", { className: "note" }, `Not reachable within ${meta.max_minutes} minutes with these settings.`));
    return;
  }
  const how = state.mode === "car" ? "by car" : "by public transport";
  box.append(el("p", { className: "total" }, `${fmtMin(route.total_s / 60)} min`, el("small", {}, `${how}, ${bandLabel(state.band)}`)));
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
  box.append(list);
}

function dimOverlays(dim) {
  map.setPaintProperty("mrt-lines", "line-opacity", dim ? 0.35 : 1);
  map.setPaintProperty("mrt-casing", "line-opacity", dim ? 0.35 : 1);
  map.setPaintProperty("mrt-stations", "circle-opacity", dim ? 0.4 : 1);
  map.setPaintProperty("mrt-stations", "circle-stroke-opacity", dim ? 0.4 : 1);
}

function clearRoute() {
  state.dest = null; lastRoute = null;
  if (destMarker) { destMarker.remove(); destMarker = null; }
  map.getSource("route").setData({ type: "FeatureCollection", features: [] });
  dimOverlays(false);
  $("#route-section").hidden = true;
  writeHash();
}

/* --- tables (the text view of the map) ------------------------------------- */

function updateTables() {
  if (!lastResult || !grid) return;
  const areaBody = $("#area-table tbody");
  areaBody.replaceChildren();
  for (const [m, km2] of Object.entries(lastResult.area_km2)) {
    areaBody.append(el("tr", {}, el("td", {}, `${m} min`), el("td", { className: "num" }, `${km2.toFixed(1)} km²`)));
  }
  const t = lastResult.timing_ms;
  $("#timing").textContent = `Computed in ${t.search + t.grid} ms${t.cached ? " (reused search)" : ""} · ${grid.res_m} m grid`;

  const rows = meta.places.map((p) => {
    const cell = cellAt(p.lon, p.lat);
    const v = cell ? cell.v : NO_DATA;
    return { ...p, minutes: v >= UNREACHED ? null : v / 10 };
  }).sort((a, b) => (a.minutes ?? 1e9) - (b.minutes ?? 1e9));
  const body = $("#places-table tbody");
  body.replaceChildren();
  for (const p of rows) {
    const route = el("button", { type: "button", className: "link-btn", title: `Route to ${p.name}` }, "Route");
    route.addEventListener("click", () => { state.dest = { lon: p.lon, lat: p.lat }; setDestMarker(); writeHash(); fetchRoute(); });
    const start = el("button", { type: "button", className: "link-btn", title: `Start from ${p.name}` }, "Start");
    start.addEventListener("click", () => {
      state.origin = { lon: p.lon, lat: p.lat }; setOriginMarker(); writeHash(); compute();
      map.easeTo({ center: [p.lon, p.lat] });
    });
    body.append(el("tr", {},
      el("td", {}, p.name),
      el("td", { className: p.minutes === null ? "num muted" : "num" }, p.minutes === null ? "—" : fmtMin(p.minutes)),
      el("td", { className: "actions" }, route, start)));
  }
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
    el("li", {}, "Bus waits come from LTA's published headway ranges per time band; running times from LTA's scheduled times, adjusted for peak traffic."),
    el("li", {}, "Train waits and running times come from the official timetable (running times calibrated per line, as the feed rounds them up to whole minutes); interchange walks use the Reddit-measured timings, scaled by walking speed."),
    el("li", {}, "Car: typical-congestion speeds by road class for the time band, with peaks calibrated to LTA's measured peak-hour averages (no live traffic). The start and end are joined to the road network on foot."),
    el("li", {}, "Limitations: no real-time data; boarding the first of several buses that go your way is not modelled (waits can be pessimistic at busy stops); cross-border and ferry services are excluded."),
  );
  box.append(el("h3", {}, "Data"), sources, el("h3", {}, "Model"), model);
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
  renderLegend();
  initMap();
}

main();
