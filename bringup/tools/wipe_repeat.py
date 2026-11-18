#!/usr/bin/env python3
"""One run of the test card's stage 10: N wipe cycles by angle through the jog bench's HTTP API, from any machine on
the tailnet (or through the SSH tunnel). Restart `jog-bench` on the Pi before each run; this script doesn't.

    python3 bringup/tools/wipe_repeat.py --host <pi>:8765 --motor "<Pololu part #>" --expect-sha <sha> \\
        --wipes 30 --sweep 90 --out ~/NereusData/UrchinCam/<YYYY-MM-DD>_stage10_run1

It refuses, before writing anything, a bench that isn't idle, runs another sha, or has its pot not trusted (every
restart leaves the pot latched until the operator presses "The arm is on the track" on the Jog page; meta.json records
the pot state and the bench events from before the run, which hold that confirm).
Before the wipes it sends the arm to --start-deg (`go to`, which may correct up to 3 times at <= 75 %) and refuses to
run unless it stopped within START_TOL_DEG of it; the wipes are relative to where it stopped. After them it sends the
arm back there and asks for Nick's by-eye reading against the marked zero (blank or no terminal: not read).

Writes into --out: meta.json (deployed sha, motor, settings incl. the cut-off, pot calibration, learned leads, start
move, time, host, the arguments), moves.json (one record per stroke), events.json (every bench event seen during the
run, not just the last 20), samples.csv (t_s, amps, state, angle), loop.json (/api/loop after the run) and
summary.json (the stage 10 numbers, `complete`, `stops`, `cutoff_ok`, `conditions_ok`, `untrained_at_start`,
`throttled_before` / `_after`, `end_angle`, `zero_check_deg`). The record is written before the return move and again
after it. Ctrl-C at any point stops the motor and still writes what it has, marked "aborted"; a run that fails (a
refused or unfinished move, the pot latching, the link or stream dropping) stops the motor, writes every file it can
with the reason in `failed`, and exits non-zero. Standard library only.
"""

import argparse
import string
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

MAX_WIPES = 100  # the bench's own cap (jog_server.MAX_CYCLES); more is refused here, not cut silently
START_TOL_DEG = 3.0  # the arm must stop this close to --start-deg before the wipes start
CUTOFF = {"stall_amps": 0.5, "stall_ms": 250.0}  # stage 10 runs at the default cut-off; anything else doesn't count
UNTRAINED_LEAD_S = 0.04  # jog_server.LEAD_S: the simple method's brake lead before anything is learned
REGISTERED = {"sweep": 90.0, "duty": 1.0, "pause_s": 0.3, "start_deg": 0.0}  # the test card's run conditions


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


def stops(events):
    """Events that are not a normal end of a stroke: any safety stop, a cancelled move, the watchdog."""
    out = []
    for e in events:
        text = e["text"]
        normal = ("stopped:" in text and ": at target (peak" in text) or (": at " in text and "(target" in text)
        if not normal:
            out.append(e)
    return out


class Bench:
    def __init__(self, host):
        self.base = f"http://{host}"
        self.rows, self.status, self.stop = [], {}, threading.Event()
        self.events, self._seen = [], set()

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

    def take(self, m):
        """One stream message: keep the status, the rows and every event not seen before."""
        self.status = m["status"]
        self.rows.extend(zip(m["t"], m["a"], m["s"], m["g"]))
        for e in m["status"].get("events", []):
            key = (e["t"], e["text"])
            if key not in self._seen:
                self._seen.add(key)
                self.events.append(e)

    def listen(self):
        def run():
            with urllib.request.urlopen(self.base + "/stream", timeout=10) as f:
                for line in f:
                    if self.stop.is_set():
                        return
                    line = line.decode()
                    if line.startswith("data: "):
                        self.take(json.loads(line[6:]))
        threading.Thread(target=run, daemon=True).start()
        deadline = time.time() + 10
        while not self.status and time.time() < deadline:
            time.sleep(0.1)
        if not self.status:
            sys.exit("no status from the bench stream")


def idle(st):
    return st["state"] == "asleep" and not st["move"]


def wait_idle(b, seconds, sleep=time.sleep, clock=time.time):
    """Heartbeat the bench until it's idle again (or `seconds` pass); True if it went idle."""
    end = clock() + seconds
    while clock() < end:
        b.post("/hb")
        sleep(0.2)
        if idle(b.status):
            return True
    return False


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--host", required=True, help="host:port of the jog bench")
    p.add_argument("--motor", required=True, help="motor part number, recorded in meta.json")
    p.add_argument("--expect-sha", required=True,
                   help="the code sha with QE code PASS (7+ characters); refuses if the bench runs anything else")
    p.add_argument("--wipes", type=int, default=30)
    p.add_argument("--sweep", type=float, default=REGISTERED["sweep"])
    p.add_argument("--duty", type=float, default=REGISTERED["duty"])
    p.add_argument("--pause-s", type=float, default=REGISTERED["pause_s"])
    p.add_argument("--start-deg", type=float, default=REGISTERED["start_deg"],
                   help="go here before the wipes, and back here after them (pot degrees)")
    p.add_argument("--out", required=True)
    a = p.parse_args(argv)
    if not 1 <= a.wipes <= MAX_WIPES:
        p.error(f"--wipes must be 1-{MAX_WIPES} (the bench caps cycles at {MAX_WIPES})")
    if not is_sha(a.expect_sha):
        p.error("--expect-sha needs at least 7 hex characters (a git sha)")
    return a


def is_sha(x):
    return len(x) >= 7 and all(c in string.hexdigits for c in x)


class Failed(Exception):
    """The run can't go on: stop the motor, write what there is, exit non-zero."""


def ask_zero(isatty=sys.stdin.isatty, ask=input):
    """Nick's by-eye reading of the arm against the marked zero, taken after the arm went back to the start."""
    if not isatty():
        return None
    try:
        s = ask("Arm is back at the start. Read it against the marked zero, by eye (deg; blank = not read): ").strip()
    except (KeyboardInterrupt, EOFError):
        return None
    try:
        return float(s) if s else None
    except ValueError:
        return None


def goto(b, deg, duty, sleep, clock):
    """Send the arm to `deg` and wait for it; a refusal or timeout fails the run."""
    r = b.post("/move", {"kind": "goto", "value": deg, "duty": duty, "method": "simple"})
    if r.get("error"):
        raise Failed(f"go to {deg:g} refused: {r['error']}")
    sleep(0.3)
    if not wait_idle(b, 30, sleep, clock):
        raise Failed(f"go to {deg:g} didn't finish in 30 s; stopped")


def run(a, b, out, sleep=time.sleep, clock=time.time, isatty=sys.stdin.isatty, ask=input):
    """The run itself, against a listening Bench `b`. Checks the bench first and creates `out` only then."""
    st = b.status
    if not idle(st):
        sys.exit("the bench isn't idle")
    sha = str(st.get("sha"))
    if not is_sha(sha) or not (sha.startswith(a.expect_sha) or a.expect_sha.startswith(sha)):
        sys.exit(f"the bench runs {sha}, not {a.expect_sha}: a stage 10 run on another sha doesn't count")
    if st.get("pot_state") != "trusted":
        sys.exit(f"the pot is {st.get('pot_state')} ({st.get('pot_why')}): press 'The arm is on the track' on the Jog "
                 f"page (arm still, on the track), then start the run again")
    os.makedirs(out, exist_ok=True)
    k_before = len(b.events)  # events already in the bench's last 20 belong to before this run
    meta = {"started": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"), "host": a.host,
            "args": vars(a), "sha": sha, "motor": a.motor, "settings": st.get("settings"),
            "pot_cal": st.get("pot_cal"), "pot_state": st.get("pot_state"), "pot_v": st.get("pot_v"),
            "events_before": list(b.events[:k_before]),  # the bench's last 20: the restart and the operator's confirm
            "lead_s": st.get("lead_s"), "leads": st.get("leads"), "throttled_before": st.get("throttled")}

    def save(name, data):
        with open(os.path.join(out, name), "w") as f:
            json.dump(data, f, indent=1)
    save("meta.json", meta)
    n_before = max([m.get("n", 0) for m in b.get("/api/moves")], default=0)

    def moves_after(n):
        return [m for m in b.get("/api/moves") if m.get("n", 0) > n]

    aborted, failed, moves, end, n_start = False, None, [], {}, None

    def record():
        """Write everything the run has so far; each part on its own, so a dead link loses only what needs it."""
        nonlocal moves
        if n_start is not None and not moves:
            try:
                moves = moves_after(n_start)
            except Exception:
                pass
        events = b.events[k_before:]
        save("moves.json", moves)
        save("events.json", events)
        with open(os.path.join(out, "samples.csv"), "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["t_s", "amps", "state", "angle"])
            w.writerows(b.rows)
        try:
            save("loop.json", b.get("/api/loop"))
        except Exception as e:
            save("loop.json", {"error": repr(e)})
        summary = summarize(moves)
        settings_after = b.status.get("settings") or {}
        fault = b.status.get("fault")
        summary.update({
            "aborted": aborted,
            "failed": failed,
            "strokes_expected": 2 * a.wipes,
            "complete": not aborted and not failed and not fault and len(moves) == 2 * a.wipes,
            "fault": fault,
            "stops": [e["text"] for e in stops(events)],
            "cutoff_ok": all((meta["settings"] or {}).get(k) == v and settings_after.get(k) == v
                             for k, v in CUTOFF.items()),
            "conditions_ok": all(getattr(a, k) == v for k, v in REGISTERED.items()),
            # the simple method brakes on lead_s; `leads` is only the approach method's (cam02 11/15 5:12 PM: leads
            # {} while lead_s carried what earlier runs taught, so "leads empty" alone said nothing)
            "untrained_at_start": meta.get("lead_s") == UNTRAINED_LEAD_S and not meta.get("leads"),
            "settings_after": settings_after,
            "throttled_before": meta["throttled_before"],
            "throttled_after": b.status.get("throttled"),
            "end_angle": end.get("angle"),
            "end_move": end.get("move"),
            "zero_check_deg": None,
        })
        save("summary.json", summary)
        return summary

    def stop_motor():
        try:
            b.post("/cmd", {"action": "stop"})
        except Exception:
            pass
        sleep(0.5)

    try:
        goto(b, a.start_deg, a.duty, sleep, clock)
        meta["start_angle"], meta["start_pot_v"] = b.status.get("angle"), b.status.get("pot_v")
        meta["start_move"] = moves_after(n_before)
        save("meta.json", meta)
        if meta["start_angle"] is None or abs(meta["start_angle"] - a.start_deg) > START_TOL_DEG:
            raise Failed(f"the arm stopped at {meta['start_angle']}, not within {START_TOL_DEG} deg of {a.start_deg}")
        n_start = max([m.get("n", 0) for m in b.get("/api/moves")], default=0)

        r = b.post("/move", {"kind": "wipe", "value": a.sweep, "cycles": a.wipes, "pause_s": a.pause_s,
                             "duty": a.duty, "method": "simple"})
        if r.get("error"):
            raise Failed(f"refused: {r['error']}")
        sleep(0.3)
        if not wait_idle(b, 20 + a.wipes * 6, sleep, clock):
            raise Failed(f"the wipes didn't finish in {20 + a.wipes * 6} s (link or stream lost?); stopped")
        sleep(0.5)
        moves = moves_after(n_start)
        n_end = max([m.get("n", 0) for m in b.get("/api/moves")], default=0)
        record()  # the run's record, before the return move
        goto(b, a.start_deg, a.duty, sleep, clock)  # back to the start for the zero check (not scored)
        end = {"angle": b.status.get("angle"), "pot_v": b.status.get("pot_v"), "move": moves_after(n_end)}
    except KeyboardInterrupt:
        aborted = True
        stop_motor()
    except Failed as e:
        failed = str(e)
        stop_motor()
    except Exception as e:  # the link or the stream dropped mid-run
        failed = f"{type(e).__name__}: {e}"
        stop_motor()
    b.stop.set()
    summary = record()
    if failed:
        sys.exit(f"{failed}; the record is in {out}")
    if not aborted:
        summary["zero_check_deg"] = ask_zero(isatty, ask)
        save("summary.json", summary)
    return summary


def main(argv=None):
    a = parse_args(argv)
    out = os.path.expanduser(a.out)
    if os.path.exists(out):
        sys.exit(f"{out} exists: a run never overwrites another (11/15 lost one that way)")
    b = Bench(a.host)
    b.listen()
    print(json.dumps(run(a, b, out), indent=1))


if __name__ == "__main__":
    main()
