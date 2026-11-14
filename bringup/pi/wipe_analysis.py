"""Pure math for wipe runs: VISEN to amps, energy per stroke and wipe, stall current, torque margin.

No hardware imports, so it runs (and is tested) on the Mac. Scale facts are in ../VISEN_SCALE.md.
"""

import csv
import statistics

ISET_GAIN_A_PER_A = 100e-6  # MP6550 p. 11 / p. 4: 100 uA of ISET current per amp of output current
DRIVE_PHASES = ("fwd", "rev")
FIELDS = ["t_s", "wipe", "phase", "raw", "visen_v", "amps", "vbus_v", "vbus_fresh"]


def visen_to_amps(visen_v, r_iset=2000.0):
    return visen_v / (ISET_GAIN_A_PER_A * r_iset)


def amps_per_volt(r_iset=2000.0):
    return 1.0 / (ISET_GAIN_A_PER_A * r_iset)


class StallDetector:
    """Trips once current stays at or above `amps` for `ms` milliseconds."""

    def __init__(self, amps, ms):
        self.amps = amps
        self.window_s = ms / 1000.0
        self.since = None

    def update(self, t, amps):
        if amps >= self.amps:
            if self.since is None:
                self.since = t
            return t - self.since >= self.window_s - 1e-9  # float slack on the timestamps
        self.since = None
        return False


def _trapz(ts, ys):
    return sum((ts[k] - ts[k - 1]) * (ys[k] + ys[k - 1]) / 2.0 for k in range(1, len(ts)))


def _percentile(values, q):
    if not values:
        return None
    s = sorted(values)
    idx = min(len(s) - 1, max(0, round(q * (len(s) - 1))))
    return s[idx]


def stroke_stats(samples, vbus_nominal, inrush_s, stall_t=None):
    """Stats for one stroke's samples (all one wipe, one drive phase), ordered by time."""
    ts = [s["t_s"] for s in samples]
    amps = [s["amps"] for s in samples]
    volts = [s["vbus_v"] if s.get("vbus_v") is not None else vbus_nominal for s in samples]
    power = [v * a for v, a in zip(volts, amps)]
    t0 = ts[0] if ts else 0.0
    settled = [a for t, a in zip(ts, amps) if t - t0 >= inrush_s]
    out = {
        "n": len(samples),
        "duration_s": (ts[-1] - t0) if ts else 0.0,
        "energy_j": _trapz(ts, power),
        "mean_amps": statistics.fmean(amps) if amps else None,
        "peak_amps": max(amps) if amps else None,
        "p99_after_inrush_amps": _percentile(settled, 0.99),
        "median_after_inrush_amps": statistics.median(settled) if settled else None,
        "min_vbus_v": min(s["vbus_v"] for s in samples if s.get("vbus_v") is not None)
        if any(s.get("vbus_v") is not None for s in samples) else None,
        "stalled": stall_t is not None,
        "stall_amps": None,
    }
    if stall_t is not None:
        window = [a for t, a in zip(ts, amps) if t >= stall_t]
        out["stall_amps"] = statistics.median(window) if window else None
    return out


def torque_margin(stall_amps, peak_amps, noload_amps):
    """Torque ~ (I - I_noload) for a brushed DC motor, so margin = (I_stall - I_nl) / (I_peak - I_nl)."""
    if None in (stall_amps, peak_amps, noload_amps) or peak_amps <= noload_amps:
        return None
    return (stall_amps - noload_amps) / (peak_amps - noload_amps)


def _spread(values):
    values = [v for v in values if v is not None]
    if not values:
        return None
    return {"n": len(values), "median": statistics.median(values), "min": min(values), "max": max(values)}


def summarize(samples, strokes_meta, vbus_nominal, inrush_s, noload_amps=None, stall_ref_amps=None):
    """strokes_meta: list of {"wipe", "phase", "end", "stall_t"} in run order."""
    strokes = []
    for meta in strokes_meta:
        rows = [s for s in samples if s["wipe"] == meta["wipe"] and s["phase"] == meta["phase"]]
        st = stroke_stats(rows, vbus_nominal, inrush_s, meta.get("stall_t"))
        st.update({"wipe": meta["wipe"], "phase": meta["phase"], "end": meta["end"]})
        strokes.append(st)

    wipes = {}
    for st in strokes:
        w = wipes.setdefault(st["wipe"], {"wipe": st["wipe"], "energy_j": 0.0, "stalled": False})
        w["energy_j"] += st["energy_j"]
        w["stalled"] = w["stalled"] or st["stalled"]
    wipe_list = [wipes[k] for k in sorted(wipes)]
    complete = [w["energy_j"] for w in wipe_list if not w["stalled"]]

    stall_values = [st["stall_amps"] for st in strokes if st["stall_amps"] is not None]
    stall_median = statistics.median(stall_values) if stall_values else None
    stall_for_margin = stall_median if stall_median is not None else stall_ref_amps
    peaks = [st["p99_after_inrush_amps"] for st in strokes if not st["stalled"]]
    peak_median = statistics.median([p for p in peaks if p is not None]) if any(p is not None for p in peaks) else None

    vbus_measured = any(s.get("vbus_v") is not None for s in samples)
    clipped = sum(1 for s in samples if s.get("clipped"))
    return {
        "strokes": strokes,
        "wipes": wipe_list,
        "energy_per_wipe_j": _spread(complete),
        "energy_per_wipe_mwh": _spread([e / 3.6 for e in complete]),
        "stall_amps": _spread(stall_values),
        "peak_p99_amps": _spread(peaks),
        "noload_amps": noload_amps,
        "torque_margin": torque_margin(stall_for_margin, peak_median, noload_amps),
        "torque_margin_stall_source": "this run" if stall_median is not None
        else ("--stall-ref-amps" if stall_ref_amps is not None else None),
        "vbus": "measured on AIN1" if vbus_measured else f"assumed {vbus_nominal} V (no divider)",
        "clipped_samples": clipped,
    }


def write_csv(path, samples):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        for s in samples:
            w.writerow({k: ("" if s.get(k) is None else s.get(k)) for k in FIELDS})


def read_csv(path):
    out = []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            out.append({
                "t_s": float(row["t_s"]),
                "wipe": int(row["wipe"]),
                "phase": row["phase"],
                "raw": int(row["raw"]),
                "visen_v": float(row["visen_v"]),
                "amps": float(row["amps"]),
                "vbus_v": float(row["vbus_v"]) if row["vbus_v"] else None,
                "vbus_fresh": row["vbus_fresh"] == "1",
            })
    return out
