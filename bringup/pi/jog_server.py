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
import pot as potmod
import wipe_analysis as wa
import wipe_logger as wl
from urchin_hw import raw_to_volts

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
# Moves to an angle (Jog page). The pot is read every MOVE_POT_EVERY samples while a move runs (~100 Hz on cam02).
MOVE_POT_EVERY = 2
DUTY = (0.05, 1.0)
LEAD_S = 0.04  # start: brake this long before the target at the current speed (pot latency + braking);
LEAD_S_RANGE = (0.0, 0.15)  # learned from each landing's overshoot (lead += overshoot / speed / 2)
LEARN_MIN_DPS = 100.0  # only strokes braked at least this fast teach the lead; slow corrections inflated it (11/15)
CORR_LEAD_S = 0.02  # fixed lead for corrections
SETTLE_S = 0.15  # wait after the brake before judging where the shaft stopped
TOL_DEG = 0.75  # a step or go-to within this is done; outside it, a slow correction move
CORR_DUTY = 0.75  # speed cap for corrections: the seal needs 75-100 % to turn (Nick, cam02 11/15; 0.3 and 0.5 stalled)
MAX_TRIES = 3  # corrections per target
NO_MOTION_S = 0.6  # driving but the angle moved < 0.5 deg in this long -> stop (stalled, or too slow to turn);
GAP_DEG = 35.0  # ...plus the time to cross the pot's ~30 deg gap, where the reading doesn't move, at this speed
WRONG_WAY_DEG = 5.0  # moving away from the target by this much -> stop: forward must raise the pot voltage
# (true on cam02 11/13); if the J3 leads are swapped it lowers it, and every move runs away.
FULL_SPEED_DPS = 300.0  # rough full-speed shaft speed (cam02 11/13: ~325 deg/s); only sets move timeouts
# During moves the ADC streams the pot (AIN2) and reads the current only every FAST_CUR_EVERY samples: at ~300 deg/s
# that's ~0.4 deg of travel between angle readings instead of ~3 deg (pot read every 2nd sample, ~10 ms apart,
# cam02 11/15). The current in between is held from the last read (stall cut-off, energy).
FAST_CUR_EVERY = 8
# Move methods. "simple": drive at the set speed, brake one learned lead before the target (lead_s).
# "approach": drive at the set speed until APPROACH_DEG out, then APPROACH_DUTY (raised by APPROACH_BUMP every
# BUMP_EVERY_S while slower than SPEED_FLOOR_DPS, e.g. on the brush's raised surface), and brake with a lead learned
# per direction and region. cam02 11/15 free shaft: 60 % ~105 deg/s and ~3 deg coast vs ~9 deg at 100 %.
METHODS = ("simple", "approach")
APPROACH_DEG = 25.0
APPROACH_DUTY = 0.6
SPEED_FLOOR_DPS = 40.0
APPROACH_BUMP = 0.1
BUMP_EVERY_S = 0.1
SURFACE_DEG = (0.0, 90.0)  # brush on the raised surface (Nick 11/15: 0 to 90 deg forward/CCW from the top)
NO_CURRENT_A = 0.02  # driving below 100 % with less than this for NO_CURRENT_S: the PWM stopped driving (11/15 bug)
NO_CURRENT_S = 0.3


class JogController:
    """Owns the motor and the sampler; the control thread calls tick() once per sample.

    request() and heartbeat() come from the HTTP threads, so they and tick() share a lock.
    States: "asleep" (nSLEEP_HB low), "fwd", "rev", "brake".
    """

    def __init__(self, motor, sampler, clock=time.monotonic, show=lambda phase: None, buf_s=12.0, rate=860,
                 pot=None, pot_every=0):
        self.motor, self.sampler, self.clock, self.show = motor, sampler, clock, show
        # Shaft angle: every `pot_every` samples, one single-shot read of AIN2, then AIN0 continuous again.
        self.pot, self.pot_every, self.ticks = pot, pot_every, 0
        self.angle = None
        self.pot_v = None
        self.pot_errors = 0
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
        self.events = collections.deque(maxlen=20)
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
        self.duty = 1.0  # speed of the current drive (PWM duty on the active IN pin)
        self.want_duty = 1.0
        # Moves to an angle: a queue of ("to", target_deg, correct, label) and ("pause", s); `move` is the one going.
        self.moves = collections.deque()
        self.move = None
        self.wait_until = None
        self.move_total = 0
        self.pot_hist = collections.deque(maxlen=4)
        self.lead_s = LEAD_S
        self.leads = {}  # approach method: learned lead per (direction sign, region)
        self.move_method = "simple"
        self.move_log = collections.deque(maxlen=500)  # one record per finished move (Jog page, comparisons)
        self.move_seq = 0
        self.prev_t = None
        self.nocur_since = None
        self.pwm_reset_done = False
        self.fast = False  # ADC streaming the pot (moves) instead of the current
        self.fast_k = 0
        self.last_cur = None

    # --- HTTP side ---
    def request_move(self, kind, value, duty=1.0, cycles=1, pause_s=0.3, method="simple"):
        """Jog page: "step" by `value` deg, "goto" `value` deg (the counted angle since zero), or "wipe": out by
        `value` deg and back to the start, `cycles` times, `pause_s` apart. Closed loop on the pot angle."""
        with self.lock:
            if not self.pot or self.angle is None:
                raise ValueError("no pot reading")
            if not self.pot.ok:
                raise ValueError("the pot is in its dead zone (angle unknown): turn the arm back onto the track")
            if self.state != "asleep" or self.cycle or self.run or self.move or self.moves:
                raise ValueError("stop the motor first")
            if method not in METHODS:
                raise ValueError(f"method must be one of {METHODS}")
            duty = min(DUTY[1], max(DUTY[0], float(duty)))
            value = float(value)
            if kind == "step":
                items = [("to", self.angle + value, True, f"step {value:+g}°")]
            elif kind == "goto":
                items = [("to", value, True, f"go to {value:g}°")]
            elif kind == "wipe":
                n = min(MAX_CYCLES, max(1, int(cycles)))
                p = min(PAUSE_S[1], max(PAUSE_S[0], float(pause_s)))
                start, items = self.angle, []
                for i in range(1, n + 1):
                    items += [("to", start + value, False, f"wipe {i}/{n} out {value:+g}°"), ("pause", p),
                              ("to", start, False, f"wipe {i}/{n} back")]
                    if i < n:
                        items.append(("pause", p))
            else:
                raise ValueError(kind)
            room = self.pot.room_deg()
            for it in items:
                if it[0] == "to" and not -room["minus"] <= it[1] - self.angle <= room["plus"]:
                    raise ValueError(f"{it[3]} would run into the pot's dead zone: from here the arm can go "
                                     f"+{room['plus']:.0f}° / -{room['minus']:.0f}°. Rotate the pot on the shaft so "
                                     f"the working range sits mid-track.")
            self.moves.extend(items)
            self.move_total = sum(1 for it in items if it[0] == "to")
            self.move_duty = duty
            self.move_method = method
            self.want = "stop"
            self.last_hb = self.clock()

    def _cancel_moves(self, now, why):
        if self.move or self.moves:
            left = len([it for it in self.moves if it[0] == "to"]) + (1 if self.move else 0)
            self._event(now, f"moves stopped ({why}); {left} not finished")
        self.moves.clear()
        self.move = None
        self.wait_until = None

    def request(self, action, seconds=None, cycles=1, pause_s=1.0, run=None, duty=1.0):
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
            else:  # Stop, or a manual direction, ends a cycle or a move
                if self.cycle:
                    self._end_cycle(now, "stop pressed" if action == "stop" else "manual drive", False)
                self._cancel_moves(now, "stop pressed" if action == "stop" else "manual drive")
                self.want = action
                self.want_duty = min(DUTY[1], max(DUTY[0], float(duty if duty is not None else 1.0)))
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
            self._cancel_moves(now, why)
        if self.state in ("fwd", "rev"):
            self._event(now, f"{self.state} stopped: {why} (peak {self.peak * 1000:.0f} mA)")
            self.motor.brake()
            self.state = "brake"
            self.brake_until = now + BRAKE_S
            self.last_stop = now
        self.show("brake")

    def _drive(self, now, direction, duty=1.0):
        if self.state == "asleep":
            self.motor.wake()
        self.motor.forward(duty) if direction == "fwd" else self.motor.reverse(duty)
        self.duty = duty
        self.pwm_reset_done, self.nocur_since = False, None
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
                elif not self.cycle and not self.move and now - self.drive_start > self.settings["max_on_s"]:
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
            elif (self.state == "asleep" and not self.move and self.want in ("fwd", "rev")
                  and now - self.last_hb <= WATCHDOG_S):
                self._drive(now, self.want, self.want_duty)

            if self.state in ("fwd", "rev"):
                self.sampler.zero_key = self.state
            elif self.state == "asleep":
                self.sampler.zero_key = "sleep"
            self.last_tick = now
            try:
                s, fast_pot_v = self._sample()  # brake keeps the zero of the stroke before it
            except OSError as e:  # cam02 sees rare I2C errors (11/12, 11/13); skip the sample, don't die
                self.i2c_errors += 1
                self.err_run += 1
                if self.err_run == 1:
                    self._event(now, f"I2C read failed ({e.strerror or e}); skipping samples")
                if self.err_run == SENSOR_FAIL_N and self.state in ("fwd", "rev"):
                    self._stop(now, f"current sensor not answering ({SENSOR_FAIL_N} reads)")
                if self.err_run % SENSOR_FAIL_N == 0:
                    try:
                        self._restart_adc()  # in case the ADC lost its setup
                    except OSError:
                        pass
                return None
            if self.err_run:
                self._event(now, f"I2C back after {self.err_run} failed read(s)")
                self.err_run = 0
            t = now - self.t0
            if self.state == "asleep":
                # The bridge can't conduct asleep, and VISEN then floats: cam02 read ~37-40 mV that drifts for
                # minutes after boot (11/13). Report 0 A; visen_v and raw keep the real reading.
                s["amps"] = 0.0
            if s["clipped"] and s.get("fresh_cur", True):
                self.clipped += 1
            if self.state in ("fwd", "rev"):
                self.peak = max(self.peak, s["amps"])
                if self.detector.update(t, s["amps"]):
                    self._stop(now, f"stall cut-off ({self.settings['stall_amps']:g} A for "
                                    f"{self.settings['stall_ms']:g} ms)")
                else:
                    self._check_drive_current(now, s["amps"])
            if self.move is not None and self.prev_t is not None and self.state != "asleep":
                self.move["energy_j"] += max(s["amps"], 0.0) * br.VBUS_NOMINAL * (now - self.prev_t)  # 5 V assumed, no divider
                self.move["peak_a"] = max(self.move["peak_a"], s["amps"])
            self.prev_t = now
            pot_v = self._read_pot(fast_pot_v)
            if pot_v is not None:
                self.pot_hist.append((now, self.angle))
            self._move_step(now, pot_v is not None)
            self.seq += 1
            row = {"seq": self.seq, "t_s": t, "state": self.state, "amps": s["amps"], "visen_v": s["visen_v"],
                   "raw": s["raw"], "clipped": s["clipped"], "cycle": self.cycle_i if self.run else None,
                   "pot_v": pot_v, "angle": self.angle, "pot_ok": None if pot_v is None else int(self.pot.ok)}
            self.buf.append(row)
            if self.run is not None:
                self.run.push(row)
                if self.cycle is None and self.state == "asleep":  # cycles over and the last brake logged
                    self.run.finish(self.cycle_result)
                    self.run = None
            return row

    def _restart_adc(self):
        if self.fast:
            self.sampler.adc.start_continuous(potmod.POT_CHANNEL, potmod.POT_FSR)
        else:
            self.sampler.begin()

    def _sample(self):
        """(current row, pot volts or None). Moves stream the pot; otherwise the current, as before."""
        want_fast = bool(self.pot and (self.move or self.moves))
        if want_fast and not self.fast:
            self.last_cur = self.sampler.take()  # last current before the switch
            self.fast, self.fast_k = True, 0
            self._restart_adc()
        elif not want_fast and self.fast:
            self.fast = False
            self._restart_adc()
        if not self.fast:
            return self.sampler.take(), None
        raw = self.sampler.adc.read_raw()
        v = raw_to_volts(raw, potmod.POT_FSR)
        self.fast_k += 1
        if self.fast_k % FAST_CUR_EVERY == 0:
            try:
                self.last_cur = self.sampler.take_single()
            finally:
                self._restart_adc()
            return dict(self.last_cur, fresh_cur=True), v
        return dict(self.last_cur, fresh_cur=False), v

    def _read_pot(self, fast_v=None):
        """Every pot_every ticks: AIN2 single-shot (the ADC leaves continuous mode), then AIN0 continuous again.
        Costs about one conversion plus the restart, so the current rate drops a little (see the status).
        While a move streams the pot (fast_v), every tick has a reading and nothing is switched."""
        if fast_v is not None:
            self.pot_v = fast_v
            self.angle = self.pot.update(fast_v, self.clock())
            return fast_v
        if self.fast:
            return None
        self.ticks += 1
        every = MOVE_POT_EVERY if (self.move or self.moves) else self.pot_every
        if not self.pot or not every or self.ticks % every:
            return None
        v = None
        try:
            v = self.sampler.adc.single_shot(potmod.POT_CHANNEL, potmod.POT_FSR)[1]
            self.pot_v = v
            self.angle = self.pot.update(v, self.clock())
        except (OSError, TimeoutError):
            self.pot_errors += 1
        try:
            self._restart_adc()
        except OSError:
            self.pot_errors += 1
        return v

    def _check_drive_current(self, now, amps):
        """11/15: a long-running server once drove 0 mA at every duty below 100 %. Re-create the PWM once, then stop."""
        if self.duty >= 1.0 or now - self.drive_start < 0.1 or amps >= NO_CURRENT_A:
            self.nocur_since = None
            return
        self.nocur_since = self.nocur_since or now
        if now - self.nocur_since < NO_CURRENT_S:
            return
        self.nocur_since = None
        if not self.pwm_reset_done:
            self.pwm_reset_done = True
            self._event(now, f"no drive current at {self.duty:.0%}: re-creating the PWM output")
            self.motor.coast()  # back to plain outputs (closes the PWM devices), then PWM again
            self._set_duty(self.duty)
        else:
            self._stop(now, f"no drive current at {self.duty:.0%} (PWM not driving; restart jog-bench)")

    def _set_duty(self, duty):
        self.motor.forward(duty) if self.state == "fwd" else self.motor.reverse(duty)
        self.duty = duty

    @staticmethod
    def _region(angle):
        return "surface" if SURFACE_DEG[0] <= angle <= SURFACE_DEG[1] else "free"

    def _speed_dps(self):
        h = [x for x in self.pot_hist if x[1] is not None]
        if len(h) < 2 or h[-1][0] - h[0][0] <= 0:
            return 0.0
        return (h[-1][1] - h[0][1]) / (h[-1][0] - h[0][0])

    def _move_step(self, now, fresh):
        """Runs once per tick: starts queued moves, stops each at its target, corrects, reports."""
        m = self.move
        if m is None:
            if not self.moves or self.state != "asleep" or self.cycle:
                return
            if now - self.last_hb > WATCHDOG_S:
                return self._cancel_moves(now, "lost contact with the page")
            if self.wait_until is not None and now < self.wait_until:
                return
            self.wait_until = None
            item = self.moves.popleft()
            if item[0] == "pause":
                self.wait_until = now + item[1]
                return
            _, target, correct, label = item
            m = self.move = {"target": target, "correct": correct, "label": label, "tries": 0, "driven": False,
                             "phase": "settle", "settle_until": now, "duty": self.move_duty,
                             "method": self.move_method, "start": self.angle, "t_start": now, "energy_j": 0.0,
                             "peak_a": 0.0}
        if m["phase"] == "drive":
            if self.state not in ("fwd", "rev"):  # a safety stop already braked and cancelled; nothing to do
                return
            if not self.pot.ok:
                return self._stop(now, f"{m['label']}: pot reading unreliable (dead zone?)")
            sign = self.pot.cal["sign"] * (1 if self.state == "fwd" else -1)  # forward raises the pot voltage
            remaining = (m["target"] - self.angle) * sign
            if fresh:
                # cap the speed: crossing the gap makes the angle jump, and a spike here would brake far too early
                speed = min(abs(self._speed_dps()), 1.5 * FULL_SPEED_DPS * self.duty)
                approach = m["method"] == "approach" and m["tries"] == 0
                if approach and not m.get("approaching") and remaining <= APPROACH_DEG and self.duty > APPROACH_DUTY:
                    m["approaching"], m["bump_t"] = True, now
                    self._set_duty(APPROACH_DUTY)
                elif (approach and m.get("approaching") and speed < SPEED_FLOOR_DPS and self.duty < 1.0
                      and now - m["bump_t"] >= BUMP_EVERY_S):
                    m["bump_t"] = now
                    self._set_duty(min(1.0, self.duty + APPROACH_BUMP))
                key = (sign, self._region(m["target"]))
                lead = (self.leads.get(key, LEAD_S) if approach else self.lead_s) if m["tries"] == 0 else CORR_LEAD_S
                if remaining <= speed * lead:
                    m["stop_speed"], m["stop_sign"], m["lead_key"] = speed, sign, key if approach else None
                    self.want = "stop"
                    self._stop(now, f"{m['label']}: at target", keep_want=True)
                    m["phase"], m["settle_until"] = "settle", now + BRAKE_S + SETTLE_S
                    return
                if abs(self.angle - m["progress_angle"]) >= 0.5:
                    m["progress_angle"], m["progress_t"] = self.angle, now
                m["best"] = min(m["best"], remaining)
                if remaining > m["best"] + WRONG_WAY_DEG:
                    return self._stop(now, f"{m['label']}: moving away from the target (J3 motor leads swapped?)")
            if now - m["progress_t"] > m["no_motion_s"]:
                return self._stop(now, f"{m['label']}: no motion for {m['no_motion_s']:.1f} s (stalled or too slow)")
            if now - m["t0"] > m["timeout"]:
                return self._stop(now, f"{m['label']}: timed out after {m['timeout']:.0f} s")
            return
        # settle: the motor is (or is about to be) stopped; judge, then finish or drive (again)
        if self.state != "asleep" or now < m["settle_until"]:
            return
        err = self.angle - m["target"]
        stop_speed = m.pop("stop_speed", 0)
        if stop_speed >= LEARN_MIN_DPS and m["tries"] == 0 and self.pot.ok:  # overshoot (+): brake earlier next time
            over = err * m.pop("stop_sign")
            key = m.pop("lead_key", None)
            old = self.leads.get(key, LEAD_S) if key else self.lead_s
            new = min(LEAD_S_RANGE[1], max(LEAD_S_RANGE[0], old + 0.5 * over / stop_speed))
            if key:
                self.leads[key] = new
            else:
                self.lead_s = new
        if not self.pot.ok:
            self._event(now, f"{m['label']}: stopped in the pot's dead zone, angle unknown")
            return self._cancel_moves(now, "pot in its dead zone")
        if abs(err) <= TOL_DEG or (m["driven"] and (not m["correct"] or m["tries"] >= MAX_TRIES)):
            self._event(now, f"{m['label']}: at {self.angle:.1f}° (target {m['target']:.1f}°, "
                             f"error {err:+.1f}°{', ' + str(m['tries']) + ' correction(s)' if m['tries'] else ''})")
            self.move_seq += 1
            self.move_log.append({
                "n": self.move_seq, "label": m["label"], "method": m["method"], "duty": m["duty"],
                "start": round(m["start"], 2), "target": round(m["target"], 2), "final": round(self.angle, 2),
                "error_deg": round(err, 2), "corrections": m["tries"],
                "dir": "fwd" if (m["target"] - m["start"]) * self.pot.cal["sign"] > 0 else "rev",
                "region": self._region(m["target"]), "duration_s": round(now - m["t_start"], 3),
                "energy_j": round(m["energy_j"], 4), "peak_ma": round(m["peak_a"] * 1000),
            })
            self.move = None
            return
        direction = "fwd" if (m["target"] - self.angle) * self.pot.cal["sign"] > 0 else "rev"
        duty = m["duty"] if not m["driven"] else min(m["duty"], CORR_DUTY)
        if m["driven"]:
            m["tries"] += 1
        timeout = abs(err) / (FULL_SPEED_DPS * duty) * 4 + 2.0
        m.update(phase="drive", driven=True, t0=now, timeout=min(60.0, timeout), progress_angle=self.angle,
                 progress_t=now, best=abs(err), no_motion_s=NO_MOTION_S + GAP_DEG / (FULL_SPEED_DPS * duty))
        self.want = direction
        self.pot_hist.clear()
        self._drive(now, direction, duty)

    def zero_angle(self):
        with self.lock:
            if not self.pot:
                raise ValueError("no pot")
            cal = self.pot.zero_here()
            self.angle = self.pot.angle
            return cal

    def flip_angle(self):
        with self.lock:
            if not self.pot:
                raise ValueError("no pot")
            cal = self.pot.flip()
            self.angle = self.pot.angle
            return cal

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
            mv = None
            if self.move or self.moves:
                left = len([it for it in self.moves if it[0] == "to"])
                mv = {"label": self.move["label"] if self.move else "pause", "phase": self.move["phase"] if self.move else "pause",
                      "target": round(self.move["target"], 2) if self.move else None,
                      "done": self.move_total - left - (1 if self.move else 0), "total": self.move_total}
            return {"state": self.state, "want": self.want, "cycle": dict(self.cycle) if self.cycle else None,
                    "duty": self.duty, "move": mv,
                    "run_id": self.run.meta["id"] if self.run else None, "last_result": self.cycle_result,
                    "run_s": round(now - self.drive_start, 2) if driving else None,
                    "peak_a": round(self.peak, 4), "settings": dict(self.settings), "events": list(self.events),
                    "clipped": self.clipped, "i2c_errors": self.i2c_errors, "fault": self.fault,
                    "tick_age_s": round(now - self.last_tick, 3),
                    "angle": None if self.angle is None else round(self.angle, 2), "pot_v": self.pot_v,
                    "pot_errors": self.pot_errors, "pot_cal": dict(self.pot.cal) if self.pot else None,
                    "pot_ok": self.pot.ok if self.pot else None, "room": self.pot.room_deg() if self.pot else None,
                    "lead_s": round(self.lead_s, 3),
                    "leads": {f"{'+' if k[0] > 0 else '-'}{k[1]}": round(v, 3) for k, v in self.leads.items()},
                    "move_log": list(self.move_log)[-12:], "method": self.move_method,
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
    FIELDS = ["t_s", "state", "amps", "visen_v", "raw", "clipped", "pot_v", "angle", "pot_ok"]

    def __init__(self, run_dir):
        self.path = os.path.join(run_dir, "samples.csv")
        self.f = open(self.path, "w", newline="")
        self.w = csv.DictWriter(self.f, fieldnames=self.FIELDS, extrasaction="ignore")
        self.w.writeheader()
        self.n = 0

    def write(self, row):
        self.w.writerow({**row, "t_s": f"{row['t_s']:.5f}", "amps": f"{row['amps']:.5f}",
                         "visen_v": f"{row['visen_v']:.6f}", "clipped": int(row["clipped"]),
                         "pot_v": "" if row.get("pot_v") is None else f"{row['pot_v']:.5f}",
                         "angle": "" if row.get("angle") is None else f"{row['angle']:.2f}"})
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
            "fsr": extra.get("fsr"), "zero_mv": ctrl.status()["zero_mv"], "source_sha": extra.get("sha"),
            "pot_cal": ctrl.status()["pot_cal"]}
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
            if path == "/api/moves":
                with ctrl.lock:
                    return list(ctrl.move_log)
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
            elif path == "/jog":
                with open(os.path.join(HERE, "jog_move_page.html"), "rb") as f:
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
                               "g": [None if r.get("angle") is None else round(r["angle"], 1) for r in rows],
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
                    ctrl.request(body["action"], body.get("seconds"), duty=body.get("duty", 1.0))
                elif self.path == "/move":
                    ctrl.request_move(body["kind"], body["value"], body.get("duty", 1.0), body.get("cycles", 1),
                                      body.get("pause_s", 0.3), body.get("method", "simple"))
                elif self.path == "/hb":
                    ctrl.heartbeat()
                elif self.path == "/settings":
                    return self._send(200, json.dumps(ctrl.set_settings(body)))
                elif self.path == "/pot/zero":
                    return self._send(200, json.dumps(ctrl.zero_angle()))
                elif self.path == "/pot/flip":
                    return self._send(200, json.dumps(ctrl.flip_angle()))
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
    p.add_argument("--pot-every", type=int, default=10,
                   help="read the shaft pot (AIN2) every N current samples; 0 = no pot (pot.py)")
    args = p.parse_args(argv)
    import urchin_hw as hw

    run_dir = wl.make_run_dir("jog_" + datetime.datetime.now().strftime("%H%M%S"))
    gpio = hw.open_gpio(prefer_lgpio=True)  # gpiozero device swaps + LED PWM lost the motor PWM (cam02 11/15)
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
    pot = potmod.PotAngle(potmod.load_cal(), path=potmod.CAL_PATH) if args.pot_every > 0 else None
    ctrl = JogController(motor, sampler, show=leds.show if leds else (lambda phase: None), rate=args.rate,
                         pot=pot, pot_every=args.pot_every)
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
