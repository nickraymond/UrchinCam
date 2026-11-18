"""Bench runs for the jog bench: tagged multi-cycle wipe runs, their logs, and statistics across runs.

One run = N wipe cycles (forward for the stroke time, brake, reverse for the same time) with a motor and a
wipe condition picked from bench_options.json. Each run gets its own folder under RUNS_ROOT:

    meta.json      what was run (motor, condition, stroke, cycles, settings, VISEN zeros, source sha, status)
    samples.csv    every sample: t_s, cycle, state, amps, raw, clipped, pot_v (blank between pot reads), angle_deg
    summary.json   per-stroke stats (written when the run ends) and the run's result

Standard library only. Nothing here touches hardware: the control thread hands rows to RunLogger.push().
"""

import csv
import datetime
import json
import os
import queue
import re
import shutil
import statistics
import threading

import wipe_analysis as wa

HERE = os.path.dirname(os.path.abspath(__file__))
RUNS_ROOT = os.path.expanduser("~/NereusData/UrchinCam/bench_runs")
OPTIONS_PATH = os.path.join(HERE, "bench_options.json")
INRUSH_S = 0.05  # start of a stroke left out of the settled stats, as in wipe_logger
VBUS_NOMINAL = 5.0  # no +5V divider fitted: energy assumes 5.0 V (VISEN_SCALE.md)
RUN_ID_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{6}Z_[A-Za-z0-9_.-]+$")
SAMPLE_FIELDS = ["t_s", "cycle", "state", "amps", "raw", "clipped", "pot_v", "angle_deg"]
METRICS = [  # (key in a stroke record, label)
    ("median_ma", "Median current (mA)"),
    ("mean_ma", "Mean current (mA)"),
    ("p99_ma", "p99 current (mA)"),
    ("peak_ma", "Peak incl. inrush (mA)"),
    ("energy_j", "Energy (J)"),
    ("sweep_deg", "Sweep (deg)"),
    ("speed_dps", "Mean speed (deg/s)"),
]


def load_options(path=OPTIONS_PATH):
    with open(path) as f:
        opts = json.load(f)
    for key in ("motors", "conditions"):
        if not opts.get(key) or not all(isinstance(x, str) and x.strip() for x in opts[key]):
            raise ValueError(f"{path}: '{key}' must be a list of names")
    return opts


def slug(text, n=24):
    s = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    return s[:n].strip("-") or "x"


def make_run_id(now_utc, motor, condition):
    return f"{now_utc:%Y-%m-%dT%H%M%S}Z_{slug(motor)}_{slug(condition)}"


def _write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2)
    os.replace(tmp, path)  # readers never see a half-written file


def _ma(a):
    return None if a is None else round(a * 1000, 2)


class RunLogger:
    """Writes one run. push() is cheap (a queue put) so the control thread never waits on the SD card; a worker
    thread writes samples.csv and closes each stroke's stats as it ends. finish() writes summary.json."""

    def __init__(self, meta, root=RUNS_ROOT, threaded=True, now_utc=None):
        now_utc = now_utc or datetime.datetime.now(datetime.timezone.utc)
        self.meta = dict(meta)
        self.meta.setdefault("mode", "time")  # "angle" comes with the pot
        self.meta.update({"id": make_run_id(now_utc, meta["motor"], meta["condition"]),
                          "started": now_utc.isoformat(timespec="seconds"), "status": "running",
                          "excluded": False})
        base, k = self.meta["id"], 1
        while os.path.exists(os.path.join(root, self.meta["id"])):  # two runs in the same second
            k += 1
            self.meta["id"] = f"{base}_{k}"
        self.dir = os.path.join(root, self.meta["id"])
        os.makedirs(self.dir)
        _write_json(os.path.join(self.dir, "meta.json"), self.meta)
        self.f = open(os.path.join(self.dir, "samples.csv"), "w", newline="")
        self.w = csv.writer(self.f)
        self.w.writerow(SAMPLE_FIELDS)
        self.strokes = []
        self.cur = None  # (cycle, dir, rows) of the stroke being driven
        self.clipped = 0
        self.q = queue.Queue()
        self.done = threading.Event()
        self.thread = None
        if threaded:
            self.thread = threading.Thread(target=self._work, daemon=True)
            self.thread.start()

    # --- control-thread side ---
    def push(self, row):
        self.q.put(row)

    def finish(self, result):
        self.q.put(("__finish__", result))
        if self.thread is None:
            self._drain()

    def wait(self, timeout=10):
        if self.thread is not None:
            self.thread.join(timeout)
        return self.done.is_set()

    # --- worker side ---
    def _work(self):
        while not self.done.is_set():
            self._handle(self.q.get())

    def _drain(self):
        while not self.done.is_set():
            self._handle(self.q.get_nowait())

    def _handle(self, item):
        if isinstance(item, tuple) and item and item[0] == "__finish__":
            self._finalize(item[1])
            return
        row = item
        pot_v, angle = row.get("pot_v"), row.get("angle")
        self.w.writerow([f"{row['t_s']:.5f}", row.get("cycle") or "", row["state"], f"{row['amps']:.5f}",
                         row["raw"], int(row["clipped"]), "" if pot_v is None else f"{pot_v:.5f}",
                         "" if angle is None else f"{angle:.2f}"])
        self.clipped += int(row["clipped"])
        state = row["state"]
        if state in ("fwd", "rev"):
            key = (row.get("cycle"), state)
            if self.cur is None or self.cur[:2] != key:
                self._close_stroke()
                self.cur = (key[0], key[1], [])
            self.cur[2].append({"t_s": row["t_s"], "amps": row["amps"], "clipped": row["clipped"],
                                "angle": row.get("angle")})
        else:
            self._close_stroke()

    def _close_stroke(self):
        if self.cur is None:
            return
        cycle, direction, rows = self.cur
        self.cur = None
        st = wa.stroke_stats(rows, VBUS_NOMINAL, INRUSH_S)
        # the last sample lands at most one sample period short of the stroke time
        complete = st["duration_s"] >= self.meta["stroke_s"] - 0.02
        angles = [r["angle"] for r in rows if r["angle"] is not None]
        sweep = angles[-1] - angles[0] if len(angles) >= 2 else None
        self.strokes.append({
            "cycle": cycle, "dir": direction, "complete": complete, "n": st["n"],
            "duration_s": round(st["duration_s"], 4),
            "mean_ma": _ma(st["mean_amps"]), "median_ma": _ma(st["median_after_inrush_amps"]),
            "p99_ma": _ma(st["p99_after_inrush_amps"]), "peak_ma": _ma(st["peak_amps"]),
            "energy_j": round(st["energy_j"], 4), "clipped": sum(1 for r in rows if r["clipped"]),
            "sweep_deg": None if sweep is None else round(sweep, 2),
            "speed_dps": None if sweep is None or not st["duration_s"] else round(abs(sweep) / st["duration_s"], 2),
        })

    def _finalize(self, result):
        self._close_stroke()
        self.f.close()
        result = dict(result or {})
        status = "complete" if result.get("complete") else "stopped"
        cycles = []
        for c in sorted({s["cycle"] for s in self.strokes if s["cycle"] is not None}):
            pair = {s["dir"]: s for s in self.strokes if s["cycle"] == c}
            if all(d in pair and pair[d]["complete"] for d in ("fwd", "rev")):
                cycles.append({"cycle": c, "energy_j": round(pair["fwd"]["energy_j"] + pair["rev"]["energy_j"], 4)})
        summary = {"result": {**result, "status": status}, "strokes": self.strokes, "cycles": cycles,
                   "clipped": self.clipped, "headline": headline(self.strokes, cycles)}
        _write_json(os.path.join(self.dir, "summary.json"), summary)
        self.meta["status"] = status
        self.meta["ended_reason"] = result.get("reason")
        self.meta["cycles_done"] = len(cycles)
        _write_json(os.path.join(self.dir, "meta.json"), self.meta)
        self.done.set()

    def abort(self):
        """Start was refused: remove the empty folder."""
        self.done.set()
        self.f.close()
        shutil.rmtree(self.dir, ignore_errors=True)


def _spread(values):
    values = [v for v in values if v is not None]
    if not values:
        return None
    return {"n": len(values), "median": round(statistics.median(values), 4),
            "mean": round(statistics.fmean(values), 4), "max": round(max(values), 4), "min": round(min(values), 4)}


def headline(strokes, cycles):
    out = {}
    for d in ("fwd", "rev"):
        ok = [s for s in strokes if s["dir"] == d and s["complete"]]
        out[d] = {k: _spread([s[k] for s in ok]) for k, _ in METRICS}
    out["cycle_energy_j"] = _spread([c["energy_j"] for c in cycles])
    return out


# --- reading runs back ---
def _read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def run_dir(root, run_id):
    if not RUN_ID_RE.match(run_id or ""):
        raise ValueError("bad run id")
    d = os.path.join(root, run_id)
    if not os.path.isfile(os.path.join(d, "meta.json")):
        raise KeyError(run_id)
    return d


def list_runs(root=RUNS_ROOT):
    """Newest first: each run's meta plus its summary headline (None while running)."""
    if not os.path.isdir(root):
        return []
    out = []
    for name in sorted(os.listdir(root), reverse=True):
        if not RUN_ID_RE.match(name):
            continue
        meta = _read_json(os.path.join(root, name, "meta.json"))
        if not meta:
            continue
        summ = _read_json(os.path.join(root, name, "summary.json"))
        out.append({**meta, "headline": summ["headline"] if summ else None,
                    "result": summ["result"] if summ else None})
    return out


def load_run(root, run_id):
    d = run_dir(root, run_id)
    return {"meta": _read_json(os.path.join(d, "meta.json")), "summary": _read_json(os.path.join(d, "summary.json"))}


def set_excluded(root, run_id, excluded):
    d = run_dir(root, run_id)
    path = os.path.join(d, "meta.json")
    meta = _read_json(path)
    meta["excluded"] = bool(excluded)
    _write_json(path, meta)
    return meta


def group_stats(root=RUNS_ROOT):
    """Statistics across runs, grouped by motor, condition, mode and stroke; one row per direction plus one for
    whole-cycle energy. Only complete strokes of finished, included runs count."""
    groups = {}
    for meta in list_runs(root):
        if meta.get("excluded") or meta.get("status") == "running":
            continue
        summ = _read_json(os.path.join(root, meta["id"], "summary.json"))
        if not summ:
            continue
        key = (meta["motor"], meta["condition"], meta.get("mode", "time"), meta["stroke_s"])
        g = groups.setdefault(key, {"runs": 0, "strokes": {"fwd": [], "rev": []}, "cycles": [], "stopped_runs": 0})
        g["runs"] += 1
        g["stopped_runs"] += meta.get("status") != "complete"
        for s in summ["strokes"]:
            if s["complete"] and s["dir"] in g["strokes"]:
                g["strokes"][s["dir"]].append(s)
        g["cycles"].extend(summ.get("cycles", []))
    rows = []
    for (motor, condition, mode, stroke_s), g in sorted(groups.items()):
        base = {"motor": motor, "condition": condition, "mode": mode, "stroke_s": stroke_s, "runs": g["runs"],
                "stopped_runs": g["stopped_runs"]}
        for d in ("fwd", "rev"):
            ss = g["strokes"][d]
            rows.append({**base, "dir": d, "n": len(ss), **{k: _spread([s[k] for s in ss]) for k, _ in METRICS}})
        rows.append({**base, "dir": "cycle", "n": len(g["cycles"]),
                     "energy_j": _spread([c["energy_j"] for c in g["cycles"]])})
    return rows


def trace(root, run_id, max_points=4000):
    """The run's current for plotting: min and max of each time bucket, so short spikes survive."""
    d = run_dir(root, run_id)
    with open(os.path.join(d, "samples.csv"), newline="") as f:
        rows = [(float(r["t_s"]), float(r["amps"]), r["state"][0],
                 float(r["angle_deg"]) if r.get("angle_deg") else None) for r in csv.DictReader(f)]
    buckets = max(1, max_points // 2)
    k = max(1, -(-len(rows) // buckets))
    t, a, s, g = [], [], [], []
    for i in range(0, len(rows), k):
        chunk = rows[i:i + k]
        lo = min(chunk, key=lambda r: r[1])
        hi = max(chunk, key=lambda r: r[1])
        for r in sorted({lo, hi}, key=lambda r: r[0]):
            t.append(round(r[0], 4))
            a.append(round(r[1], 5))
            s.append(r[2])
            g.append(None if r[3] is None else round(r[3], 1))
    return {"t": t, "a": a, "s": s, "g": g}
