"""Shaft angle from the rotary pot on AIN2 (TP6): wiper = white, ends green -> TP3 (+3V3) and blue -> TP2 (GND).

cam02, 11/13 (~/NereusData/UrchinCam/2026-11-13_pot_cal/): the pot reads 0 to 3.35 V and turns all the way round,
jumping from 3.35 V back to 0 at the end of its track. 11 hand turns of ~90 deg gave a median 0.916 V
(0.82-1.07 V), so DEG_PER_V = 98.3 (about +-10 %), and the track covers ~330 deg; the ~30 deg rest is a gap, inferred
on the assumption that a full turn is 360 deg. Readings in the gap are not trustworthy.

Unwrapping: a step of more than half a turn between two readings is taken as crossing the gap, so the angle keeps
counting through it instead of jumping by a full turn.

The dead zone (cam02 11/15, ~/NereusData/UrchinCam/2026-11-15_jog_*): past 3.35 V the wiper reads exactly 0 V (on the
end terminal) or floats near 0.57 V (touching nothing; unused ADC inputs float at ~0.57 V too). Both look like real
track positions, so a reading that jumps further than the shaft can turn between two reads is held as unreliable
(`ok` False) until three readings in a row agree again. Moves refuse targets outside SAFE_V.

Away from the track ends, three readings that form a possible path (each step no faster than MAX_DPS) are also
trusted again, so a move doesn't stop when the control loop stalls (cam02 11/15: a 51 ms gap, the shaft turned 14 deg
in it). Callers stamp readings with when the sample was taken (`t`), not when Python got to it.
"""

import json
import os

POT_CHANNEL = 2
POT_FSR = 4.096  # the pot spans 0-3.35 V
DEFAULT_CAL = {"deg_per_v": 98.3, "sign": 1, "zero_v": None,
               "source": "cam02 11/13: 11 hand turns of ~90 deg, median 0.916 V (0.82-1.07 V), about +-10 %"}
CAL_PATH = os.path.expanduser("~/NereusData/UrchinCam/pot_cal.json")
SAFE_V = (0.10, 3.25)  # moves stay inside this part of the track (it reads 0-3.35 V; the dead zone is past both ends)
MAX_DPS = 600.0  # the shaft can't turn faster than this (cam02 full speed ~325 deg/s); a faster jump is a bad reading
AGREE_V = 0.03  # readings agreeing within this, three in a row, are trusted again after a bad one...
END_V = 0.35  # ...but after leaving the track at one end, only near an end: the floating ~0.57 V is steady too
TRACK_V = 3.35


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
        self.last_t = None
        self.angle = None
        self.ok = True  # False while readings are unreliable (dead zone)
        self.suspect = []  # (volts, t) of readings since the last trusted one

    @property
    def period_v(self):
        return 360.0 / self.cal["deg_per_v"]  # volts one full turn would span if the track had no gap

    def _turn_step(self, v):
        step = v - self.last_v
        return 1 if step < -self.period_v / 2 else -1 if step > self.period_v / 2 else 0

    def update(self, v, t=None):
        """`t` (seconds) enables the plausibility check; without it every reading is trusted (desk tests)."""
        if self.last_v is not None and t is not None and self.last_t is not None:
            dt = max(t - self.last_t, 0.005)
            k = self._turn_step(v)
            jump_deg = abs(v + k * self.period_v - self.last_v) * self.cal["deg_per_v"]
            # once unreliable, stay so until re-trusted below (a long stay would make any jump look "plausible")
            if not self.ok or jump_deg > MAX_DPS * dt + 3.0:
                self.suspect.append((v, t))
                near_end = lambda x: x < END_V or x > TRACK_V - END_V
                last3 = [x for x, _ in self.suspect[-3:]]
                agree = len(self.suspect) >= 3 and max(last3) - min(last3) <= AGREE_V
                path = len(self.suspect) >= 3 and all(
                    abs(self.suspect[i][0] - self.suspect[i - 1][0]) * self.cal["deg_per_v"]
                    <= MAX_DPS * max(self.suspect[i][1] - self.suspect[i - 1][1], 0.005) + 3.0
                    for i in (-2, -1)) and not any(near_end(x) for x in last3)
                if (agree and (near_end(v) or not near_end(self.last_v))) or (path and not near_end(self.last_v)):
                    self.suspect = []  # out of the dead zone, settled after a fast hand turn, or a stall: trust it
                else:
                    self.ok = False
                    return self.angle  # hold the last good angle
            else:
                self.suspect = []
        self.ok = True
        if self.last_v is not None:
            self.turns += self._turn_step(v)
        self.last_v, self.last_t = v, t
        if self.cal["zero_v"] is None:  # first reading after a fresh install: call it 0 deg
            self.cal["zero_v"] = v
        u = v + self.turns * self.period_v
        self.angle = self.cal["sign"] * (u - self.cal["zero_v"]) * self.cal["deg_per_v"]
        return self.angle

    def room_deg(self):
        """Degrees the angle can still go up and down before leaving SAFE_V (None until there's a reading)."""
        if self.last_v is None:
            return None
        up_v, down_v = SAFE_V[1] - self.last_v, self.last_v - SAFE_V[0]  # pot volts left either way
        k = self.cal["deg_per_v"]
        if self.cal["sign"] > 0:
            return {"plus": round(up_v * k, 1), "minus": round(down_v * k, 1)}
        return {"plus": round(down_v * k, 1), "minus": round(up_v * k, 1)}

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
