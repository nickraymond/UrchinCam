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
import subprocess
import sys
import time

import wipe_analysis as wa
from urchin_hw import raw_to_volts

DATA_ROOT = os.path.expanduser("~/NereusData/UrchinCam")
MAX_STROKE_S = 10.0
VBUS_FSR = 4.096


class Sampler:
    """Reads VISEN (AIN0) and, if a divider is fitted, +5V on AIN1."""

    def __init__(self, adc, fsr, r_iset, vbus_divider=None, vbus_every=4):
        self.adc, self.fsr, self.r_iset = adc, fsr, r_iset
        self.vbus_divider, self.vbus_every = vbus_divider, vbus_every
        self.k = 0
        self.vbus = None

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
        return {
            "raw": raw,
            "visen_v": visen,
            "amps": wa.visen_to_amps(visen, self.r_iset),
            "vbus_v": self.vbus,
            "vbus_fresh": fresh,
            "clipped": raw == 0x7FFF,
        }


def run_wipes(motor, sampler, args, clock=time.monotonic, sleep=time.sleep):
    """Returns (samples, strokes_meta). Leaves the motor braked then coasting; the caller puts it to sleep."""
    period = 1.0 / args.rate
    samples, strokes = [], []
    t0 = clock()
    stalls = 0

    def log_for(phase, wipe, seconds, detector=None):
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

    sampler.begin()
    motor.wake()
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
                    return samples, strokes
                break  # skip the rest of this wipe
    return samples, strokes


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
    samples, strokes = [], []
    try:
        samples, strokes = run_wipes(motor, sampler, args)
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
    summary = write_outputs(run_dir, samples, summary, args, achieved, started)
    e = summary["energy_per_wipe_j"]
    print(f"run folder: {run_dir}")
    print(f"samples: {len(samples)} at {achieved or 0:.0f} S/s; vbus {summary['vbus']}; "
          f"clipped {summary['clipped_samples']}")
    if e:
        print(f"energy per wipe: median {e['median']:.3f} J (min {e['min']:.3f}, max {e['max']:.3f}, n={e['n']})")
    if summary["stall_amps"]:
        print(f"stall current: median {summary['stall_amps']['median']:.3f} A (n={summary['stall_amps']['n']})")
    if summary["torque_margin"] is not None:
        print(f"torque margin estimate: {summary['torque_margin']:.2f}x")
    print(f"plot: python3 {os.path.join(os.path.dirname(__file__), 'plot_run.py')} {run_dir}")


if __name__ == "__main__":
    main()
