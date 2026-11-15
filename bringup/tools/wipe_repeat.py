#!/usr/bin/env python3
"""One run of the test card's stage 10: N wipe cycles by angle through the jog bench's HTTP API, from any machine on
the tailnet (or through the SSH tunnel). Restart `jog-bench` on the Pi before each run; this script doesn't.

    python3 bringup/tools/wipe_repeat.py --host <pi>:8765 --motor "<Pololu part #>" --wipes 30 --sweep 90 \\
        --out ~/NereusData/UrchinCam/<YYYY-MM-DD>_stage10_run1

Writes into --out: meta.json (deployed sha, motor, settings incl. the cut-off, pot calibration, learned leads, time,
host, the arguments), moves.json (one record per stroke), samples.csv (t_s, amps, state, angle), loop.json
(/api/loop after the run) and summary.json (the stage 10 numbers). Standard library only.
"""

import argparse
import csv
import datetime
import json
import os
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request


def summarize(moves, bar_deg=3.0, worst_deg=5.0):
    """Stage 10 numbers for one run (moves in order: out, back, out, back, ...)."""
    err = [abs(m["error_deg"]) for m in moves]
    wipes = [moves[i]["energy_j"] + moves[i + 1]["energy_j"] for i in range(0, len(moves) - 1, 2)]
    return {
        "strokes": len(moves),
        "within_bar": sum(e <= bar_deg for e in err),
        "over_worst": sum(e > worst_deg for e in err),
        "first_stroke_deg": moves[0]["error_deg"] if moves else None,
        "mean_abs_deg": round(statistics.fmean(err), 3) if err else None,
        "max_abs_deg": round(max(err), 3) if err else None,
        "energy_per_wipe_j": {"median": round(statistics.median(wipes), 4), "min": round(min(wipes), 4),
                              "max": round(max(wipes), 4)} if wipes else None,
    }


class Bench:
    def __init__(self, host):
        self.base = f"http://{host}"
        self.rows, self.status, self.stop = [], {}, threading.Event()

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=10) as r:
            return json.loads(r.read())

    def post(self, path, body=None):
        req = urllib.request.Request(self.base + path, data=json.dumps(body or {}).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            return {"error": json.loads(e.read()).get("error", str(e))}

    def listen(self):
        def run():
            with urllib.request.urlopen(self.base + "/stream", timeout=10) as f:
                for line in f:
                    if self.stop.is_set():
                        return
                    line = line.decode()
                    if line.startswith("data: "):
                        m = json.loads(line[6:])
                        self.status = m["status"]
                        self.rows.extend(zip(m["t"], m["a"], m["s"], m["g"]))
        threading.Thread(target=run, daemon=True).start()
        deadline = time.time() + 10
        while not self.status and time.time() < deadline:
            time.sleep(0.1)
        if not self.status:
            sys.exit("no status from the bench stream")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--host", required=True, help="host:port of the jog bench")
    p.add_argument("--motor", required=True, help="motor part number, recorded in meta.json")
    p.add_argument("--wipes", type=int, default=30)
    p.add_argument("--sweep", type=float, default=90.0)
    p.add_argument("--duty", type=float, default=1.0)
    p.add_argument("--pause-s", type=float, default=0.3)
    p.add_argument("--out", required=True)
    a = p.parse_args(argv)
    out = os.path.expanduser(a.out)
    if os.path.exists(out):
        sys.exit(f"{out} exists: a run never overwrites another (11/15 lost one that way)")
    os.makedirs(out)

    b = Bench(a.host)
    b.listen()
    st = b.status
    if st["state"] != "asleep" or st["move"]:
        sys.exit("the bench isn't idle")
    meta = {"started": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"), "host": a.host,
            "args": vars(a), "sha": st.get("sha"), "motor": a.motor, "settings": st.get("settings"),
            "pot_cal": st.get("pot_cal"), "lead_s": st.get("lead_s"), "leads": st.get("leads"),
            "start_angle": st.get("angle"), "start_pot_v": st.get("pot_v"), "throttled_before": st.get("throttled")}
    with open(os.path.join(out, "meta.json"), "w") as f:
        json.dump(meta, f, indent=1)

    n0 = len(b.get("/api/moves"))
    r = b.post("/move", {"kind": "wipe", "value": a.sweep, "cycles": a.wipes, "pause_s": a.pause_s,
                         "duty": a.duty, "method": "simple"})
    if r.get("error"):
        sys.exit(f"refused: {r['error']}")
    time.sleep(0.3)
    end = time.time() + 20 + a.wipes * 6
    while time.time() < end:
        b.post("/hb")
        time.sleep(0.2)
        if b.status["state"] == "asleep" and not b.status["move"]:
            break
    time.sleep(0.5)
    b.stop.set()
    moves = b.get("/api/moves")[n0:]
    with open(os.path.join(out, "moves.json"), "w") as f:
        json.dump(moves, f, indent=1)
    with open(os.path.join(out, "samples.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["t_s", "amps", "state", "angle"])
        w.writerows(b.rows)
    with open(os.path.join(out, "loop.json"), "w") as f:
        json.dump(b.get("/api/loop"), f, indent=1)
    summary = summarize(moves)
    summary["events"] = [e["text"] for e in b.status.get("events", [])]
    summary["throttled_after"] = b.status.get("throttled")
    with open(os.path.join(out, "summary.json"), "w") as f:
        json.dump(summary, f, indent=1)
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
