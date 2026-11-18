"""Shaft angle from the rotary pot on AIN2 (TP6): wiper = white, ends green -> TP3 (+3V3) and blue -> TP2 (GND).

cam02, 11/13 (~/NereusData/UrchinCam/2026-11-13_pot_cal/): the pot reads 0 to 3.35 V and turns all the way round,
jumping from 3.35 V back to 0 at the end of its track. 11 hand turns of ~90 deg gave a median 0.916 V
(0.82-1.07 V), so DEG_PER_V = 98.3 (about +-10 %), and the track covers ~330 deg; the ~30 deg rest is a gap, inferred
on the assumption that a full turn is 360 deg. Readings in the gap are not trustworthy.

Trust (PE 018's ruling 1, ~/NereusData/ops/pe_specs/RULINGS_urchincam_pr1_2026-11-15.md @ 09a395d1c56a). In the ~30 deg
gap past the track the wiper touches nothing and floats anywhere from 0.08 to 1.22 V (QE 019, cam02 11/15 jog_194426),
overlapping real track voltages, so no reading can show the arm is out of the gap. What a reading can show is that the
arm had no time to reach it. Three states; `ok` is True only in "trusted":
  - trusted: each reading keeps trust only if the gap is out of reach (reach = MAX_DPS x time since the last trusted
    reading + REACH_PAD_DEG, less than that reading's distance to the nearer track end), the reading is inside SAFE_V
    and its step from the last trusted reading is within reach;
  - suspect: the gap is out of reach but the reading fails the step or SAFE_V test: dropped, the last trusted reading
    stays; a later reading that passes makes the pot trusted again (no other path does);
  - latched: any reading that arrives with the gap within reach, and every start. Only the operator's confirm
    (`confirm`) leaves it: the arm still (readings across the last CONFIRM_S, none more than CONFIRM_GAP_S apart or
    older than that at the confirm, all within CONFIRM_BAND_V) and inside SAFE_V.
Limits (each pinned by a test): the confirm is the operator's word, so one given with the arm in the gap while the float
sits still inside SAFE_V is accepted; and the proof assumes the arm never turns faster than MAX_DPS (a faster turn by
hand can carry it into the gap unseen). No turn counting: the angle never crosses the gap.
Callers stamp readings with when the sample was taken (`t`), not when Python got to it.
"""

import collections
import json
import os

POT_CHANNEL = 2
POT_FSR = 4.096  # the pot spans 0-3.35 V
DEFAULT_CAL = {"deg_per_v": 98.3, "sign": 1, "zero_v": None,
               "source": "cam02 11/13: 11 hand turns of ~90 deg, median 0.916 V (0.82-1.07 V), about +-10 %"}
CAL_PATH = os.path.expanduser("~/NereusData/UrchinCam/pot_cal.json")
SAFE_V = (0.10, 3.25)  # trusted readings and move targets stay inside this part of the track (it reads 0-3.35 V)
TRACK_V = 3.35  # the track's top end; the bottom end is 0 V. The gap is past both.
MAX_DPS = 600.0  # the shaft can't turn faster than this (cam02 full speed ~325 deg/s): the reach of the proof
REACH_PAD_DEG = 3.0  # added to the reach (reading noise and timing)
CONFIRM_S = 0.3  # the operator's confirm needs every reading of this long still...
CONFIRM_BAND_V = 0.005  # ...within this (= jog_server.RAW_MOVE_V; PE 018's reference numbers for the confirm)
CONFIRM_GAP_S = 0.1  # ...with no two readings in that window further apart than this, and the newest no older (QE 019
# @ 536b720, L2: one reading 10 s old plus one new one passed). cam02 reads the idle pot every ~19 ms (median, 11/15
# jog_194426); a rare slow read (up to 150 ms on 11/15) refuses the confirm and the operator presses again.
NEXT = {
    "trusted": "",
    "suspect": "a reading was dropped as impossible; the last good angle is held",
    "latched": "angle unknown (the arm may be in the pot's gap): turn the arm by hand onto the track, well away from "
               "both ends, hold it still, then press 'The arm is on the track'",
}


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
        self.last_v = None  # last trusted reading
        self.last_t = None
        self.angle = None  # from the last trusted reading
        self.state = "latched"  # every start is latched (ruling 2)
        self.why = "server started"
        self.recent = collections.deque(maxlen=400)  # (t, volts) of every reading, for the confirm
        self.raw_v = None  # latest reading, trusted or not

    @property
    def ok(self):
        return self.state == "trusted"

    def reach_deg(self, t):
        return MAX_DPS * max(t - self.last_t, 0.0) + REACH_PAD_DEG

    def end_deg(self):
        """Distance from the last trusted reading to the nearer track end."""
        return min(self.last_v, TRACK_V - self.last_v) * self.cal["deg_per_v"]

    def update(self, v, t):
        self.raw_v = v
        self.recent.append((t, v))
        if self.state == "latched":
            return self.angle
        reach = self.reach_deg(t)
        if reach >= self.end_deg():
            self.state, self.why = "latched", f"the gap was within reach ({reach:.0f}°) at {v:.3f} V"
            return self.angle
        step = abs(v - self.last_v) * self.cal["deg_per_v"]
        if SAFE_V[0] <= v <= SAFE_V[1] and step <= reach:
            self.state, self.why = "trusted", ""
            self._trust(v, t)
        else:
            self.state = "suspect"
            self.why = (f"{v:.3f} V is outside the safe track" if not SAFE_V[0] <= v <= SAFE_V[1]
                        else f"a {step:.0f}° step in {max(t - self.last_t, 0.0) * 1000:.0f} ms")
        return self.angle

    def _trust(self, v, t):
        self.last_v, self.last_t = v, t
        if self.cal["zero_v"] is None:  # first trusted reading after a fresh install: call it 0 deg
            self.cal["zero_v"] = v
        self.angle = self.cal["sign"] * (v - self.cal["zero_v"]) * self.cal["deg_per_v"]

    def confirm(self, now):
        """The operator's word that the arm is on the track: latched or suspect -> trusted. Keeps the zero.
        Raises ValueError with the reason when refused."""
        if self.ok:
            return
        if not self.recent or now - self.recent[-1][0] > CONFIRM_GAP_S:
            raise ValueError("no recent pot reading")
        t, v = self.recent[-1]
        if not SAFE_V[0] <= v <= SAFE_V[1]:
            raise ValueError(f"the pot reads {v:.3f} V, outside the safe track ({SAFE_V[0]}-{SAFE_V[1]} V): turn "
                             f"the arm further onto the track")
        pts = list(self.recent)
        since = [i for i, (tt, _) in enumerate(pts) if tt <= now - CONFIRM_S]
        if not since:
            raise ValueError(f"not enough readings yet: hold the arm still for {CONFIRM_S:g} s")
        win = pts[since[-1]:]  # the last reading at or before the window's start, then every one in it
        gap = max(b[0] - a[0] for a, b in zip(win, win[1:]))  # >= 2 readings: the newest is < CONFIRM_GAP_S old
        if gap > CONFIRM_GAP_S:
            raise ValueError(f"pot readings {gap * 1000:.0f} ms apart in the last {CONFIRM_S:g} s: hold the arm still "
                             f"and press again")
        vals = [x for _, x in win]
        if max(vals) - min(vals) > CONFIRM_BAND_V:
            raise ValueError(f"the reading is moving ({(max(vals) - min(vals)) * 1000:.0f} mV in {CONFIRM_S:g} s): "
                             f"hold the arm still")
        fresh = self.cal["zero_v"] is None
        self.state, self.why = "trusted", ""
        self._trust(v, t)
        if fresh:  # a fresh install's first trusted reading became the zero: keep it (QE 019 @ 536b720)
            self._save()

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
        if not self.ok:
            raise ValueError("the pot is not trusted: confirm the arm is on the track first")
        self.cal["zero_v"] = self.last_v
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
