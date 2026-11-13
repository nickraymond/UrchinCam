#!/usr/bin/env python3
"""Jog bench: drive the wiper motor from a browser and watch its current live.

Run on the Pi with the expander board fitted, then tunnel from the Mac and open the page:
    python3 jog_server.py                                   # on the Pi; listens on 127.0.0.1:8765 only
    ssh -N -L 8765:127.0.0.1:8765 pi@<host>                 # on the Mac
    open http://localhost:8765

Forward / Reverse: hold to run (stops when released), or a quick click to latch on (click again or Stop).
Wipe cycle: forward for the set time, brake, then reverse for the same time, then stop.
The motor stops on its own when the stall cut-off trips, after the max run time, or when the page stops sending
heartbeats (tab closed, hidden or the tunnel dropped). Every drive is logged to
~/NereusData/UrchinCam/<YYYY-MM-DD>_jog_<HHMMSS>/samples.csv.

/runs is the Runs page: tagged multi-cycle runs (motor, wipe condition, stroke, cycles), each logged to its own
folder under ~/NereusData/UrchinCam/bench_runs/ (see bench_runs.py), a list to review them, and statistics across
the runs ticked "include".
"""

import argparse
import collections
import csv
import datetime
import json
import os
import statistics
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import bench_runs as br
import wipe_analysis as wa
import wipe_logger as wl

HERE = os.path.dirname(os.path.abspath(__file__))
BRAKE_S = 0.1  # brake before a stop or a direction change, as the logger does
LOG_TAIL_S = 1.0  # keep logging this long after the motor stops
LIMITS = {  # setting: (default, min, max)
    "stall_amps": (0.5, 0.05, 4.0),
    "stall_ms": (250.0, 20.0, 2000.0),
    "max_on_s": (10.0, 0.5, 60.0),
}
CYCLE_S = (0.5, 30.0)  # wipe cycle stroke time limits; the stroke time replaces max_on_s during a cycle
MAX_CYCLES = 100
PAUSE_S = (0.0, 30.0)  # rest between the cycles of a run
WATCHDOG_S = 1.5  # no heartbeat for this long while driving -> stop
SENSOR_FAIL_N = 20  # consecutive failed ADC reads (~25 ms) while driving -> stop: the stall cut-off is blind


class JogController:
    """Owns the motor and the sampler; the control thread calls tick() once per sample.

    request() and heartbeat() come from the HTTP threads, so they and tick() share a lock.
    States: "asleep" (nSLEEP_HB low), "fwd", "rev", "brake".
    """

    def __init__(self, motor, sampler, clock=time.monotonic, show=lambda phase: None, buf_s=12.0, rate=860):
        self.motor, self.sampler, self.clock, self.show = motor, sampler, clock, show
        self.lock = threading.Lock()
        self.settings = {k: v[0] for k, v in LIMITS.items()}
        self.state = "asleep"
        self.want = "stop"
        self.last_hb = 0.0
        self.drive_dir = None
        self.drive_start = None
        self.brake_until = None
        self.detector = None
        self.peak = 0.0
        self.last_stop = None
        self.t0 = clock()
        self.seq = 0
        self.buf = collections.deque(maxlen=int(buf_s * rate))
        self.events = collections.deque(maxlen=8)
        self.clipped = 0
        self.i2c_errors = 0
        self.err_run = 0
        self.last_tick = clock()
        self.fault = None  # set by the control thread if it dies; the page shows it
        # While wipe cycles run: {"s": stroke s, "n": cycles, "i": current cycle (1-based), "done": complete cycles,
        # "stage": "fwd" | "rev" | "pause", "pause_s", "pause_until"}
        self.cycle = None
        self.cycle_i = None  # last cycle number, for the rows logged during the final brake
        self.cycle_result = None
        self.run = None  # a bench_runs.RunLogger while a logged run is going

    # --- HTTP side ---
    def request(self, action, seconds=None, cycles=1, pause_s=1.0, run=None):
        """fwd / rev / stop, or "cycle": `cycles` wipe cycles of `seconds` each way, `pause_s` apart.
        `run` (a RunLogger) gets every row until the cycles end, then finish(result)."""
        if action not in ("fwd", "rev", "stop", "cycle"):
            raise ValueError(action)
        with self.lock:
            now = self.clock()
            if action == "cycle":
                if self.state != "asleep" or self.cycle or self.run:
                    raise ValueError("stop the motor before a wipe cycle")
                s = min(CYCLE_S[1], max(CYCLE_S[0], float(seconds)))
                n = min(MAX_CYCLES, max(1, int(cycles)))
                p = min(PAUSE_S[1], max(PAUSE_S[0], float(pause_s)))
                self.cycle = {"s": s, "n": n, "i": 1, "done": 0, "stage": "fwd", "pause_s": p, "pause_until": None}
                self.cycle_i, self.cycle_result, self.run = 1, None, run
                self.want = "fwd"
            else:  # Stop, or a manual direction, ends a cycle
                if self.cycle:
                    self._end_cycle(now, "stop pressed" if action == "stop" else "manual drive", False)
                self.want = action
            self.last_hb = now

    def heartbeat(self):
        with self.lock:
            self.last_hb = self.clock()

    def set_settings(self, new):
        with self.lock:
            for k, v in new.items():
                if k in LIMITS:
                    _, lo, hi = LIMITS[k]
                    self.settings[k] = min(hi, max(lo, float(v)))
            return dict(self.settings)

    # --- control thread ---
    def _event(self, now, text):
        self.events.append({"t": round(now - self.t0, 3), "text": text})

    def _end_cycle(self, now, reason, complete):
        c = self.cycle
        self.cycle_result = {"complete": complete, "reason": reason, "cycles_done": c["done"], "cycles": c["n"]}
        self.cycle = None
        if c["n"] > 1 or self.run:
            self._event(now, f"run {'complete' if complete else 'stopped'}: {c['done']}/{c['n']} cycles"
                             f"{'' if complete else ' (' + reason + ')'}")

    def _stop(self, now, why, keep_want=False):
        if not keep_want:  # a safety stop needs a new press to start again
            self.want = "stop"
            if self.cycle:
                self._end_cycle(now, why, False)
        if self.state in ("fwd", "rev"):
            self._event(now, f"{self.state} stopped: {why} (peak {self.peak * 1000:.0f} mA)")
            self.motor.brake()
            self.state = "brake"
            self.brake_until = now + BRAKE_S
            self.last_stop = now
        self.show("brake")

    def _drive(self, now, direction):
        if self.state == "asleep":
            self.motor.wake()
        self.motor.forward() if direction == "fwd" else self.motor.reverse()
        self.state = self.drive_dir = direction
        self.drive_start = now
        self.peak = 0.0
        self.detector = wa.StallDetector(self.settings["stall_amps"], self.settings["stall_ms"])
        self.show(direction)

    def tick(self):
        """Apply the request and the safety limits, take one sample, return it."""
        with self.lock:
            now = self.clock()
            if self.state in ("fwd", "rev"):
                if now - self.last_hb > WATCHDOG_S:
                    self._stop(now, "lost contact with the page")
                elif self.cycle and now - self.drive_start >= self.cycle["s"]:
                    c = self.cycle
                    tag = f" {c['i']}/{c['n']}" if c["n"] > 1 else ""
                    if c["stage"] == "fwd":
                        c["stage"] = self.want = "rev"
                        self._stop(now, f"wipe cycle{tag} forward {c['s']:g} s done", keep_want=True)
                    else:
                        c["done"] += 1
                        self.want = "stop"
                        self._stop(now, f"wipe cycle{tag} done ({c['s']:g} s each way)", keep_want=True)
                        if c["i"] < c["n"]:
                            c["stage"] = "pause"
                            c["pause_until"] = now + BRAKE_S + c["pause_s"]
                        else:
                            self._end_cycle(now, "complete", True)
                elif not self.cycle and now - self.drive_start > self.settings["max_on_s"]:
                    self._stop(now, f"max run time {self.settings['max_on_s']:g} s")
                elif self.want != self.state:  # stop pressed, or the other direction: brake first
                    self._stop(now, "stop pressed" if self.want == "stop" else "direction change", keep_want=True)
            if self.state == "brake" and now >= self.brake_until:
                self.motor.sleep()
                self.state = "asleep"
                self.show("asleep")
            elif self.state == "asleep" and self.cycle and self.cycle["stage"] == "pause":
                c = self.cycle
                if now - self.last_hb > WATCHDOG_S:
                    self._end_cycle(now, "lost contact with the page", False)
                elif now >= c["pause_until"]:
                    c["i"] += 1
                    c["stage"] = self.want = "fwd"
                    self.cycle_i = c["i"]
                    self._drive(now, "fwd")
            elif self.state == "asleep" and self.want in ("fwd", "rev") and now - self.last_hb <= WATCHDOG_S:
                self._drive(now, self.want)

            if self.state in ("fwd", "rev"):
                self.sampler.zero_key = self.state
            elif self.state == "asleep":
                self.sampler.zero_key = "sleep"
            self.last_tick = now
            try:
                s = self.sampler.take()  # brake keeps the zero of the stroke before it
            except OSError as e:  # cam02 sees rare I2C errors (11/12, 11/13); skip the sample, don't die
                self.i2c_errors += 1
                self.err_run += 1
                if self.err_run == 1:
                    self._event(now, f"I2C read failed ({e.strerror or e}); skipping samples")
                if self.err_run == SENSOR_FAIL_N and self.state in ("fwd", "rev"):
                    self._stop(now, f"current sensor not answering ({SENSOR_FAIL_N} reads)")
                if self.err_run % SENSOR_FAIL_N == 0:
                    try:
                        self.sampler.begin()  # restart continuous mode in case the ADC lost its setup
                    except OSError:
                        pass
                return None
            if self.err_run:
                self._event(now, f"I2C back after {self.err_run} failed read(s)")
                self.err_run = 0
            t = now - self.t0
            if s["clipped"]:
                self.clipped += 1
            if self.state in ("fwd", "rev"):
                self.peak = max(self.peak, s["amps"])
                if self.detector.update(t, s["amps"]):
                    self._stop(now, f"stall cut-off ({self.settings['stall_amps']:g} A for "
                                    f"{self.settings['stall_ms']:g} ms)")
            self.seq += 1
            row = {"seq": self.seq, "t_s": t, "state": self.state, "amps": s["amps"], "visen_v": s["visen_v"],
                   "raw": s["raw"], "clipped": s["clipped"], "cycle": self.cycle_i if self.run else None}
            self.buf.append(row)
            if self.run is not None:
                self.run.push(row)
                if self.cycle is None and self.state == "asleep":  # cycles over and the last brake logged
                    self.run.finish(self.cycle_result)
                    self.run = None
            return row

    def logging_now(self):
        return self.state != "asleep" or (self.last_stop is not None and self.clock() - self.last_stop < LOG_TAIL_S)

    def since(self, seq):
        """Rows newer than seq, oldest first (walks back from the newest, so it costs only the new rows)."""
        with self.lock:
            out = []
            for row in reversed(self.buf):
                if row["seq"] <= seq:
                    break
                out.append(row)
            out.reverse()
            return out

    def status(self):
        with self.lock:
            now = self.clock()
            driving = self.state in ("fwd", "rev")
            return {"state": self.state, "want": self.want, "cycle": dict(self.cycle) if self.cycle else None,
                    "run_id": self.run.meta["id"] if self.run else None, "last_result": self.cycle_result,
                    "run_s": round(now - self.drive_start, 2) if driving else None,
                    "peak_a": round(self.peak, 4), "settings": dict(self.settings), "events": list(self.events),
                    "clipped": self.clipped, "i2c_errors": self.i2c_errors, "fault": self.fault,
                    "tick_age_s": round(now - self.last_tick, 3),
                    "zero_mv": {k: round(v * 1000, 3) for k, v in self.sampler.zero_v.items()}}


def measure_all_zeros(motor, sampler, period, sleep=time.sleep, n=64):
    """The logger's fwd/rev zeros (driver awake, motor at rest) plus one with the driver asleep."""
    motor.sleep()
    sleep(0.1)
    sampler.zero_v["sleep"] = 0.0
    sampler.zero_key = "sleep"
    vals = []
    for _ in range(n):
        vals.append(sampler.take()["visen_v"])
        sleep(period)
    zeros = {"sleep": statistics.median(vals)}
    motor.wake()
    zeros.update(wl.measure_zeros(motor, sampler, period, sleep))
    motor.sleep()
    sampler.zero_v = dict(zeros)
    return zeros


def deployed_sha():
    """The Pi copy isn't a git checkout: deploy writes the commit to VERSION next to this file."""
    try:
        with open(os.path.join(HERE, "VERSION")) as f:
            return f.read().strip()
    except OSError:
        return wl.source_sha()


def bind_addresses(spec):
    out = []
    for item in spec.split(","):
        item = item.strip()
        if item == "tailscale":  # fails until tailscaled is up; systemd restarts us
            item = subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True, check=True,
                                  timeout=5).stdout.split()[0]
        out.append(item)
    return out


def throttled():
    try:
        return subprocess.run(["vcgencmd", "get_throttled"], capture_output=True, text=True,
                              timeout=2).stdout.strip().split("=")[-1]
    except (OSError, subprocess.SubprocessError):
        return None


class Recorder:
    FIELDS = ["t_s", "state", "amps", "visen_v", "raw", "clipped"]

    def __init__(self, run_dir):
        self.path = os.path.join(run_dir, "samples.csv")
        self.f = open(self.path, "w", newline="")
        self.w = csv.DictWriter(self.f, fieldnames=self.FIELDS, extrasaction="ignore")
        self.w.writeheader()
        self.n = 0

    def write(self, row):
        self.w.writerow({**row, "t_s": f"{row['t_s']:.5f}", "amps": f"{row['amps']:.5f}",
                         "visen_v": f"{row['visen_v']:.6f}", "clipped": int(row["clipped"])})
        self.n += 1
        if self.n % 860 == 0:
            self.f.flush()

    def close(self):
        self.f.close()


def start_run(ctrl, body, extra, root=br.RUNS_ROOT, options_path=br.OPTIONS_PATH):
    """POST /api/run/start: validate the pick against bench_options.json, open the run folder, start the cycles."""
    st = ctrl.status()
    if st["state"] != "asleep" or st["cycle"] or st["run_id"]:
        raise ValueError("stop the motor before a run")  # checked again under the lock in request()
    opts = br.load_options(options_path)
    motor, condition = body.get("motor"), body.get("condition")
    if motor not in opts["motors"] or condition not in opts["conditions"]:
        raise ValueError("pick a motor and a condition from the lists")
    meta = {"motor": motor, "condition": condition, "mode": "time",
            "stroke_s": min(CYCLE_S[1], max(CYCLE_S[0], float(body["stroke_s"]))),
            "cycles": min(MAX_CYCLES, max(1, int(body["cycles"]))),
            "pause_s": min(PAUSE_S[1], max(PAUSE_S[0], float(body.get("pause_s", 1.0)))),
            "notes": str(body.get("notes", ""))[:500], "settings": ctrl.status()["settings"],
            "fsr": extra.get("fsr"), "zero_mv": ctrl.status()["zero_mv"], "source_sha": extra.get("sha")}
    run = br.RunLogger(meta, root=root)
    try:
        ctrl.request("cycle", meta["stroke_s"], meta["cycles"], meta["pause_s"], run=run)
    except ValueError:
        run.abort()
        raise
    return run.meta["id"]


def make_handler(ctrl, extra, runs_root=br.RUNS_ROOT):
    page = os.path.join(HERE, "jog_page.html")
    runs_page = os.path.join(HERE, "runs_page.html")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # quiet: the stream would flood the log
            pass

        def _send(self, code, body, ctype="application/json"):
            data = body.encode() if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _api_get(self, path):
            parts = path.strip("/").split("/")  # api, runs, <id>, trace
            if path == "/api/options":
                return br.load_options()
            if path == "/api/runs":
                return br.list_runs(runs_root)
            if path == "/api/summary":
                return br.group_stats(runs_root)
            if len(parts) == 3 and parts[1] == "runs":
                return br.load_run(runs_root, parts[2])
            if len(parts) == 4 and parts[1] == "runs" and parts[3] == "trace":
                return br.trace(runs_root, parts[2])
            raise KeyError(path)

        def do_GET(self):
            path = self.path.split("?")[0]
            if path in ("/", "/index.html"):
                with open(page, "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            elif path == "/runs":
                with open(runs_page, "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            elif path.startswith("/api/runs/") and path.endswith("/samples.csv"):
                try:
                    d = br.run_dir(runs_root, path.split("/")[3])
                except (ValueError, KeyError):
                    return self._send(404, '{"error": "no such run"}')
                with open(os.path.join(d, "samples.csv"), "rb") as f:
                    data = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/csv")
                self.send_header("Content-Disposition", f'attachment; filename="{os.path.basename(d)}.csv"')
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            elif path.startswith("/api/"):
                try:
                    self._send(200, json.dumps(self._api_get(path)))
                except KeyError:
                    self._send(404, '{"error": "not found"}')
                except ValueError as e:
                    self._send(400, json.dumps({"error": str(e)}))
            elif path == "/stream":
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                seq = max(0, ctrl.seq - 860 * 5)  # start with the last 5 s
                try:
                    while True:
                        rows = ctrl.since(seq)
                        if rows:
                            seq = rows[-1]["seq"]
                        msg = {"t": [round(r["t_s"], 4) for r in rows],
                               "a": [round(r["amps"], 5) for r in rows],
                               "s": [r["state"][0] for r in rows],  # a f r b
                               "status": {**ctrl.status(), **extra}}
                        self.wfile.write(f"data: {json.dumps(msg)}\n\n".encode())
                        self.wfile.flush()
                        time.sleep(0.05)
                except (BrokenPipeError, ConnectionResetError):
                    return
            else:
                self._send(404, '{"error": "not found"}')

        def do_POST(self):
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                if self.path == "/cmd":
                    ctrl.request(body["action"], body.get("seconds"))
                elif self.path == "/hb":
                    ctrl.heartbeat()
                elif self.path == "/settings":
                    return self._send(200, json.dumps(ctrl.set_settings(body)))
                elif self.path == "/api/run/start":
                    return self._send(200, json.dumps({"id": start_run(ctrl, body, extra, runs_root)}))
                elif self.path.startswith("/api/runs/") and self.path.endswith("/include"):
                    meta = br.set_excluded(runs_root, self.path.split("/")[3], not body["include"])
                    return self._send(200, json.dumps(meta))
                else:
                    return self._send(404, '{"error": "not found"}')
                self._send(200, '{"ok": true}')
            except (ValueError, KeyError, TypeError) as e:
                self._send(400, json.dumps({"error": str(e)}))

    return Handler


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--bind", default="127.0.0.1",
                   help="comma list; 127.0.0.1 = SSH tunnel only, 'tailscale' = this Pi's Tailscale IPv4")
    p.add_argument("--fsr", type=float, default=1.024, choices=[0.256, 0.512, 1.024, 2.048],
                   help="ADS1115 range; 1.024 V reads to 5.1 A so a stall is not clipped (VISEN_SCALE.md option B)")
    p.add_argument("--rate", type=int, default=860, choices=[128, 250, 475, 860])
    p.add_argument("--r-iset", type=float, default=2000.0)
    p.add_argument("--led-level", type=float, default=0.02, help="LED1 phase colors; 0 = off")
    args = p.parse_args(argv)
    import urchin_hw as hw

    run_dir = wl.make_run_dir("jog_" + datetime.datetime.now().strftime("%H%M%S"))
    gpio = hw.open_gpio()
    motor = hw.Motor(gpio)
    dev = hw.I2CDevice(hw.ADS1115_ADDR)
    adc = hw.ADS1115(dev, fsr=args.fsr, rate=args.rate)
    sampler = wl.Sampler(adc, args.fsr, args.r_iset)
    leds = None
    if args.led_level > 0:
        try:
            leds = wl.PhaseLeds(args.led_level)
        except ImportError:
            print("gpiozero missing: no LED1 phase colors", file=sys.stderr)
    period = 1.0 / args.rate
    sampler.begin()
    zeros = measure_all_zeros(motor, sampler, period)
    print(f"VISEN zeros (mV): { {k: round(v * 1000, 3) for k, v in zeros.items()} }")
    ctrl = JogController(motor, sampler, show=leds.show if leds else (lambda phase: None), rate=args.rate)
    rec = Recorder(run_dir)
    extra = {"throttled": throttled(), "fsr": args.fsr, "run_dir": run_dir, "i2c_retries": 0,
             "sha": deployed_sha()}
    stop = threading.Event()

    def control_loop():
        next_t = time.monotonic()
        k = 0
        while not stop.is_set():
            try:
                row = ctrl.tick()
            except Exception as e:  # never leave the bridge driving with nobody watching the current
                motor.sleep()
                ctrl.fault = f"controller stopped: {e!r}"
                print(ctrl.fault, file=sys.stderr, flush=True)
                return
            if row is not None and ctrl.logging_now():
                rec.write(row)
            k += 1
            if k % (args.rate * 2) == 0:  # every ~2 s
                extra["throttled"] = throttled()
                extra["i2c_retries"] = dev.retries
            next_t += period
            delay = next_t - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                next_t = time.monotonic()

    worker = threading.Thread(target=control_loop, daemon=True)
    worker.start()
    servers = []
    try:
        for addr in bind_addresses(args.bind):
            srv = ThreadingHTTPServer((addr, args.port), make_handler(ctrl, extra))
            srv.daemon_threads = True
            servers.append(srv)
            print(f"jog bench on http://{addr}:{args.port}  (runs page /runs)", flush=True)
        print(f"jog log {rec.path}", flush=True)
        for srv in servers[1:]:
            threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers[0].serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        worker.join(timeout=1)
        motor.sleep()
        if leds:
            leds.close()
        rec.close()
        try:
            adc.stop()
        finally:
            dev.close()
            gpio.close()


def _on_sigterm(*_):
    raise KeyboardInterrupt  # same clean shutdown as Ctrl-C: motor asleep, log closed


if __name__ == "__main__":
    import signal
    signal.signal(signal.SIGTERM, _on_sigterm)
    main()
