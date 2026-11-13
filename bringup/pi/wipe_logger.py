#!/usr/bin/env python3
"""Wipe logger: drive the wiper motor forward and back, log motor current, report energy per wipe and stall current.

Run on the Pi with the expander board fitted (test card stages 7-9):
    python3 wipe_logger.py --run seal --fwd-s 1.5 --rev-s 1.5 --wipes 10 --motor 4791 --noload-amps 0.04

Writes ~/NereusData/UrchinCam/<YYYY-MM-DD>_<run>/ with samples.csv, summary.json and MANIFEST.md.
The software stall cut-off is always on: the MP6550's own limit (~2.5 A with R3 = 2 kOhm) is far above a micro
metal gearmotor's stall current (see ../VISEN_SCALE.md).
"""

import argparse
import datetime
import json
import os
import socket
import statistics
import subprocess
import sys
import time

import wipe_analysis as wa
from urchin_hw import raw_to_volts

DATA_ROOT = os.path.expanduser("~/NereusData/UrchinCam")
MAX_STROKE_S = 10.0
VBUS_FSR = 4.096
# With the MP6550 awake and no load, VISEN reads a few mV (cam02, J3 open, 11/12, 5 sequences): ~4.4 mV in
# coast, forward, and brake entered from forward or coast; ~5.5 mV in reverse and in brake entered straight from
# reverse; ~0.2 mV asleep. Coast resets it to the forward value. So there are two zeros: "fwd" and "rev".
ZERO_MAX_V = 0.015  # a zero above 15 mV (75 mA) means current is flowing: the motor is moving
REV_PULSE_S = 0.002  # reverse pulse before the brake that reads the "rev" zero; far too short to move a gearmotor


class Sampler:
    """Reads VISEN (AIN0) and, if a divider is fitted, +5V on AIN1.

    `visen_v` is the raw reading; `amps` subtracts the no-load zero named by `zero_key` ("fwd" or "rev").
    """

    def __init__(self, adc, fsr, r_iset, vbus_divider=None, vbus_every=4):
        self.adc, self.fsr, self.r_iset = adc, fsr, r_iset
        self.vbus_divider, self.vbus_every = vbus_divider, vbus_every
        self.k = 0
        self.vbus = None
        self.zero_v = {"fwd": 0.0, "rev": 0.0}
        self.zero_key = "fwd"

    def begin(self):
        if self.vbus_divider is None:
            self.adc.start_continuous(0, self.fsr)  # fastest path: one channel, back-to-back conversions

    def take(self):
        fresh = 0
        if self.vbus_divider is None:
            raw = self.adc.read_raw()
        else:
            if self.k % self.vbus_every == 0:
                _, v = self.adc.single_shot(1, VBUS_FSR)
                self.vbus = v * self.vbus_divider
                fresh = 1
            raw, _ = self.adc.single_shot(0, self.fsr)
            self.k += 1
        visen = raw_to_volts(raw, self.fsr)
        zero = self.zero_v[self.zero_key]
        return {
            "raw": raw,
            "visen_v": visen,
            "amps": wa.visen_to_amps(visen - zero, self.r_iset),
            "vbus_v": self.vbus,
            "vbus_fresh": fresh,
            "clipped": raw == 0x7FFF,
        }


class ZeroError(RuntimeError):
    pass


def measure_zeros(motor, sampler, period, sleep, settle_s=0.1, n=64):
    """VISEN with the driver awake and the motor at rest, so no current flows.

    "fwd": in coast. "rev": in brake entered straight from a 2 ms reverse pulse, which holds the reverse offset.
    """

    def rev_then_brake():
        motor.reverse()
        sleep(REV_PULSE_S)
        motor.brake()

    out = {}
    for key, set_state in (("fwd", motor.coast), ("rev", rev_then_brake)):
        set_state()
        sleep(settle_s)
        vals = []
        for _ in range(n):
            vals.append(sampler.take()["visen_v"])
            sleep(period)
        out[key] = statistics.median(vals)
    motor.coast()
    bad = {k: v for k, v in out.items() if abs(v) > ZERO_MAX_V}
    if bad:
        raise ZeroError("VISEN zero out of range (" + ", ".join(f"{k} {v * 1000:.1f} mV" for k, v in bad.items())
                        + "): is the motor still moving? Use --zero off to skip.")
    return out


def run_wipes(motor, sampler, args, clock=time.monotonic, sleep=time.sleep):
    """Returns (samples, strokes_meta, zeros). Leaves the motor coasting; the caller puts it to sleep.

    With --zero auto (default) the no-load VISEN zero is measured before the first wipe and subtracted from
    `amps`; it is measured again after the last one so the summary shows any drift.
    """
    period = 1.0 / args.rate
    samples, strokes = [], []
    t0 = clock()
    zeros = {"start": None, "end": None}

    sampler.begin()
    motor.wake()
    if args.zero == "auto":
        zeros["start"] = measure_zeros(motor, sampler, period, sleep)
        sampler.zero_v = dict(zeros["start"])
    _drive(motor, sampler, args, clock, sleep, period, t0, samples, strokes)
    if args.zero == "auto":
        sleep(args.pause_s)
        try:
            zeros["end"] = measure_zeros(motor, sampler, period, sleep)
        except ZeroError as e:  # never lose a finished run over the drift check
            print(f"end zero not recorded: {e}", file=sys.stderr)
    return samples, strokes, zeros


def _drive(motor, sampler, args, clock, sleep, period, t0, samples, strokes):
    stalls = 0

    def log_for(phase, wipe, seconds, detector=None):
        if phase != "brake":  # brake keeps the zero of the stroke before it
            sampler.zero_key = "rev" if phase == "rev" else "fwd"
        start = clock()
        next_t = start
        while clock() - start < seconds:
            s = sampler.take()
            s.update({"t_s": clock() - t0, "wipe": wipe, "phase": phase})
            samples.append(s)
            if detector is not None and detector.update(s["t_s"], s["amps"]):
                return detector.since
            next_t += period
            delay = next_t - clock()
            if delay > 0:
                sleep(delay)
            else:
                next_t = clock()  # fell behind; don't try to catch up with a burst
        return None

    for wipe in range(1, args.wipes + 1):
        for phase, seconds in (("fwd", args.fwd_s), ("rev", args.rev_s)):
            if seconds <= 0:
                continue
            motor.forward() if phase == "fwd" else motor.reverse()
            stall_t = log_for(phase, wipe, seconds, wa.StallDetector(args.stall_amps, args.stall_ms))
            motor.brake()
            strokes.append({"wipe": wipe, "phase": phase, "end": "stall" if stall_t is not None else "time",
                            "stall_t": stall_t})
            log_for("brake", wipe, args.brake_s)
            motor.coast()
            log_for("pause", wipe, args.pause_s)
            if stall_t is not None:
                stalls += 1
                print(f"wipe {wipe} {phase}: stall cut-off at {stall_t:.3f} s", file=sys.stderr)
                if stalls >= args.max_stalls:
                    print(f"stopping: {stalls} stall(s) reached --max-stalls", file=sys.stderr)
                    return
                break  # skip the rest of this wipe


def make_run_dir(run):
    base = os.path.join(DATA_ROOT, f"{datetime.date.today():%Y-%m-%d}_{run}")
    path, n = base, 2
    while os.path.exists(path):
        path, n = f"{base}_{n}", n + 1
    os.makedirs(path)
    return path


def source_sha():
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        return subprocess.run(["git", "-C", here, "rev-parse", "--short", "HEAD"], capture_output=True,
                              text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown (not a git checkout)"


def write_outputs(run_dir, samples, summary, args, achieved_rate, started=None):
    wa.write_csv(os.path.join(run_dir, "samples.csv"), samples)
    summary = dict(summary, args=vars(args), achieved_rate_sps=achieved_rate, n_samples=len(samples))
    with open(os.path.join(run_dir, "summary.json"), "w") as f:
        json.dump(summary, f, indent=2)
    with open(os.path.join(run_dir, "MANIFEST.md"), "w") as f:
        f.write(f"# {os.path.basename(run_dir)}\n\n"
                f"- What: wiper motor current log ({args.wipes} wipe(s), fwd {args.fwd_s} s, rev {args.rev_s} s)\n"
                f"- Source: UrchinCam bringup/pi @ {source_sha()}\n"
                f"- Host: {socket.gethostname()}\n"
                f"- Unit: UrchinCam expander board; motor {args.motor}\n"
                f"- Started: {(started or datetime.datetime.now()).astimezone():%Y-%m-%d %I:%M %p %Z}\n"
                f"- Files: samples.csv, summary.json\n")
    return summary


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--run", required=True, help="short run name, e.g. motor_free, seal, stall")
    p.add_argument("--motor", required=True, help="motor part number, recorded in the manifest")
    p.add_argument("--wipes", type=int, default=10)
    p.add_argument("--fwd-s", type=float, required=True, help="forward stroke time (s)")
    p.add_argument("--rev-s", type=float, required=True, help="reverse stroke time (s); 0 skips it")
    p.add_argument("--brake-s", type=float, default=0.2)
    p.add_argument("--pause-s", type=float, default=0.5)
    p.add_argument("--stall-amps", type=float, default=0.25)
    p.add_argument("--stall-ms", type=float, default=150.0)
    p.add_argument("--max-stalls", type=int, default=1, help="stop the run after this many stalls")
    p.add_argument("--inrush-ms", type=float, default=50.0, help="start of a stroke left out of the peak")
    p.add_argument("--fsr", type=float, default=0.256, choices=[0.256, 0.512, 1.024, 2.048])
    p.add_argument("--rate", type=int, default=860, choices=[128, 250, 475, 860])
    p.add_argument("--r-iset", type=float, default=2000.0, help="R3 in ohms (2000 as built)")
    p.add_argument("--vbus-divider", type=float, default=None, help="AIN1 divider ratio, e.g. 2.0 for 10k+10k")
    p.add_argument("--vbus-nominal", type=float, default=5.0, help="used for energy when there's no divider")
    p.add_argument("--noload-amps", type=float, default=None, help="free-run current (test card 7.4)")
    p.add_argument("--stall-ref-amps", type=float, default=None, help="stall current from a stall run (9.2)")
    p.add_argument("--zero", choices=["auto", "off"], default="auto",
                   help="auto: measure and subtract VISEN's no-load zero (coast and brake, motor at rest)")
    a = p.parse_args(argv)
    for name in ("fwd_s", "rev_s"):
        if not 0 <= getattr(a, name) <= MAX_STROKE_S:
            p.error(f"--{name.replace('_', '-')} must be 0-{MAX_STROKE_S} s")
    return a


def main(argv=None):
    args = parse_args(argv)
    import urchin_hw as hw

    run_dir = make_run_dir(args.run)
    started = datetime.datetime.now()
    gpio = hw.open_gpio()
    motor = hw.Motor(gpio)
    dev = hw.I2CDevice(hw.ADS1115_ADDR)
    adc = hw.ADS1115(dev, fsr=args.fsr, rate=args.rate)
    sampler = Sampler(adc, args.fsr, args.r_iset, args.vbus_divider)
    samples, strokes, zeros = [], [], {}
    try:
        samples, strokes, zeros = run_wipes(motor, sampler, args)
    except ZeroError as e:
        print(f"stopped before the first wipe: {e}", file=sys.stderr)
        os.rmdir(run_dir)  # nothing was logged
        sys.exit(2)
    finally:
        motor.sleep()
        try:
            adc.stop()
        finally:
            dev.close()
            gpio.close()

    span = samples[-1]["t_s"] - samples[0]["t_s"] if len(samples) > 1 else 0
    achieved = (len(samples) - 1) / span if span else None
    summary = wa.summarize(samples, strokes, args.vbus_nominal, args.inrush_ms / 1000.0,
                           args.noload_amps, args.stall_ref_amps)
    summary["visen_zero_mv"] = {when: ({k: round(v * 1000, 3) for k, v in z.items()} if z else None)
                                for when, z in zeros.items()} if args.zero == "auto" else "off"
    summary["i2c_retries"] = dev.retries
    summary = write_outputs(run_dir, samples, summary, args, achieved, started)
    e = summary["energy_per_wipe_j"]
    print(f"run folder: {run_dir}")
    print(f"samples: {len(samples)} at {achieved or 0:.0f} S/s; vbus {summary['vbus']}; "
          f"clipped {summary['clipped_samples']}; I2C retries {dev.retries}")
    print(f"VISEN zero (mV): {summary['visen_zero_mv']}")
    if e:
        print(f"energy per wipe: median {e['median']:.3f} J (min {e['min']:.3f}, max {e['max']:.3f}, n={e['n']})")
    if summary["stall_amps"]:
        print(f"stall current: median {summary['stall_amps']['median']:.3f} A (n={summary['stall_amps']['n']})")
    if summary["torque_margin"] is not None:
        print(f"torque margin estimate: {summary['torque_margin']:.2f}x")
    print(f"plot: python3 {os.path.join(os.path.dirname(__file__), 'plot_run.py')} {run_dir}")


if __name__ == "__main__":
    main()
