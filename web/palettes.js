"use strict";

/* ---------------------------------------------------------------------------
 * Heatmap colour schemes.
 *
 * Most schemes are one-way ramps ("fade"), listed from the strong (dark) end to
 * the weak (light) end. The start point takes whichever end stands out from the
 * basemap, and colour fades toward the basemap as travel time grows: quick =
 * dark in the light theme, quick = light in the dark theme. The two-sided
 * schemes carry meaning in their hues (green = quick, red = slow), so they are
 * listed from 0 minutes to the cut-off and keep that order in both themes.
 * Reverse flips either kind.
 *
 * Smooth shading uses the whole ramp, so it dissolves at the cut-off. Bands are
 * spread evenly along it, after trimming any ramp end that sits too close to the
 * panel surface to see (OKLab distance x100 under MIN_BAND_DE). That rule
 * reproduces the Blues band steps validated for ordinal use: lightest 250 in the
 * light theme, darkest 600 in the dark theme.
 * ------------------------------------------------------------------------- */
const BLUE = {
  100: "#cde2fb", 150: "#b7d3f6", 200: "#9ec5f4", 250: "#86b6ef", 300: "#6da7ec", 350: "#5598e7",
  400: "#3987e5", 450: "#2a78d6", 500: "#256abf", 550: "#1c5cab", 600: "#184f95", 650: "#104281", 700: "#0d366b",
};

const PALETTES = [
  // one hue: the dataviz reference ramp, steps 700 -> 100
  { key: "blues", name: "Blues", fade: true,
    stops: [700, 650, 600, 550, 500, 450, 400, 350, 300, 250, 200, 150, 100].map((s) => BLUE[s]) },
  // matplotlib's perceptually uniform maps (CC0), 11 samples each
  { key: "viridis", name: "Viridis", fade: true,
    stops: ["#440154", "#482475", "#414487", "#355f8d", "#2a788e", "#21918c", "#22a884", "#44bf70", "#7ad151", "#bddf26", "#fde725"] },
  { key: "plasma", name: "Plasma", fade: true,
    stops: ["#0d0887", "#41049d", "#6a00a8", "#8f0da4", "#b12a90", "#cc4778", "#e16462", "#f2844b", "#fca636", "#fcce25", "#f0f921"] },
  // ColorBrewer (Cynthia Brewer, Mark Harrower and Penn State; Apache-2.0), 9 classes
  { key: "ylorrd", name: "Yellow–orange–red", fade: true,
    stops: ["#800026", "#bd0026", "#e31a1c", "#fc4e2a", "#fd8d3c", "#feb24c", "#fed976", "#ffeda0", "#ffffcc"] },
  { key: "ylgnbu", name: "Yellow–green–blue", fade: true,
    stops: ["#081d58", "#253494", "#225ea8", "#1d91c0", "#41b6c4", "#7fcdbb", "#c7e9b4", "#edf8b1", "#ffffd9"] },
  // two-sided: ColorBrewer RdBu's blue arm and PuOr's orange arm around the shared near-white midpoint
  { key: "buwhor", name: "Blue–white–orange", fade: false,
    stops: ["#2166ac", "#4393c3", "#92c5de", "#d1e5f0", "#f7f7f7", "#fee0b6", "#fdb863", "#e08214", "#b35806"] },
  // ColorBrewer RdYlGn, green first. Red and green ends look alike to many colour-blind people.
  { key: "gnylrd", name: "Green–yellow–red", fade: false, cvdWarning: true,
    stops: ["#1a9850", "#66bd63", "#a6d96a", "#d9ef8b", "#ffffbf", "#fee08b", "#fdae61", "#f46d43", "#d73027"] },
];
const MIN_BAND_DE = 24;

const hexRgb = (h) => { const n = parseInt(h.slice(1), 16); return [(n >> 16) & 255, (n >> 8) & 255, n & 255]; };
const rgbCss = ([r, g, b]) => `rgb(${r}, ${g}, ${b})`;

function oklab([r, g, b]) {
  const lin = (c) => { c /= 255; return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4; };
  const R = lin(r), G = lin(g), B = lin(b);
  const l = Math.cbrt(0.4122214708 * R + 0.5363325363 * G + 0.0514459929 * B);
  const m = Math.cbrt(0.2119034982 * R + 0.6806995451 * G + 0.1073969566 * B);
  const s = Math.cbrt(0.0883024619 * R + 0.2817188376 * G + 0.6299787005 * B);
  return [0.2104542553 * l + 0.793617785 * m - 0.0040720468 * s,
    1.9779984951 * l - 2.428592205 * m + 0.4505937099 * s,
    0.0259040371 * l + 0.7827717662 * m - 0.808675766 * s];
}

const deltaE = (a, b) => { const p = oklab(a), q = oklab(b); return 100 * Math.hypot(p[0] - q[0], p[1] - q[1], p[2] - q[2]); };

/** The scheme's stops as [r, g, b], ordered from 0 minutes to the cut-off. */
function rampStops(key, dark, reverse) {
  const p = PALETTES.find((q) => q.key === key) || PALETTES[0];
  const stops = p.stops.map(hexRgb);
  if (p.fade && dark) stops.reverse();
  if (reverse) stops.reverse();
  return stops;
}

/** Colour at t (0 = start, 1 = cut-off), linear between neighbouring stops. */
function sampleRamp(stops, t) {
  const x = Math.min(1, Math.max(0, t)) * (stops.length - 1);
  const i = Math.min(Math.floor(x), stops.length - 2), f = x - i;
  return stops[i].map((c, k) => Math.round(c + (stops[i + 1][k] - c) * f));
}

/** Colours for n bands, evenly spaced over the part of the ramp that stands out from `surface`. */
function bandColours(stops, n, surface) {
  const visible = (t) => deltaE(sampleRamp(stops, t), surface) >= MIN_BAND_DE;
  let lo = 0, hi = 1;
  while (lo < 0.45 && !visible(lo)) lo += 0.01;
  while (hi > 0.55 && !visible(hi)) hi -= 0.01;
  return Array.from({ length: n }, (_, i) => sampleRamp(stops, n > 1 ? lo + ((hi - lo) * i) / (n - 1) : lo));
}
