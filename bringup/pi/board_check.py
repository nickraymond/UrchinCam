#!/usr/bin/env python3
"""Bring-up checks for the UrchinCam expander board (test card stages 3, 4 and 6).

    python3 board_check.py i2c          # 3.1: expect 0x48 (ADS1115) and 0x4A or 0x4B (BNO085), nothing else
    python3 board_check.py ads          # 3.2: AIN0-AIN3 single-shot at +-4.096 V, bridge asleep
    python3 board_check.py leds         # 4.1: each LED color for 1 s
    python3 board_check.py motor-open   # 6.x: steps the MP6550 truth table with J3 open, prints VISEN

Each prints PASS / FAIL lines against the test card's bars and exits non-zero on any FAIL.
"""

import argparse
import shutil
import subprocess
import sys
import time

import urchin_hw as hw

EXPECTED_ADS = 0x48
BNO085_CHOICES = (0x4A, 0x4B)


def parse_i2cdetect(text):
    """Addresses from `i2cdetect -y 1` output (hex cells; 'UU' means a kernel driver holds it)."""
    found = []
    for line in text.splitlines()[1:]:
        row, _, cells = line.partition(":")
        if not cells:
            continue
        base = int(row.strip(), 16)
        for col, cell in enumerate(cells.split()):
            if cell not in ("--", ""):
                found.append(base + col)
    return found


def judge_i2c(found):
    ok = EXPECTED_ADS in found
    bno = [a for a in found if a in BNO085_CHOICES]
    extra = [a for a in found if a != EXPECTED_ADS and a not in BNO085_CHOICES]
    lines = [
        f"{'PASS' if ok else 'FAIL'} ADS1115 at 0x48",
        f"{'PASS' if len(bno) == 1 else 'FAIL'} BNO085 at {', '.join(hex(a) for a in bno) or 'neither 0x4a nor 0x4b'}",
        f"{'PASS' if not extra else 'FAIL'} no other devices{': ' + ', '.join(hex(a) for a in extra) if extra else ''}",
    ]
    return ok and len(bno) == 1 and not extra, lines


def cmd_i2c(_):
    if not shutil.which("i2cdetect"):
        print("FAIL i2cdetect not found (package i2c-tools); is I2C enabled?")
        return False
    out = subprocess.run(["i2cdetect", "-y", str(hw.I2C_BUS)], capture_output=True, text=True, check=True).stdout
    print(out)
    ok, lines = judge_i2c(parse_i2cdetect(out))
    print("\n".join(lines))
    return ok


def cmd_ads(_):
    dev = hw.I2CDevice(hw.ADS1115_ADDR)
    adc = hw.ADS1115(dev, rate=128)
    try:
        ok = True
        for ch in range(4):
            raw, v = adc.single_shot(ch, 4.096)
            label = "VISEN" if ch == 0 else f"TP{4 + ch}"
            line = f"AIN{ch} ({label}): {v * 1000:8.2f} mV  raw 0x{raw:04x}"
            if ch == 0:
                good = abs(v) < 0.005
                ok &= good
                line = f"{'PASS' if good else 'FAIL'} {line} (bar < 5 mV)"
            print(line)
        return ok
    finally:
        dev.close()


def cmd_leds(args):
    gpio = hw.open_gpio()
    try:
        for pin, _ in hw.LEDS:
            gpio.output(pin, 1)  # high = off (common anode)
        for pin, label in hw.LEDS:
            print(f"{label} (GPIO{pin}) on")
            gpio.output(pin, 0)
            time.sleep(args.seconds)
            gpio.output(pin, 1)
        print("Nick: PASS only if each of the 6 colors lit alone, in that order.")
        return True
    finally:
        gpio.close()


def visen_mv(adc):
    _, v = adc.single_shot(0, 0.256)
    return v * 1000


def cmd_motor_open(args):
    """J3 must be open (no motor). Nick reads the DMM across J3.1 (red) and J3.3 (black) in each state."""
    gpio = hw.open_gpio()
    motor = hw.Motor(gpio)
    dev = hw.I2CDevice(hw.ADS1115_ADDR)
    adc = hw.ADS1115(dev, rate=128)
    states = [
        ("6.1 asleep (nSLEEP_HB low)", motor.sleep, "~0 V, outputs floating"),
        ("6.2 forward (IN1=1, IN2=0)", motor.forward, "+4.7 to +5.1 V"),
        ("6.3 reverse (IN1=0, IN2=1)", motor.reverse, "-4.7 to -5.1 V"),
        ("6.4 brake (IN1=1, IN2=1)", motor.brake, "~0 V"),
    ]
    ok = True
    try:
        for label, action, expect in states:
            if action is not motor.sleep:
                motor.wake()
            action()
            time.sleep(args.seconds)
            mv = visen_mv(adc)
            good = abs(mv) < 2.0
            ok &= good
            print(f"{label}: DMM should read {expect}; {'PASS' if good else 'FAIL'} VISEN {mv:.2f} mV (bar < 2 mV)")
        return ok
    finally:
        motor.sleep()
        dev.close()
        gpio.close()


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("i2c").set_defaults(fn=cmd_i2c)
    sub.add_parser("ads").set_defaults(fn=cmd_ads)
    leds = sub.add_parser("leds")
    leds.add_argument("--seconds", type=float, default=1.0)
    leds.set_defaults(fn=cmd_leds)
    mo = sub.add_parser("motor-open")
    mo.add_argument("--seconds", type=float, default=2.0)
    mo.set_defaults(fn=cmd_motor_open)
    args = p.parse_args(argv)
    sys.exit(0 if args.fn(args) else 1)


if __name__ == "__main__":
    main()
