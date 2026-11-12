#!/usr/bin/env python3
"""Make plot.html for one wipe-logger run folder (samples.csv + summary.json), self-contained, no internet needed.

    python3 plot_run.py ~/NereusData/UrchinCam/2026-11-13_seal
"""

import html
import json
import os
import sys

import wipe_analysis as wa

MAX_POINTS = 6000  # min/max-per-bucket downsampling keeps peaks visible


def downsample(samples, max_points=MAX_POINTS):
    if len(samples) <= max_points:
        return samples
    bucket = len(samples) / (max_points / 2)
    out, i = [], 0.0
    while int(i) < len(samples):
        chunk = samples[int(i):int(i + bucket)] or samples[int(i):int(i) + 1]
        lo = min(chunk, key=lambda s: s["amps"])
        hi = max(chunk, key=lambda s: s["amps"])
        out.extend(sorted({id(lo): lo, id(hi): hi}.values(), key=lambda s: s["t_s"]))
        i += bucket
    return out


def phase_bands(samples):
    """Contiguous (start, end, phase, wipe) runs of fwd/rev samples."""
    bands = []
    for s in samples:
        if s["phase"] not in wa.DRIVE_PHASES:
            continue
        if bands and bands[-1]["phase"] == s["phase"] and bands[-1]["wipe"] == s["wipe"]:
            bands[-1]["end"] = s["t_s"]
        else:
            bands.append({"start": s["t_s"], "end": s["t_s"], "phase": s["phase"], "wipe": s["wipe"]})
    return bands


def build_html(run_name, samples, summary):
    pts = downsample(samples)
    data = {
        "run": run_name,
        "t": [round(s["t_s"], 4) for s in pts],
        "a": [round(s["amps"] * 1000, 2) for s in pts],  # mA
        "v": [None if s["vbus_v"] is None else round(s["vbus_v"], 3) for s in pts],
        "bands": phase_bands(samples),
        "stalls": [st for st in summary.get("strokes", []) if st.get("stalled")],
        "wipes": summary.get("wipes", []),
        "strokes": summary.get("strokes", []),
        "summary": {k: summary.get(k) for k in ("energy_per_wipe_j", "energy_per_wipe_mwh", "stall_amps",
                                                "peak_p99_amps", "torque_margin", "noload_amps", "vbus",
                                                "clipped_samples", "achieved_rate_sps", "n_samples")},
        "args": summary.get("args", {}),
    }
    return TEMPLATE.replace("__TITLE__", html.escape(f"Wipe run {run_name}")).replace("__DATA__", json.dumps(data).replace("</", "<\\/"))


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    if len(argv) != 1:
        sys.exit(__doc__)
    run_dir = os.path.expanduser(argv[0])
    samples = wa.read_csv(os.path.join(run_dir, "samples.csv"))
    with open(os.path.join(run_dir, "summary.json")) as f:
        summary = json.load(f)
    out = os.path.join(run_dir, "plot.html")
    with open(out, "w") as f:
        f.write(build_html(os.path.basename(run_dir.rstrip("/")), samples, summary))
    print(out)


TEMPLATE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
:root { color-scheme: light; --surface: #fcfcfb; --ink: #0b0b0b; --ink-2: #52514e; --ink-3: #8a8983;
  --grid: #e6e5e0; --band-fwd: #eeedea; --band-rev: #e2e1dc; --s1: #2a78d6; --s2: #eb6834; --crit: #e34948; }
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) { color-scheme: dark; --surface: #1a1a19;
  --ink: #f0efec; --ink-2: #c3c2b7; --ink-3: #8f8e86; --grid: #2e2e2c; --band-fwd: #242423; --band-rev: #2d2d2b;
  --s1: #3987e5; --s2: #d95926; --crit: #e66767; } }
:root[data-theme="dark"] { color-scheme: dark; --surface: #1a1a19; --ink: #f0efec; --ink-2: #c3c2b7;
  --ink-3: #8f8e86; --grid: #2e2e2c; --band-fwd: #242423; --band-rev: #2d2d2b; --s1: #3987e5; --s2: #d95926;
  --crit: #e66767; }
body { margin: 0; background: var(--surface); color: var(--ink); font: 14px/1.4 system-ui, sans-serif; }
main { max-width: 1100px; margin: 0 auto; padding: 20px 16px 40px; }
h1 { font-size: 20px; margin: 0 0 4px; } h2 { font-size: 15px; margin: 28px 0 8px; }
.sub { color: var(--ink-2); margin: 0 0 16px; }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; }
.tile { border: 1px solid var(--grid); border-radius: 8px; padding: 10px 12px; }
.tile .k { color: var(--ink-2); font-size: 12px; } .tile .v { font-size: 22px; font-variant-numeric: tabular-nums; }
.tile .n { color: var(--ink-3); font-size: 12px; }
.chart { position: relative; width: 100%; }
svg { display: block; width: 100%; height: auto; overflow: visible; }
.axis text { fill: var(--ink-2); font-size: 11px; } .grid line { stroke: var(--grid); }
.tip { position: absolute; pointer-events: none; background: var(--surface); border: 1px solid var(--grid);
  border-radius: 6px; padding: 6px 8px; font-size: 12px; color: var(--ink); display: none; white-space: nowrap;
  box-shadow: 0 2px 8px rgba(0,0,0,.12); }
.key { color: var(--ink-2); font-size: 12px; display: flex; gap: 14px; flex-wrap: wrap; margin: 4px 0; }
.sw { display: inline-block; width: 12px; height: 12px; border-radius: 3px; vertical-align: -2px; margin-right: 4px; }
table { border-collapse: collapse; font-variant-numeric: tabular-nums; width: 100%; }
th, td { text-align: right; padding: 4px 8px; border-bottom: 1px solid var(--grid); }
th:first-child, td:first-child { text-align: left; } th { color: var(--ink-2); font-weight: 500; }
details { margin-top: 16px; color: var(--ink-2); } pre { white-space: pre-wrap; font-size: 12px; }
</style></head>
<body><main>
<h1 id="title"></h1><p class="sub" id="sub"></p>
<div class="tiles" id="tiles"></div>
<h2>Motor current</h2>
<div class="key"><span><span class="sw" style="background:var(--band-fwd)"></span>forward stroke</span>
<span><span class="sw" style="background:var(--band-rev)"></span>reverse stroke</span>
<span><span class="sw" style="background:var(--crit)"></span>stall cut-off</span></div>
<div class="chart" id="cur"></div>
<div id="vwrap" hidden><h2>+5V rail (AIN1 divider)</h2><div class="chart" id="vbus"></div></div>
<h2>Energy per wipe</h2><div class="chart" id="energy"></div>
<h2>Strokes</h2><div style="overflow-x:auto"><table id="strokes"></table></div>
<details><summary>Run arguments</summary><pre id="args"></pre></details>
</main>
<script>
const D = __DATA__;
const NS = "http://www.w3.org/2000/svg";
const $ = (id) => document.getElementById(id);
const el = (tag, attrs, parent) => { const e = document.createElementNS(NS, tag);
  for (const k in attrs) e.setAttribute(k, attrs[k]); if (parent) parent.appendChild(e); return e; };
const fmt = (x, d) => x == null ? "–" : Number(x).toFixed(d);

$("title").textContent = "Wipe run " + D.run;
const S = D.summary;
$("sub").textContent = `${S.n_samples ?? D.t.length} samples at ${fmt(S.achieved_rate_sps, 0)} S/s · +5V ${S.vbus}` +
  (S.clipped_samples ? ` · ${S.clipped_samples} clipped samples` : "") + ` · motor ${D.args.motor ?? "?"}`;

function tile(k, v, n) { const t = document.createElement("div"); t.className = "tile";
  t.innerHTML = `<div class="k">${k}</div><div class="v">${v}</div><div class="n">${n}</div>`; $("tiles").appendChild(t); }
const E = S.energy_per_wipe_j, Ew = S.energy_per_wipe_mwh, St = S.stall_amps, Pk = S.peak_p99_amps;
tile("Energy per wipe", E ? fmt(E.median, 3) + " J" : "–", E ? `${fmt(Ew.median, 4)} mWh · ${fmt(E.min, 3)}–${fmt(E.max, 3)} J · n=${E.n}` : "no complete wipe");
tile("Peak current (p99)", Pk ? fmt(Pk.median * 1000, 0) + " mA" : "–", Pk ? `${fmt(Pk.min * 1000, 0)}–${fmt(Pk.max * 1000, 0)} mA · n=${Pk.n}` : "");
tile("Stall current", St ? fmt(St.median * 1000, 0) + " mA" : "–", St ? `n=${St.n}` : "no stall this run");
tile("Torque margin (est.)", S.torque_margin != null ? fmt(S.torque_margin, 2) + "×" : "–",
  S.noload_amps != null ? `no-load ${fmt(S.noload_amps * 1000, 0)} mA` : "needs --noload-amps");

function lineChart(host, xs, ys, opt) {
  const W = Math.max(320, Math.round(host.clientWidth) || 1000), H = opt.h || (W < 600 ? 220 : 280), m = { l: 48, r: 12, t: 24, b: 28 };
  const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": opt.label }, host);
  const vals = ys.filter((y) => y != null);
  const x0 = xs[0] ?? 0, x1 = xs[xs.length - 1] ?? 1;
  let y0 = opt.y0 ?? Math.min(...vals), y1 = Math.max(...vals, opt.yMinTop ?? -Infinity);
  if (y1 === y0) y1 = y0 + 1; y1 += (y1 - y0) * 0.08;
  const X = (x) => m.l + (x - x0) / ((x1 - x0) || 1) * (W - m.l - m.r);
  const Y = (y) => H - m.b - (y - y0) / (y1 - y0) * (H - m.t - m.b);
  for (const b of opt.bands || []) el("rect", { x: X(b.start), y: m.t, width: Math.max(1, X(b.end) - X(b.start)),
    height: H - m.t - m.b, fill: b.phase === "fwd" ? "var(--band-fwd)" : "var(--band-rev)" }, svg);
  const g = el("g", { class: "grid axis" }, svg);
  for (let i = 0; i <= 4; i++) { const v = y0 + (y1 - y0) * i / 4;
    el("line", { x1: m.l, x2: W - m.r, y1: Y(v), y2: Y(v) }, g);
    el("text", { x: m.l - 6, y: Y(v) + 4, "text-anchor": "end" }, g).textContent = fmt(v, opt.yDec ?? 0); }
  const nx = W < 600 ? 3 : 6;
  for (let i = 0; i <= nx; i++) { const v = x0 + (x1 - x0) * i / nx;
    el("text", { x: X(v), y: H - 8, "text-anchor": "middle" }, g).textContent = fmt(v, 1) + " s"; }
  el("text", { x: 4, y: 12, "text-anchor": "start" }, g).textContent = opt.unit;
  if (opt.hline != null) { el("line", { x1: m.l, x2: W - m.r, y1: Y(opt.hline), y2: Y(opt.hline),
    stroke: "var(--ink-3)", "stroke-dasharray": "4 4" }, svg);
    el("text", { x: W - m.r, y: Y(opt.hline) - 4, "text-anchor": "end", fill: "var(--ink-2)", "font-size": 11 }, svg).textContent = opt.hlineLabel; }
  for (const s of opt.marks || []) { el("line", { x1: X(s), x2: X(s), y1: m.t, y2: H - m.b, stroke: "var(--crit)",
    "stroke-width": 2, "stroke-dasharray": "3 3" }, svg); }
  let d = ""; let pen = false;
  xs.forEach((x, i) => { if (ys[i] == null) { pen = false; return; } d += (pen ? "L" : "M") + X(x).toFixed(1) + " " + Y(ys[i]).toFixed(1); pen = true; });
  el("path", { d, fill: "none", stroke: opt.color, "stroke-width": 2, "stroke-linejoin": "round" }, svg);
  const cross = el("line", { y1: m.t, y2: H - m.b, stroke: "var(--ink-3)", visibility: "hidden" }, svg);
  const dot = el("circle", { r: 4, fill: opt.color, stroke: "var(--surface)", "stroke-width": 2, visibility: "hidden" }, svg);
  const tip = document.createElement("div"); tip.className = "tip"; host.appendChild(tip);
  const hit = el("rect", { x: m.l, y: m.t, width: W - m.l - m.r, height: H - m.t - m.b, fill: "transparent" }, svg);
  hit.addEventListener("pointermove", (ev) => {
    const r = svg.getBoundingClientRect(); const px = (ev.clientX - r.left) / r.width * W;
    const tx = x0 + (px - m.l) / (W - m.l - m.r) * (x1 - x0);
    let lo = 0, hi = xs.length - 1; while (hi - lo > 1) { const mid = (lo + hi) >> 1; xs[mid] < tx ? lo = mid : hi = mid; }
    const i = Math.abs(xs[lo] - tx) < Math.abs(xs[hi] - tx) ? lo : hi; if (ys[i] == null) return;
    cross.setAttribute("x1", X(xs[i])); cross.setAttribute("x2", X(xs[i])); cross.setAttribute("visibility", "visible");
    dot.setAttribute("cx", X(xs[i])); dot.setAttribute("cy", Y(ys[i])); dot.setAttribute("visibility", "visible");
    const band = (opt.bands || []).find((b) => xs[i] >= b.start && xs[i] <= b.end);
    tip.innerHTML = `${fmt(xs[i], 3)} s<br><b>${fmt(ys[i], opt.tipDec ?? 1)} ${opt.unit}</b>` + (band ? `<br>wipe ${band.wipe} ${band.phase}` : "");
    tip.style.display = "block";
    const left = X(xs[i]) / W * r.width; tip.style.left = Math.min(left + 12, r.width - tip.offsetWidth) + "px";
    tip.style.top = (Y(ys[i]) / H * r.height - 40) + "px";
  });
  hit.addEventListener("pointerleave", () => { tip.style.display = "none"; cross.setAttribute("visibility", "hidden"); dot.setAttribute("visibility", "hidden"); });
}

const stallMa = (D.args.stall_amps ?? 0) * 1000;
lineChart($("cur"), D.t, D.a, { label: "Motor current over time", unit: "mA", color: "var(--s1)", y0: 0,
  yMinTop: stallMa, bands: D.bands, hline: stallMa || null, hlineLabel: `stall cut-off ${fmt(stallMa, 0)} mA`,
  marks: D.stalls.map((s) => D.bands.find((b) => b.wipe === s.wipe && b.phase === s.phase)?.end).filter((x) => x != null) });

if (D.v.some((v) => v != null)) { $("vwrap").hidden = false;
  lineChart($("vbus"), D.t, D.v, { label: "+5V rail over time", unit: "V", color: "var(--s2)", yDec: 2, tipDec: 3,
    bands: D.bands, hline: 4.75, hlineLabel: "4.75 V bar" }); }

(function energyBars() {
  const host = $("energy"), W = Math.max(320, Math.round(host.clientWidth) || 1000), H = 200, m = { l: 48, r: 12, t: 24, b: 28 };
  const ws = D.wipes; if (!ws.length) { host.textContent = "No wipes."; return; }
  const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Energy per wipe" }, host);
  const ymax = Math.max(...ws.map((w) => w.energy_j)) * 1.1 || 1;
  const Y = (y) => H - m.b - y / ymax * (H - m.t - m.b);
  const g = el("g", { class: "grid axis" }, svg);
  for (let i = 0; i <= 4; i++) { const v = ymax * i / 4; el("line", { x1: m.l, x2: W - m.r, y1: Y(v), y2: Y(v) }, g);
    el("text", { x: m.l - 6, y: Y(v) + 4, "text-anchor": "end" }, g).textContent = fmt(v, 2); }
  el("text", { x: 4, y: 12 }, g).textContent = "J";
  const bw = (W - m.l - m.r) / ws.length;
  const tip = document.createElement("div"); tip.className = "tip"; host.appendChild(tip);
  ws.forEach((w, i) => { const x = m.l + i * bw + 1, wd = Math.max(2, Math.min(bw - 2, 48)), cx = x + (bw - 2 - wd) / 2;
    const h = H - m.b - Y(w.energy_j);
    el("path", { d: `M${cx} ${H - m.b} V${Y(w.energy_j) + 4} q0 -4 4 -4 h${wd - 8} q4 0 4 4 V${H - m.b} Z`,
      fill: w.stalled ? "var(--crit)" : "var(--s1)" }, svg);
    el("text", { x: cx + wd / 2, y: H - 8, "text-anchor": "middle" }, g).textContent = w.wipe + (w.stalled ? " stall" : "");
    const hit = el("rect", { x: m.l + i * bw, y: m.t, width: bw, height: H - m.t - m.b, fill: "transparent" }, svg);
    hit.addEventListener("pointermove", (ev) => { const r = svg.getBoundingClientRect();
      tip.innerHTML = `wipe ${w.wipe}<br><b>${fmt(w.energy_j, 3)} J</b>${w.stalled ? "<br>stalled" : ""}`; tip.style.display = "block";
      tip.style.left = Math.min(ev.clientX - r.left + 12, r.width - tip.offsetWidth) + "px"; tip.style.top = (ev.clientY - r.top - 40) + "px"; });
    hit.addEventListener("pointerleave", () => tip.style.display = "none"); });
})();

const rows = [["Wipe", "Stroke", "End", "s", "Mean mA", "p99 mA", "Stall mA", "Energy J", "Min +5V"]];
for (const s of D.strokes) rows.push([s.wipe, s.phase, s.end, fmt(s.duration_s, 2), fmt(s.mean_amps * 1000, 1),
  fmt(s.p99_after_inrush_amps == null ? null : s.p99_after_inrush_amps * 1000, 1), fmt(s.stall_amps == null ? null : s.stall_amps * 1000, 1),
  fmt(s.energy_j, 3), fmt(s.min_vbus_v, 3)]);
$("strokes").innerHTML = rows.map((r, i) => "<tr>" + r.map((c) => i ? `<td>${c}</td>` : `<th>${c}</th>`).join("") + "</tr>").join("");
$("args").textContent = JSON.stringify(D.args, null, 2);
</script></body></html>
"""

if __name__ == "__main__":
    main()
