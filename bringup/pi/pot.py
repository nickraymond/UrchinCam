"""Shaft angle from the rotary pot on AIN2 (TP6): wiper = white, ends green -> TP3 (+3V3) and blue -> TP2 (GND).

cam02, 11/13 (~/NereusData/UrchinCam/2026-11-13_pot_cal/): the pot reads 0 to 3.35 V and turns all the way round,
jumping from 3.35 V back to 0 at the end of its track. 11 hand turns of ~90 deg gave a median 0.916 V
(0.82-1.07 V), so DEG_PER_V = 98.3 (about +-10 %), and the track covers ~330 deg; the ~30 deg rest is a gap, inferred
on the assumption that a full turn is 360 deg. Readings in the gap are not trustworthy.

Unwrapping: a step of more than half a turn between two readings is taken as crossing the gap, so the angle keeps
counting through it instead of jumping by a full turn.
"""

import json
import os

POT_CHANNEL = 2
POT_FSR = 4.096  # the pot spans 0-3.35 V
DEFAULT_CAL = {"deg_per_v": 98.3, "sign": 1, "zero_v": None,
               "source": "cam02 11/13: 11 hand turns of ~90 deg, median 0.916 V (0.82-1.07 V), about +-10 %"}
CAL_PATH = os.path.expanduser("~/NereusData/UrchinCam/pot_cal.json")


def load_cal(path=CAL_PATH):
    cal = dict(DEFAULT_CAL)
    try:
        with open(path) as f:
            cal.update(json.load(f))
    except (OSError, ValueError):
        pass
    return cal


def save_cal(cal, path=CAL_PATH):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cal, f, indent=2)
    os.replace(tmp, path)


class PotAngle:
    def __init__(self, cal=None, path=None):
        self.cal = dict(cal or DEFAULT_CAL)
        self.path = path
        self.turns = 0  # full turns counted across the gap since the last zero
        self.last_v = None
        self.angle = None

    @property
    def period_v(self):
        return 360.0 / self.cal["deg_per_v"]  # volts one full turn would span if the track had no gap

    def update(self, v):
        if self.last_v is not None:
            step = v - self.last_v
            if step < -self.period_v / 2:
                self.turns += 1
            elif step > self.period_v / 2:
                self.turns -= 1
        self.last_v = v
        if self.cal["zero_v"] is None:  # first reading after a fresh install: call it 0 deg
            self.cal["zero_v"] = v
        u = v + self.turns * self.period_v
        self.angle = self.cal["sign"] * (u - self.cal["zero_v"]) * self.cal["deg_per_v"]
        return self.angle

    def zero_here(self):
        if self.last_v is not None:
            self.cal["zero_v"] = self.last_v
            self.turns = 0
            self.angle = 0.0
        self._save()
        return dict(self.cal)

    def flip(self):
        self.cal["sign"] = -self.cal["sign"]
        if self.angle is not None:
            self.angle = -self.angle
        self._save()
        return dict(self.cal)

    def _save(self):
        if self.path:
            save_cal(self.cal, self.path)
