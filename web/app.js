/* The two drawings on the page that need a browser: the load test chart and the audit
   chain. No framework and no charting library, like the other project pages on
   peterparker.ca: SVG built with the DOM API, every colour a class in style.css, every
   piece of text set with textContent.

   Two requests, both to this server: data/site.json, written by `boundary site` from the
   README's tables, and /audit/head, the proxy's live chain head. If the second fails, the
   page says so and draws the chain from the last published anchor instead. */

"use strict";

const SVG = "http://www.w3.org/2000/svg";
const LAYERS = [
  ["routing", "Routing only"],
  ["audit", "Plus the audit record"],
  ["redaction", "Plus redaction"],
  ["cache", "Cache, on a miss"],
];

function el(name, attrs, text) {
  const node = document.createElementNS(SVG, name);
  for (const [k, v] of Object.entries(attrs || {})) node.setAttribute(k, String(v));
  if (text !== undefined) node.textContent = text;
  return node;
}

function html(name, cls, text) {
  const node = document.createElement(name);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
}

function fail(message) {
  const box = document.getElementById("failure");
  box.textContent = message;
  box.hidden = false;
}

async function getJson(path) {
  const r = await fetch(path, { credentials: "omit", cache: "no-cache" });
  if (!r.ok) throw new Error(`${path}: ${r.status}`);
  return r.json();
}

/* ------------------------------------------------------------------ the load test */

function drawLoad(rows, pct) {
  const host = document.getElementById("load-chart");
  host.replaceChildren();
  const W = 720, H = 360, L = 64, R = 170, T = 18, B = 46;
  const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, role: "img",
    "aria-label": `Extra milliseconds per request at the ${pct}, by control and load` });
  const rates = [...new Set(rows.map((r) => r.rps))].sort((a, b) => a - b);
  const values = rows.flatMap((r) => (r[pct] ? [r[pct][1], r[pct][2]] : []));
  const lo = 1, hi = Math.max(10, ...values) * 1.25;
  const y = (v) => T + (H - T - B) * (1 - (Math.log10(Math.max(v, lo)) - Math.log10(lo)) /
    (Math.log10(hi) - Math.log10(lo)));
  const x = (i) => L + ((W - L - R) * (i + 0.5)) / rates.length;

  for (const tick of [1, 2, 5, 10, 20, 50, 100, 200, 500, 1000]) {
    if (tick > hi) break;
    svg.append(el("line", { x1: L, x2: W - R, y1: y(tick), y2: y(tick), class: "gridline" }));
    svg.append(el("text", { x: L - 8, y: y(tick) + 4, "text-anchor": "end", class: "tick" }, `${tick}`));
  }
  svg.append(el("text", { x: 14, y: T + (H - T - B) / 2, class: "axis-title",
    transform: `rotate(-90 14 ${T + (H - T - B) / 2})`, "text-anchor": "middle" }, "ms added, log scale"));
  rates.forEach((rate, i) => {
    svg.append(el("text", { x: x(i), y: H - B + 22, "text-anchor": "middle", class: "tick" }, `${rate} a second`));
  });

  const ends = [];
  LAYERS.forEach(([layer, label], k) => {
    const cells = rates.map((rate) => rows.find((r) => r.layer === layer && r.rps === rate));
    const pts = [];
    cells.forEach((c, i) => {
      if (c && c[pct]) pts.push([x(i), y(c[pct][0]), c, i]);
    });
    if (pts.length > 1) {
      svg.append(el("polyline", { points: pts.map((p) => `${p[0]},${p[1]}`).join(" "),
        class: `series l${k}` }));
    }
    for (const [px, py, c] of pts) {
      svg.append(el("line", { x1: px, x2: px, y1: y(c[pct][1]), y2: y(c[pct][2]), class: `whisker l${k}` }));
      const dot = el("circle", { cx: px, cy: py, r: 4.5, class: `dot l${k}`, tabindex: 0 });
      dot.append(el("title", {}, `${label}, ${c.rps} a second: ${c[pct][0]} ms (${c[pct][1]} to ${c[pct][2]}); ${c.errors} failed`));
      svg.append(dot);
    }
    cells.forEach((c, i) => {
      if (c && !c[pct]) {
        // On the floor of the chart, where no line runs: a cell with no figure.
        const t = el("text", { x: x(i), y: H - B - 8, "text-anchor": "middle", class: `shed halo l${k}` },
          "sheds load: no figure");
        t.append(el("title", {}, `${label}, ${c.rps} a second: ${c.note}`));
        svg.append(t);
      }
    });
    const last = pts[pts.length - 1];
    if (last) ends.push({ y: last[1], x: last[0], k, label });
  });
  // Each line's name beside its last point, nudged apart so that two lines ending close
  // together keep both names readable.
  ends.sort((a, b) => a.y - b.y);
  for (let i = 1; i < ends.length; i++) {
    if (ends[i].y - ends[i - 1].y < 15) ends[i].y = ends[i - 1].y + 15;
  }
  for (const e of ends) {
    svg.append(el("text", { x: e.x + 12, y: e.y + 4, class: `end-label halo l${e.k}` }, e.label));
  }
  host.append(svg);

  const legend = document.getElementById("load-legend");
  legend.replaceChildren(...LAYERS.map(([, label], k) => {
    const item = html("span", "legend-item");
    item.append(html("span", `swatch solid sw-l${k}`), document.createTextNode(label));
    return item;
  }));
}

/* ------------------------------------------------------------------ the audit chain */

function short(hash) {
  return hash ? `${hash.slice(0, 8)}...` : "";
}

function drawChain(head, anchors) {
  const host = document.getElementById("chain");
  host.replaceChildren();
  const newest = head ? head.seq : (anchors.length ? anchors[anchors.length - 1].seq : 0);
  if (!newest) return;
  const shown = Math.min(newest, 7);
  const first = newest - shown + 1;
  const W = 720, H = 150, box = 78, gap = (W - 40 - shown * box) / Math.max(1, shown - 1);
  const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, role: "img",
    "aria-label": `The last ${shown} records of the audit chain, newest record ${newest}` });
  const anchored = new Map(anchors.map((a) => [a.seq, a]));
  for (let i = 0; i < shown; i++) {
    const seq = first + i;
    const bx = 20 + i * (box + gap);
    if (i > 0) {
      svg.append(el("line", { x1: bx - gap, x2: bx, y1: 52, y2: 52, class: "link" }));
    }
    const isHead = head && seq === newest;
    svg.append(el("rect", { x: bx, y: 22, width: box, height: 60, rx: 8,
      class: isHead ? "block head" : "block" }));
    svg.append(el("text", { x: bx + box / 2, y: 46, "text-anchor": "middle", class: "block-seq" }, `#${seq}`));
    const a = anchored.get(seq);
    const hash = isHead ? head.head : (a ? a.head : "");
    svg.append(el("text", { x: bx + box / 2, y: 66, "text-anchor": "middle", class: "block-hash" },
      hash ? short(hash) : "fingerprint"));
    if (a) {
      svg.append(el("line", { x1: bx + box / 2, x2: bx + box / 2, y1: 82, y2: 104, class: "anchor-line" }));
      svg.append(el("text", { x: bx + box / 2, y: 120, "text-anchor": "middle", class: "anchor-label" },
        `anchored ${a.ts_utc.slice(0, 10)}`));
    }
    if (isHead) {
      svg.append(el("text", { x: bx + box / 2, y: 14, "text-anchor": "middle", class: "head-label" }, "live head"));
    }
  }
  host.append(svg);
}

function describeLive(head, anchors) {
  const live = document.getElementById("live");
  const last = anchors.length ? anchors[anchors.length - 1] : null;
  const anchorText = last
    ? ` The last fingerprint published in the repository is record ${last.seq}, on ${last.ts_utc.slice(0, 10)}.`
    : "";
  if (!head) {
    live.textContent = `The live record is not reachable from here, so the chain below is drawn from the anchors.${anchorText}`;
    return;
  }
  live.replaceChildren(
    html("strong", "", "Live now: "),
    document.createTextNode(
      `the audit chain on this server holds ${head.seq} records, and the newest fingerprint begins ${head.head.slice(0, 12)}.${anchorText}`
    )
  );
}

/* ------------------------------------------------------------------ start */

async function main() {
  let data;
  try {
    data = await getJson("data/site.json");
  } catch (e) {
    fail("The chart's data could not be loaded. Every number on this page is also in the repository's README.");
    return;
  }
  const pct = document.getElementById("pct");
  drawLoad(data.load, pct.value);
  pct.addEventListener("change", () => drawLoad(data.load, pct.value));

  let head = null;
  try {
    head = await getJson("/audit/head");
  } catch (e) {
    head = null;
  }
  describeLive(head, data.anchors || []);
  drawChain(head, data.anchors || []);
}

main();
