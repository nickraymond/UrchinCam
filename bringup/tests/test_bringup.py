"""Desk tests for bringup/pi (no hardware). Run from the repo root:
    python3 -m unittest discover -s bringup/tests -v
"""

import csv
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pi"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

import bench_runs  # noqa: E402
import board_check  # noqa: E402
import jog_server  # noqa: E402
import plot_run  # noqa: E402
import pot  # noqa: E402
import urchin_hw as hw  # noqa: E402
import wipe_analysis as wa  # noqa: E402
import wipe_logger  # noqa: E402
import wipe_repeat  # noqa: E402


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += max(0.0, s)


class FakeGpio:
    def __init__(self):
        self.levels = {}
        self.log = []

    def output(self, pin, level):
        self.levels[pin] = level
        self.log.append((pin, level))

    def pwm(self, pin, duty, freq=None):
        self.levels[pin] = duty if 0 < duty < 1 else int(duty >= 1)
        self.log.append((pin, ("pwm", duty)))

    def close(self):
        pass


class SimMotorADC:
    """Continuous-mode ADS1115 whose AIN0 follows the motor state: 60 mA running, `stall_a` while jammed."""

    def __init__(self, gpio, clock, fsr=0.256, run_a=0.060, stall_a=0.30, jam=(), offset_v=(0.0, 0.0)):
        self.gpio, self.clock, self.fsr = gpio, clock, fsr
        self.run_a, self.stall_a, self.jam = run_a, stall_a, set(jam)
        # No-load VISEN when awake, (fwd, rev): cam02 read ~4.4 mV, but ~5.5 mV in reverse and in a brake entered
        # straight from reverse; coast or forward resets it. Track that from the GPIO writes.
        self.offset_v = offset_v
        self.rev_latched = False
        write = gpio.output

        def output(pin, level):
            write(pin, level)
            a, b = gpio.levels.get(hw.GPIO_IN1, 0), gpio.levels.get(hw.GPIO_IN2, 0)
            if a == 0 and b > 0:
                self.rev_latched = True
            elif b == 0:
                self.rev_latched = False
        gpio.output = output
        self.wipe_phase = None

    # A shaft pot on AIN2 that turns with the motor at pot_vps volts per second. Like cam02's (11/13, 11/15): the track
    # reads 0-3.35 V over ~330 deg; in the ~30 deg dead zone after it (360/98.3 = 3.662 V of travel per turn) the
    # wiper first floats (~0.57 V, like an unused input), then sits on the 0 V end terminal. The float wanders read to
    # read as recorded on cam02 (11/15 jog_194426: t=908.82-908.88 after leaving the track, then t=1040.96 driving
    # reverse in the gap): it changes by more than 5 mV most reads and rises 28 mV one way at the start (QE 019, N2/N3).
    pot_u, pot_vps, pot_last_t, float_i = 1.5, 0.5, None, 0
    pot_jitter, jitter_i = (), 0  # volts added to on-track readings in turn (ADC noise on a still shaft)
    POT_TURN_V, POT_TRACK_V = 360 / 98.3, 3.35
    FLOAT_SEQ = (0.59113, 0.59788, 0.61850, 0.59325, 0.57388, 0.54575, 0.58013, 0.55250, 0.56300, 0.57150, 0.57488,
                 0.56712, 0.56463, 0.57400, 0.57450, 0.55825, 0.56575, 0.58213, 0.57437, 0.56450, 0.57163, 0.54487,
                 0.58650, 0.56788, 0.57900)

    @property
    def pot_v(self):
        v = self.pot_u % self.POT_TURN_V
        if v <= self.POT_TRACK_V:
            if self.pot_jitter:
                self.jitter_i += 1
                return v + self.pot_jitter[self.jitter_i % len(self.pot_jitter)]
            return v
        if v >= self.POT_TRACK_V + 0.2:
            return 0.0
        self.float_i += 1  # each read of the floating wiper gets the next recorded value
        return self.FLOAT_SEQ[self.float_i % len(self.FLOAT_SEQ)]

    cont_ch = 0

    def start_continuous(self, channel, fsr=None):
        self.cont_ch = channel  # 0 = current (VISEN), 2 = pot, like the real ADC's mux

    # Shaft inertia: speed follows the drive with these time constants (driving, coasting, braking).
    TAU_DRIVE, TAU_COAST, TAU_BRAKE = 0.03, 0.06, 0.012
    shaft_vps = 0.0

    def _turn_pot(self):
        import math
        now = self.clock()  # FakeClock in tests; real time in the desk sim
        if self.pot_last_t is not None:
            dt = now - self.pot_last_t
            g = self.gpio.levels
            a, b = g.get(hw.GPIO_IN1, 0), g.get(hw.GPIO_IN2, 0)
            awake = g.get(hw.GPIO_NSLEEP_HB)
            d = (a - b) if awake else 0  # +1 forward, -1 reverse, PWM duty in between
            tau = self.TAU_BRAKE if awake and a and b == a and a >= 1 else self.TAU_DRIVE if d else self.TAU_COAST
            self.shaft_vps += (d * self.pot_vps - self.shaft_vps) * (1 - math.exp(-dt / tau))
            self.pot_u += self.shaft_vps * dt
        self.pot_last_t = now

    def single_shot(self, channel, fsr):
        self.clock.t += 0.0015  # one conversion at 860 SPS
        self._turn_pot()
        if channel == 0:
            raw = self._visen_raw()
            return raw, hw.raw_to_volts(raw, self.fsr)
        if channel != 2:
            raise ValueError("sim has VISEN on AIN0 and the pot on AIN2")
        v = self.pot_v
        return int(v / fsr * 32768), v

    def read_raw(self):
        self.clock.t += 0.0003  # an I2C read takes time
        self._turn_pot()
        if self.cont_ch == 2:
            return int(self.pot_v / 4.096 * 32768)
        return self._visen_raw()

    def _visen_raw(self):
        g = self.gpio.levels
        if not g.get(hw.GPIO_NSLEEP_HB):
            amps = 0.0
        else:
            a, b = g.get(hw.GPIO_IN1, 0), g.get(hw.GPIO_IN2, 0)
            driving = a != b
            amps = (self.stall_a if self.wipe_phase in self.jam else self.run_a * abs(a - b)) if driving else 0.0
        visen = amps * wa.ISET_GAIN_A_PER_A * 2000.0
        if g.get(hw.GPIO_NSLEEP_HB):
            visen += self.offset_v[1 if self.rev_latched else 0]
        raw = int(round(visen / self.fsr * 32768))
        return min(raw, 0x7FFF) & 0xFFFF


def args(**kw):
    base = dict(wipes=3, fwd_s=0.5, rev_s=0.5, brake_s=0.1, pause_s=0.1, stall_amps=0.25, stall_ms=150.0,
                max_stalls=1, rate=860, fsr=0.256, r_iset=2000.0, zero="auto")
    base.update(kw)
    return SimpleNamespace(**base)


class TrackingSampler(wipe_logger.Sampler):
    """Tells the simulated ADC which wipe/phase is running so it can jam one stroke."""

    def __init__(self, adc, runner_state, **kw):
        super().__init__(adc, 0.256, 2000.0, **kw)
        self.state = runner_state

    def take(self):
        self.adc.wipe_phase = self.state.get("current")
        return super().take()


class TestAdsEncoding(unittest.TestCase):
    def test_continuous_ain0_0256_860(self):
        # MUX=100b AIN0/GND, PGA=101b, MODE=0, DR=111b, COMP_QUE=11b (SBAS444E Table 8-3)
        self.assertEqual(hw.ads_config_word(0, 0.256, 860, single_shot=False), 0x4AE3)

    def test_single_shot_ain1_4096_860(self):
        self.assertEqual(hw.ads_config_word(1, 4.096, 860, single_shot=True), 0xD3E3)

    def test_bad_channel(self):
        with self.assertRaises(ValueError):
            hw.ads_config_word(4, 0.256, 860, True)

    def test_raw_to_volts(self):
        self.assertAlmostEqual(hw.raw_to_volts(0x8000, 0.256), -0.256)
        self.assertAlmostEqual(hw.raw_to_volts(0x4000, 0.256), 0.128)
        self.assertAlmostEqual(hw.raw_to_volts(0xFFFF, 1.024), -1.024 / 32768)


class ContinuousADSDev:
    """ADS1115 that behaves like cam02's (11/13): in continuous mode a config write with OS=1 only changes the
    settings; a conversion starts only on an OS=1 write while already in power-down single-shot mode."""

    def __init__(self, ain):
        self.ain, self.cfg, self.ptr = ain, 0, 0
        self.continuous, self.conv = False, 0

    def _convert(self):
        mux, pga = (self.cfg >> 12) & 0b111, (self.cfg >> 9) & 0b111
        fsr = {v: k for k, v in hw.PGA_BITS.items()}[pga]
        self.conv = int(round(self.ain[mux - 0b100] / fsr * 32768)) & 0xFFFF

    def write(self, data):
        self.ptr = data[0]
        if len(data) == 3 and data[0] == hw.REG_CONFIG:
            word = (data[1] << 8) | data[2]
            was_powered_down = not self.continuous
            self.cfg = word
            self.continuous = not (word >> 8) & 1
            if self.continuous or (was_powered_down and word & 0x8000):
                self._convert()

    def read(self, n):
        v = (self.cfg | 0x8000) if self.ptr == hw.REG_CONFIG else self.conv
        return [v >> 8, v & 0xFF]


class TestSingleShotAfterContinuous(unittest.TestCase):
    def test_pot_read_after_continuous_visen(self):
        dev = ContinuousADSDev([0.0366, 0.57, 3.26, 0.57])  # AIN0 = floating VISEN, AIN2 = pot
        adc = hw.ADS1115(dev, fsr=1.024, rate=860)
        adc.start_continuous(0, 1.024)
        _, v = adc.single_shot(2, 4.096)
        self.assertAlmostEqual(v, 3.26, delta=0.001)  # not 0.146 (= 36.6 mV read at the 4.096 V scale)
        adc.start_continuous(0, 1.024)
        self.assertAlmostEqual(hw.raw_to_volts(adc.read_raw(), 1.024), 0.0366, delta=0.0001)


class FakeLgpio:
    """lgpio as cam02's behaves for tx_pwm: calls queue per GPIO, an endless entry never ends, so only a stop
    (frequency 0) clears the queue. `level(pin)` is what the pin drives now (duty for PWM)."""
    TX_PWM = 0

    def __init__(self):
        self.claimed, self.written, self.queue = set(), {}, {}

    def gpiochip_open(self, chip):
        return 7

    def gpio_claim_output(self, h, pin, level):
        if pin in self.claimed:
            return -1
        self.claimed.add(pin)
        self.written[pin] = level
        return 0

    def gpio_write(self, h, pin, level):
        self.written[pin] = level
        return 0

    def tx_pwm(self, h, pin, freq, duty):
        if freq == 0:
            self.queue[pin] = []
        else:
            self.queue.setdefault(pin, []).append(duty)
        return 0

    def tx_busy(self, h, pin, kind):
        return 1 if self.queue.get(pin) else 0

    def gpio_free(self, h, pin):
        self.claimed.discard(pin)

    def gpiochip_close(self, h):
        pass

    def level(self, pin):
        q = self.queue.get(pin)
        return q[0] / 100 if q else self.written.get(pin)


class TestLgpioBackend(unittest.TestCase):
    def _backend(self):
        b = hw.LgpioBackend.__new__(hw.LgpioBackend)
        b.lg, b.h, b.claimed, b.pwm_pins = FakeLgpio(), 7, set(), {}
        return b

    def test_duty_changes_take_effect(self):
        b = self._backend()
        m = hw.Motor(b)
        m.wake()
        m.forward(1.0)
        self.assertEqual(b.lg.level(hw.GPIO_IN1), 1)
        for duty in (0.7, 0.6, 0.8, 0.75):  # repeated changes must not queue behind the first endless entry
            m.forward(duty)
            self.assertAlmostEqual(b.lg.level(hw.GPIO_IN1), duty)
        m.brake()
        self.assertEqual((b.lg.level(hw.GPIO_IN1), b.lg.level(hw.GPIO_IN2)), (1, 1))
        m.reverse(0.6)
        self.assertEqual(b.lg.level(hw.GPIO_IN1), 0)
        self.assertAlmostEqual(b.lg.level(hw.GPIO_IN2), 0.6)
        m.sleep()
        self.assertEqual([b.lg.level(p) for p in (hw.GPIO_IN1, hw.GPIO_IN2, hw.GPIO_NSLEEP_HB)], [0, 0, 0])

    def test_pins_claimed_once_and_freed(self):
        b = self._backend()
        m = hw.Motor(b)
        for _ in range(5):
            m.wake(); m.forward(0.5); m.brake(); m.sleep()
        self.assertEqual(b.lg.claimed, {hw.GPIO_IN1, hw.GPIO_IN2, hw.GPIO_NSLEEP_HB})
        b.close()
        self.assertEqual(b.lg.claimed, set())


class TestScale(unittest.TestCase):
    def test_visen_scale_2k(self):
        self.assertAlmostEqual(wa.visen_to_amps(0.2), 1.0)
        self.assertAlmostEqual(wa.visen_to_amps(0.5), 2.5)  # trip voltage -> 2.5 A typ
        self.assertAlmostEqual(wa.amps_per_volt(10000.0), 1.0)


class TestStallDetector(unittest.TestCase):
    def test_needs_full_window(self):
        d = wa.StallDetector(0.25, 150)
        self.assertFalse(d.update(0.00, 0.30))
        self.assertFalse(d.update(0.10, 0.30))
        self.assertTrue(d.update(0.15, 0.30))

    def test_dip_resets(self):
        d = wa.StallDetector(0.25, 150)
        d.update(0.00, 0.30)
        d.update(0.10, 0.10)
        self.assertFalse(d.update(0.20, 0.30))
        self.assertTrue(d.update(0.35, 0.30))


class TestEnergy(unittest.TestCase):
    def test_constant_power(self):
        rows = [{"t_s": i / 100, "amps": 0.1, "vbus_v": None} for i in range(101)]
        st = wa.stroke_stats(rows, vbus_nominal=5.0, inrush_s=0.05)
        self.assertAlmostEqual(st["energy_j"], 0.5, places=6)
        self.assertAlmostEqual(st["duration_s"], 1.0)

    def test_measured_vbus_used(self):
        rows = [{"t_s": i / 100, "amps": 0.1, "vbus_v": 4.0} for i in range(101)]
        st = wa.stroke_stats(rows, vbus_nominal=5.0, inrush_s=0.0)
        self.assertAlmostEqual(st["energy_j"], 0.4, places=6)
        self.assertEqual(st["min_vbus_v"], 4.0)

    def test_torque_margin(self):
        self.assertAlmostEqual(wa.torque_margin(0.30, 0.15, 0.04), 0.26 / 0.11)
        self.assertIsNone(wa.torque_margin(0.30, 0.03, 0.04))
        self.assertIsNone(wa.torque_margin(None, 0.15, 0.04))


class TestRunWipes(unittest.TestCase):
    def _run(self, jam=(), offset_v=(0.0, 0.0), show=lambda phase: None, **kw):
        clock, gpio = FakeClock(), FakeGpio()
        motor = hw.Motor(gpio)
        adc = SimMotorADC(gpio, clock, jam=jam, offset_v=offset_v)
        state = {}
        sampler = TrackingSampler(adc, state)
        orig_forward, orig_reverse = motor.forward, motor.reverse
        wipe = {"n": 0}

        def fwd():
            wipe["n"] += 1
            state["current"] = (wipe["n"], "fwd")
            orig_forward()

        def rev():
            state["current"] = (wipe["n"], "rev")
            orig_reverse()

        motor.forward, motor.reverse = fwd, rev
        a = args(**kw)
        samples, strokes, self.zeros = wipe_logger.run_wipes(motor, sampler, a, clock=clock, sleep=clock.sleep,
                                                             show=show)
        return samples, strokes, gpio, a

    def test_led_follows_phase(self):
        shown = []
        self._run(wipes=1, show=shown.append)
        self.assertEqual(shown, ["fwd", "brake", "pause", "rev", "brake", "pause", "pause"])
        self.assertEqual(set(wipe_logger.PHASE_LED), {"fwd", "rev"})  # every other phase leaves LED1 off

    def test_clean_run(self):
        samples, strokes, gpio, a = self._run()
        self.assertEqual([(s["wipe"], s["phase"], s["end"]) for s in strokes],
                         [(w, p, "time") for w in (1, 2, 3) for p in ("fwd", "rev")])
        summary = wa.summarize(samples, strokes, 5.0, 0.05, noload_amps=0.04, stall_ref_amps=0.30)
        e = summary["energy_per_wipe_j"]
        self.assertEqual(e["n"], 3)
        # 60 mA at 5 V for ~1 s of drive per wipe = ~0.30 J (quantized to the ADC LSB)
        self.assertAlmostEqual(e["median"], 0.30, delta=0.01)
        self.assertEqual(summary["vbus"], "assumed 5.0 V (no divider)")
        self.assertAlmostEqual(summary["torque_margin"], (0.30 - 0.04) / (0.06 - 0.04), delta=0.1)
        rate = (len(samples) - 1) / (samples[-1]["t_s"] - samples[0]["t_s"])
        self.assertGreater(rate, 700)
        # the run ends with the bridge coasting (caller then sleeps it)
        self.assertEqual((gpio.levels[hw.GPIO_IN1], gpio.levels[hw.GPIO_IN2]), (0, 0))

    def test_stall_cuts_off_and_stops_run(self):
        samples, strokes, gpio, a = self._run(jam={(2, "fwd")})
        self.assertEqual(strokes[-1], {"wipe": 2, "phase": "fwd", "end": "stall", "stall_t": strokes[-1]["stall_t"]})
        self.assertEqual(len(strokes), 3)  # wipe 1 fwd/rev, wipe 2 fwd (stalled), then stop
        stall_rows = [s for s in samples if s["wipe"] == 2 and s["phase"] == "fwd"]
        on_time = stall_rows[-1]["t_s"] - stall_rows[0]["t_s"]
        self.assertLess(on_time, a.stall_ms / 1000 + 0.05)  # test card 9.1 bar
        summary = wa.summarize(samples, strokes, 5.0, 0.05)
        self.assertAlmostEqual(summary["stall_amps"]["median"], 0.30, delta=0.001)
        self.assertEqual(summary["energy_per_wipe_j"]["n"], 1)  # the stalled wipe is left out
        self.assertTrue(summary["wipes"][1]["stalled"])

    def test_max_stalls_keeps_going(self):
        samples, strokes, gpio, a = self._run(jam={(1, "fwd"), (2, "fwd"), (3, "fwd")}, max_stalls=3, rev_s=0)
        self.assertEqual([s["end"] for s in strokes], ["stall"] * 3)

    def test_csv_round_trip(self):
        samples, strokes, gpio, a = self._run(wipes=1)
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "samples.csv")
            wa.write_csv(p, samples)
            back = wa.read_csv(p)
        self.assertEqual(len(back), len(samples))
        self.assertAlmostEqual(back[10]["amps"], samples[10]["amps"])
        self.assertIsNone(back[0]["vbus_v"])


class TestZero(unittest.TestCase):
    OFFSET = (0.00437, 0.00548)  # cam02, J3 open, 11/12

    def _stroke_median(self, samples, phase):
        return sorted(s["amps"] for s in samples if s["phase"] == phase)[len([s for s in samples
                                                                              if s["phase"] == phase]) // 2]

    def test_zero_removes_driver_offset(self):
        t = TestRunWipes()
        samples, strokes, _, _ = t._run(offset_v=self.OFFSET)
        self.assertAlmostEqual(t.zeros["start"]["fwd"], self.OFFSET[0], delta=1e-5)
        self.assertAlmostEqual(t.zeros["start"]["rev"], self.OFFSET[1], delta=1e-5)
        self.assertIsNotNone(t.zeros["end"])
        for phase in ("fwd", "rev"):
            self.assertAlmostEqual(self._stroke_median(samples, phase), 0.060, delta=0.0005)
        # brake and pause carry no current in the sim, so after the zero they read ~0 whichever stroke came first
        for phase in ("brake", "pause"):
            self.assertLess(max(abs(s["amps"]) for s in samples if s["phase"] == phase), 0.0005)
        e = wa.summarize(samples, strokes, 5.0, 0.05)["energy_per_wipe_j"]
        self.assertAlmostEqual(e["median"], 0.30, delta=0.01)

    def test_zero_off_keeps_offset(self):
        samples, _, _, _ = TestRunWipes()._run(offset_v=self.OFFSET, zero="off")
        self.assertAlmostEqual(self._stroke_median(samples, "fwd"), 0.060 + self.OFFSET[0] / 0.2, delta=0.0005)
        self.assertAlmostEqual(self._stroke_median(samples, "rev"), 0.060 + self.OFFSET[1] / 0.2, delta=0.0005)

    def test_zero_rejects_current_flowing(self):
        class Spinning:
            def take(self):
                return {"visen_v": 0.020}  # 100 mA: the motor is still moving
        motor = hw.Motor(FakeGpio())
        with self.assertRaises(wipe_logger.ZeroError):
            wipe_logger.measure_zeros(motor, Spinning(), 0.001, lambda s: None)


class TestI2CRetry(unittest.TestCase):
    def _dev(self):
        dev = hw.I2CDevice.__new__(hw.I2CDevice)  # no /dev/i2c-1 on the desk
        dev.retries = 0
        return dev

    def test_one_error_is_retried(self):
        dev, calls = self._dev(), []

        def flaky():
            calls.append(1)
            if len(calls) == 1:
                raise OSError(121, "Remote I/O error")
            return b"ok"
        self.assertEqual(dev._again(flaky), b"ok")
        self.assertEqual(dev.retries, 1)

    def test_second_error_raises(self):
        dev = self._dev()

        def dead():
            raise OSError(5, "Input/output error")
        with self.assertRaises(OSError):
            dev._again(dead)
        self.assertEqual(dev.retries, 1)


class TestVbusSampler(unittest.TestCase):
    def test_interleaves_ain1(self):
        calls = []

        class Adc:
            def single_shot(self, ch, fsr):
                calls.append((ch, fsr))
                return (0x5000, 2.5) if ch == 1 else (0x0100, hw.raw_to_volts(0x0100, fsr))

        s = wipe_logger.Sampler(Adc(), 0.256, 2000.0, vbus_divider=2.0, vbus_every=4)
        s.begin()
        rows = [s.take() for _ in range(8)]
        self.assertEqual([c[0] for c in calls].count(1), 2)
        self.assertEqual(rows[0]["vbus_v"], 5.0)
        self.assertEqual([r["vbus_fresh"] for r in rows], [1, 0, 0, 0, 1, 0, 0, 0])


class TestArgs(unittest.TestCase):
    def test_stroke_limit(self):
        with self.assertRaises(SystemExit):
            wipe_logger.parse_args(["--run", "x", "--motor", "m", "--fwd-s", "11", "--rev-s", "1"])

    def test_defaults(self):
        a = wipe_logger.parse_args(["--run", "x", "--motor", "4791", "--fwd-s", "1", "--rev-s", "1"])
        self.assertEqual((a.fsr, a.rate, a.r_iset, a.stall_amps, a.max_stalls), (0.256, 860, 2000.0, 0.25, 1))


class TestJog(unittest.TestCase):
    def _ctrl(self, pot_every=0, confirm=True, **settings):
        clock, gpio = FakeClock(), FakeGpio()
        motor = hw.Motor(gpio)
        adc = SimMotorADC(gpio, clock, fsr=1.024, stall_a=0.80)
        sampler = wipe_logger.Sampler(adc, 1.024, 2000.0)
        sampler.zero_v["sleep"] = 0.0
        shown = []
        p = pot.PotAngle(dict(pot.DEFAULT_CAL)) if pot_every else None
        ctrl = jog_server.JogController(motor, sampler, clock=clock, show=shown.append, pot=p, pot_every=pot_every)
        ctrl.set_settings(settings)
        self.clock, self.gpio, self.adc, self.shown = clock, gpio, adc, shown
        if p and confirm:  # every start is latched (PE 018 ruling 2): the operator confirms the arm is on the track
            self._run_for(ctrl, 0.4)
            ctrl.confirm_pot()
        return ctrl

    def _place(self, ctrl, u):
        """Put the sim shaft at pot volts `u` by hand (a teleport the pot can't prove), then confirm it there."""
        self.adc.pot_u = u
        self._run_for(ctrl, 0.4)
        ctrl.confirm_pot()
        self.assertTrue(ctrl.pot.ok)

    def _run_for(self, ctrl, seconds, hb=True):
        end = self.clock.t + seconds
        while self.clock.t < end:
            ctrl.tick()
            if hb:
                ctrl.heartbeat()
            self.clock.sleep(1 / 860)

    def _bridge(self):
        g = self.gpio.levels
        return g.get(hw.GPIO_NSLEEP_HB), g.get(hw.GPIO_IN1), g.get(hw.GPIO_IN2)

    def test_latched_forward_runs_until_stop(self):
        ctrl = self._ctrl()
        ctrl.request("fwd")
        self._run_for(ctrl, 2.0)
        self.assertEqual(ctrl.state, "fwd")
        self.assertEqual(self._bridge(), (1, 1, 0))
        self.assertAlmostEqual(ctrl.buf[-1]["amps"], 0.060, delta=0.002)
        ctrl.request("stop")
        self._run_for(ctrl, 0.05)
        self.assertEqual((ctrl.state, self._bridge()), ("brake", (1, 1, 1)))
        self._run_for(ctrl, 0.1)
        self.assertEqual(ctrl.state, "asleep")
        self.assertEqual(self._bridge()[0], 0)
        self.assertEqual(self.shown[-1], "asleep")
        self.assertIn("stop pressed", ctrl.events[-1]["text"])

    def test_lost_heartbeat_stops(self):
        ctrl = self._ctrl()
        ctrl.request("fwd")
        self._run_for(ctrl, 1.0)
        self._run_for(ctrl, jog_server.WATCHDOG_S + 0.3, hb=False)
        self.assertEqual(ctrl.state, "asleep")
        self.assertIn("lost contact", ctrl.events[-1]["text"])
        self.assertEqual(ctrl.want, "stop")  # does not restart when heartbeats come back
        self._run_for(ctrl, 0.5)
        self.assertEqual(ctrl.state, "asleep")

    def test_stall_cut_off(self):
        ctrl = self._ctrl(stall_amps=0.5, stall_ms=200)
        self.adc.jam, self.adc.wipe_phase = {"held"}, "held"
        ctrl.request("rev")
        self._run_for(ctrl, 1.0)
        self.assertEqual(ctrl.state, "asleep")
        self.assertIn("stall cut-off", ctrl.events[-1]["text"])
        driven = [r for r in ctrl.buf if r["state"] == "rev"]
        self.assertLess(driven[-1]["t_s"] - driven[0]["t_s"], 0.2 + 0.05)  # test card 9.1 bar
        self.assertAlmostEqual(ctrl.status()["peak_a"], 0.80, delta=0.005)

    def test_max_run_time(self):
        ctrl = self._ctrl(max_on_s=1.0)
        ctrl.request("fwd")
        self._run_for(ctrl, 1.5)
        self.assertEqual(ctrl.state, "asleep")
        self.assertIn("max run time", ctrl.events[-1]["text"])

    def test_direction_change_brakes_first(self):
        ctrl = self._ctrl()
        ctrl.request("fwd")
        self._run_for(ctrl, 0.5)
        ctrl.request("rev")
        self._run_for(ctrl, 0.5)
        self.assertEqual(ctrl.state, "rev")
        rows = list(ctrl.buf)
        states = [r["state"] for r in rows]
        first_rev, first_brake = states.index("rev"), states.index("brake")
        self.assertEqual(states[first_brake - 1], "fwd")
        self.assertEqual(states[first_rev - 1], "asleep")
        self.assertGreaterEqual(rows[first_rev]["t_s"] - rows[first_brake]["t_s"], jog_server.BRAKE_S)

    def test_i2c_errors_skip_then_stop(self):
        ctrl = self._ctrl()
        ctrl.request("fwd")
        self._run_for(ctrl, 0.5)
        take, fails = ctrl.sampler.take, {"n": 3}

        def flaky():
            if fails["n"] > 0:
                fails["n"] -= 1
                raise OSError(5, "Input/output error")
            return take()
        ctrl.sampler.take = flaky
        self._run_for(ctrl, 0.2)
        self.assertEqual(ctrl.state, "fwd")  # a few bad reads don't stop it
        self.assertEqual(ctrl.i2c_errors, 3)
        fails["n"] = jog_server.SENSOR_FAIL_N + 5
        self._run_for(ctrl, 0.3)
        self.assertIn(ctrl.state, ("brake", "asleep"))
        self.assertTrue(any("current sensor not answering" in e["text"] for e in ctrl.events))
        self.assertEqual(ctrl.want, "stop")

    def test_wipe_cycle(self):
        ctrl = self._ctrl(max_on_s=1.0)  # a cycle stroke may run past max_on_s
        ctrl.request("cycle", 2.0)
        self._run_for(ctrl, 5.0)
        self.assertEqual(ctrl.state, "asleep")
        self.assertIsNone(ctrl.cycle)
        rows = list(ctrl.buf)
        spans = {}
        for r in rows:
            if r["state"] in ("fwd", "rev"):
                a, _ = spans.get(r["state"], (r["t_s"], None))
                spans[r["state"]] = (a, r["t_s"])
        for d in ("fwd", "rev"):
            self.assertAlmostEqual(spans[d][1] - spans[d][0], 2.0, delta=0.01)
        self.assertLess(spans["fwd"][1], spans["rev"][0])  # forward first, then reverse
        self.assertIn("wipe cycle done", ctrl.events[-1]["text"])

    def test_wipe_cycle_stop_and_guard(self):
        ctrl = self._ctrl()
        ctrl.request("cycle", 5.0)
        self._run_for(ctrl, 1.0)
        with self.assertRaises(ValueError):
            ctrl.request("cycle", 5.0)  # not while the motor runs
        ctrl.request("stop")
        self._run_for(ctrl, 0.5)
        self.assertEqual((ctrl.state, ctrl.cycle), ("asleep", None))
        self.assertEqual(ctrl.want, "stop")

    def test_asleep_reads_zero_amps(self):
        ctrl = self._ctrl()
        ctrl.sampler.zero_v["sleep"] = 0.040  # a floating VISEN that drifted from the boot-time zero
        self._run_for(ctrl, 0.1)
        self.assertTrue(all(r["amps"] == 0.0 for r in ctrl.buf))
        self.assertEqual(ctrl.buf[-1]["state"], "asleep")

    # --- moves to an angle (Jog page) ---
    def _mover(self, vps=2.0):
        ctrl = self._ctrl(pot_every=10)
        self.adc.pot_vps = vps  # sim shaft: 2 V/s ~ 197 deg/s at full speed
        self._run_for(ctrl, 0.1)
        return ctrl

    def test_step_reaches_target(self):
        ctrl = self._mover()
        a0 = ctrl.angle
        ctrl.request_move("step", 90, duty=1.0)
        self._run_for(ctrl, 3.0)
        self.assertIsNone(ctrl.move)
        self.assertEqual(ctrl.state, "asleep")
        self.assertAlmostEqual(ctrl.angle - a0, 90, delta=jog_server.TOL_DEG + 0.3)
        self.assertIn("step +90°: at", ctrl.events[-1]["text"])

    def test_slow_step_uses_pwm(self):
        ctrl = self._mover()
        a0 = ctrl.angle
        ctrl.request_move("step", -15, duty=0.25)
        self._run_for(ctrl, 3.0)
        self.assertAlmostEqual(ctrl.angle - a0, -15, delta=jog_server.TOL_DEG + 0.3)
        self.assertIn((hw.GPIO_IN2, ("pwm", 0.25)), self.gpio.log)  # reverse at 25 %

    def test_wipe_by_angle(self):
        ctrl = self._mover()
        self._place(ctrl, 1.0)  # 180 deg up from here stays on the track
        a0 = ctrl.angle
        ctrl.request_move("wipe", 180, duty=1.0, cycles=3, pause_s=0.2)
        self.assertEqual(ctrl.status()["move"]["total"], 6)
        self._run_for(ctrl, 12.0)
        self.assertIsNone(ctrl.move)
        outs = [e["text"] for e in ctrl.events if "out" in e["text"]]
        self.assertEqual(ctrl.status()["move"], None)
        errs = [float(e["text"].split("error ")[1].split("°")[0]) for e in ctrl.events if "error" in e["text"]]
        self.assertEqual(len(errs), 6)
        self.assertTrue(all(abs(x) < 12 for x in errs), errs)  # wipes don't correct
        self.assertAlmostEqual(ctrl.angle, a0, delta=12)

    def test_moves_into_the_dead_zone_are_refused(self):
        ctrl = self._mover()
        self._place(ctrl, 2.58)  # where Nick had zeroed on 11/15: ~66 deg below the top of SAFE_V
        room = ctrl.status()["room"]
        self.assertAlmostEqual(room["plus"], (3.25 - 2.58) * 98.3, delta=1)
        with self.assertRaises(ValueError) as cm:
            ctrl.request_move("wipe", 90, cycles=5)
        self.assertIn("dead zone", str(cm.exception))
        ctrl.request_move("step", -90)  # the other way is fine
        self._run_for(ctrl, 3.0)
        self.assertTrue(ctrl.pot.ok)

    def test_floating_wiper_is_not_trusted_as_a_moving_path(self):
        # QE 019, N2: cam02 11/15 jog_194426 t=908.79-908.84, then the float wandering as recorded at t=1040.96
        # the run-up at 3.352 V is past SAFE_V, so it is never trusted now: start trusted below it instead (a lead-in)
        p = trusted_pot(3.20, 908.40, zero_v=1.0)
        pts = [(908.78971, 3.35237), (908.80242, 3.35175), (908.81248, 0.80338), (908.82357, 0.59113),
               (908.83356, 0.59788), (908.84349, 0.61850), (908.85635, 0.59325), (908.86689, 0.57388)]
        t = 908.86689
        for v in (0.54575, 0.58013, 0.55250, 0.56300, 0.57150, 0.57488, 0.56712, 0.55, 0.57, 0.59, 0.62, 0.60):
            t += 0.011
            pts.append((t, v))
        good = p.angle
        for t, v in pts:
            p.update(v, t)
            self.assertFalse(p.ok, (t, v))
            self.assertEqual(p.angle, good)
        self.assertEqual(p.state, "latched")

    def test_no_motion_stops(self):
        ctrl = self._mover(vps=0.0)  # shaft held: the pot never moves
        ctrl.request_move("step", 45)
        self._run_for(ctrl, 2.0)
        self.assertEqual(ctrl.state, "asleep")
        self.assertIsNone(ctrl.move)
        self.assertTrue(any("no motion" in e["text"] for e in ctrl.events))

    def test_wrong_way_stops(self):
        ctrl = self._mover(vps=-2.0)  # motor leads swapped: forward now lowers the pot voltage
        ctrl.request_move("step", 45)
        self._run_for(ctrl, 2.0)
        self.assertEqual(ctrl.state, "asleep")
        self.assertTrue(any("moving away" in e["text"] for e in ctrl.events))

    def test_stop_cancels_moves(self):
        ctrl = self._mover()
        ctrl.request_move("wipe", 90, cycles=5)
        self._run_for(ctrl, 0.3)
        ctrl.request("stop")
        self._run_for(ctrl, 0.5)
        self.assertEqual((ctrl.state, ctrl.move, len(ctrl.moves)), ("asleep", None, 0))
        with self.assertRaises(ValueError):  # and nothing queued behind a running move
            ctrl.request_move("step", 5)
            ctrl.request_move("step", 5)

    def test_flip_direction_still_reaches_target(self):
        ctrl = self._mover()
        ctrl.flip_angle()  # display sign only; moves follow it
        a0 = ctrl.angle
        ctrl.request_move("step", 45)
        self._run_for(ctrl, 3.0)
        self.assertAlmostEqual(ctrl.angle - a0, 45, delta=jog_server.TOL_DEG + 0.3)

    def test_approach_method_slows_near_target_and_logs(self):
        ctrl = self._mover()
        a0 = ctrl.angle
        ctrl.request_move("step", 90, duty=1.0, method="approach")
        self._run_for(ctrl, 3.0)
        # the sim has no inertia, so the untrained lead stops it a little short; real coast is what the lead learns
        self.assertAlmostEqual(ctrl.angle - a0, 90, delta=1.5)
        self.assertIn((hw.GPIO_IN1, ("pwm", jog_server.APPROACH_DUTY)), self.gpio.log)
        rec = ctrl.move_log[-1]
        self.assertEqual((rec["method"], rec["dir"], rec["region"]), ("approach", "fwd", "surface"))  # 0 -> 90 deg
        self.assertGreater(rec["energy_j"], 0)
        self.assertGreater(rec["duration_s"], 0)
        self.assertEqual(ctrl.status()["move_log"][-1]["n"], rec["n"])

    def test_approach_bumps_speed_when_slow(self):
        ctrl = self._mover(vps=0.5)  # ~49 deg/s at 100 %: below the 40 deg/s floor at 60 %
        ctrl.request_move("step", 45, duty=1.0, method="approach")
        self._run_for(ctrl, 5.0)
        duties = [d for pin, d in self.gpio.log if pin == hw.GPIO_IN1 and isinstance(d, tuple)]
        self.assertIn(("pwm", jog_server.APPROACH_DUTY), duties)
        self.assertTrue(any(d[1] > jog_server.APPROACH_DUTY + 0.05 for d in duties))  # bumped up
        self.assertIsNone(ctrl.move)

    def test_simple_and_approach_learn_separate_leads(self):
        ctrl = self._mover()
        ctrl.request_move("wipe", 90, duty=1.0, cycles=2, method="approach")
        self._run_for(ctrl, 6.0)
        self.assertEqual(len([r for r in ctrl.move_log if r["method"] == "approach"]), 4)
        self.assertTrue(set(ctrl.status()["leads"]) <= {"+free", "-free", "+surface", "-surface"})
        with self.assertRaises(ValueError):
            ctrl.request_move("step", 5, method="pid")

    def test_slow_strokes_do_not_teach_the_lead(self):
        ctrl = self._mover(vps=0.5)  # ~49 deg/s: below LEARN_MIN_DPS
        ctrl.request_move("step", 30, duty=1.0)
        self._run_for(ctrl, 4.0)
        self.assertEqual(ctrl.lead_s, jog_server.LEAD_S)
        fast = self._mover(vps=2.0)  # ~197 deg/s: learns
        fast.request_move("step", 60, duty=1.0)
        self._run_for(fast, 3.0)
        self.assertNotEqual(fast.lead_s, jog_server.LEAD_S)

    def test_moves_stream_the_pot(self):
        ctrl = self._mover()
        n0 = ctrl.seq
        ctrl.request_move("step", 90, duty=1.0)
        self._run_for(ctrl, 0.3)  # mid-move
        self.assertTrue(ctrl.fast)
        rows = [r for r in ctrl.buf if r["seq"] > n0 and r["state"] == "fwd"]
        self.assertTrue(all(r["pot_v"] is not None for r in rows))  # an angle every sample
        self.assertAlmostEqual(rows[-1]["amps"], 0.060, delta=0.003)  # current still measured (held between reads)
        self._run_for(ctrl, 3.0)
        self.assertFalse(ctrl.fast)  # back to streaming the current
        self.assertEqual(self.adc.cont_ch, 0)
        ctrl.request("fwd")
        self._run_for(ctrl, 0.3)
        self.assertAlmostEqual(ctrl.buf[-1]["amps"], 0.060, delta=0.003)

    def test_stall_cut_off_during_a_move(self):
        ctrl = self._mover()
        ctrl.set_settings({"stall_amps": 0.5, "stall_ms": 200})
        self.adc.jam, self.adc.wipe_phase = {"held"}, "held"  # 0.8 A while driving
        ctrl.request_move("step", 90, duty=1.0)
        self._run_for(ctrl, 1.5)
        self.assertEqual(ctrl.state, "asleep")
        self.assertTrue(any("stall cut-off" in e["text"] for e in ctrl.events))

    # --- QE 019 B3: no-motion and track-end stops outside moves ---
    def test_cycle_stall_under_the_cut_off_stops(self):
        ctrl = self._mover(vps=0.0)  # shaft held: the pot never moves
        self.adc.run_a = 0.30  # under the 0.5 A cut-off (QE's case)
        ctrl.request("cycle", 30.0, 100, 0.5)
        self._run_for(ctrl, 4.0)
        self.assertEqual(ctrl.state, "asleep")
        self.assertIsNone(ctrl.cycle)
        self.assertTrue(any("no motion" in e["text"] for e in ctrl.events), [e["text"] for e in ctrl.events])

    def test_no_motion_threshold_is_5_mv(self):
        # pins RAW_MOVE_V: a still shaft with +-2 mV of noise stops; one creeping ~10 mV per window keeps going
        ctrl = self._mover(vps=0.0)
        self.adc.run_a, self.adc.pot_jitter = 0.30, (0.0, 0.002, -0.002, 0.001)
        ctrl.request("cycle", 3.0, 1, 0.3)
        self._run_for(ctrl, 1.5)
        self.assertTrue(any("no motion" in e["text"] for e in ctrl.events), [e["text"] for e in ctrl.events])
        ctrl = self._mover(vps=0.015)  # ~1.5 deg/s: ~11 mV per 0.72 s window
        self.adc.run_a, self.adc.pot_jitter = 0.30, ()
        ctrl.request("cycle", 3.0, 1, 0.3)
        self._run_for(ctrl, 7.0)
        self.assertFalse(any("no motion" in e["text"] for e in ctrl.events), [e["text"] for e in ctrl.events])

    def test_hold_stops_on_an_untrusted_pot(self):
        ctrl = self._mover()
        ctrl.request("fwd")
        self._run_for(ctrl, 0.2)
        self.adc.pot_u += 1.2  # impossible jump
        self._run_for(ctrl, 0.3)
        self.assertIn(ctrl.state, ("brake", "asleep"))
        self.assertTrue(any("pot reading unreliable" in e["text"] for e in ctrl.events))

    def test_hold_stops_at_the_bottom_end_in_reverse(self):
        ctrl = self._mover()
        self._place(ctrl, 0.4)  # ~29 deg above the bottom of SAFE_V; reverse lowers the voltage
        ctrl.request("rev")
        self._run_for(ctrl, 1.0)
        self.assertTrue(any("end of the pot's safe track" in e["text"] for e in ctrl.events))
        self.assertGreater(ctrl.pot.last_v, 0.10)

    def test_track_end_follows_the_flipped_sign(self):
        ctrl = self._mover()
        ctrl.flip_angle()  # sign -1: the angle now falls going forward; the voltage still rises
        self._place(ctrl, 3.0)
        ctrl.request("fwd")
        self._run_for(ctrl, 1.0)
        self.assertTrue(any("end of the pot's safe track" in e["text"] for e in ctrl.events))
        self.assertLess(ctrl.pot.last_v, 3.25)

    def test_track_end_margin_stops_short_of_the_safe_end(self):
        ctrl = self._mover(vps=1.0)  # ~98 deg/s: the 3 deg margin decides
        self._place(ctrl, 3.0)
        ctrl.request("fwd")
        self._run_for(ctrl, 1.5)
        self.assertLess(ctrl.pot.last_v, 3.25)  # with no margin it would only stop past 3.25 V

    def test_free_spin_with_pot_uncoupled_keeps_running(self):
        ctrl = self._mover(vps=0.0)  # pot not turning, motor free: ~60 mA, under NO_MOTION_MIN_A
        ctrl.request("cycle", 3.0, 1, 0.5)
        self._run_for(ctrl, 2.5)
        self.assertEqual(ctrl.state, "fwd")
        self.assertFalse(any("no motion" in e["text"] for e in ctrl.events))

    def test_hold_stops_at_the_end_of_the_safe_track(self):
        ctrl = self._mover()
        self._place(ctrl, 3.0)  # 25 deg below the top of SAFE_V; forward raises the voltage
        ctrl.request("fwd")
        self._run_for(ctrl, 1.0)
        self.assertEqual(ctrl.state, "asleep")
        self.assertTrue(any("end of the pot's safe track" in e["text"] for e in ctrl.events))
        self.assertLess(ctrl.pot.last_v, 3.35)  # stopped before the dead zone

    # --- QE 019 B5: each failure-mode path has a test ---
    def test_safe_tick_puts_the_bridge_to_sleep(self):
        ctrl = self._ctrl()
        ctrl.request("fwd")
        self._run_for(ctrl, 0.2)
        self.assertEqual(self.gpio.levels[hw.GPIO_NSLEEP_HB], 1)

        def boom():
            raise RuntimeError("I2C bus gone")
        ctrl.tick = boom
        row, ok = jog_server.safe_tick(ctrl, ctrl.motor)
        self.assertFalse(ok)
        self.assertEqual(self.gpio.levels[hw.GPIO_NSLEEP_HB], 0)
        self.assertIn("I2C bus gone", ctrl.fault)
        self.assertEqual(ctrl.state, "asleep")

    def test_cycle_pause_without_heartbeat_ends_the_run(self):
        ctrl = self._ctrl()
        ctrl.request("cycle", 0.5, 3, 3.0)
        self._run_for(ctrl, 1.5)  # first cycle done, resting
        self.assertEqual(ctrl.cycle["stage"], "pause")
        seq0 = ctrl.seq
        self._run_for(ctrl, 3.5, hb=False)
        # the next stroke must never start (not start and then be stopped by the drive watchdog)
        self.assertFalse([r for r in ctrl.buf if r["seq"] > seq0 and r["state"] in ("fwd", "rev")])
        self.assertIsNone(ctrl.cycle)
        self.assertEqual(ctrl.cycle_result["reason"], "lost contact with the page")
        self.assertEqual(ctrl.state, "asleep")

    def test_queued_moves_need_a_heartbeat_to_start(self):
        ctrl = self._mover()
        ctrl.request_move("wipe", 45, cycles=2, pause_s=5.0)
        self._run_for(ctrl, 2.0)  # out stroke done, resting before the return
        self.assertEqual(ctrl.state, "asleep")
        seq0 = ctrl.seq
        self._run_for(ctrl, 6.0, hb=False)  # the rest ends well past the 1.5 s heartbeat window
        self.assertFalse([r for r in ctrl.buf if r["seq"] > seq0 and r["state"] in ("fwd", "rev")])
        self.assertIsNone(ctrl.move)
        self.assertEqual(len(ctrl.moves), 0)
        self.assertTrue(any("lost contact" in e["text"] for e in ctrl.events))

    def test_move_stops_when_the_pot_becomes_untrusted(self):
        ctrl = self._mover()
        ctrl.request_move("step", 90)
        self._run_for(ctrl, 0.2)
        self.adc.pot_u += 1.2  # an impossible jump (~118 deg in one reading)
        self._run_for(ctrl, 0.3)
        self.assertIn(ctrl.state, ("brake", "asleep"))
        self.assertTrue(any("pot reading unreliable" in e["text"] for e in ctrl.events))

    def test_move_refused_while_the_pot_is_untrusted(self):
        ctrl = self._mover()
        ctrl.pot.state = "suspect"
        with self.assertRaises(ValueError) as cm:
            ctrl.request_move("step", 10)
        self.assertIn("pot reading unreliable (suspect", str(cm.exception))

    def test_move_times_out(self):
        ctrl = self._mover(vps=0.05)  # ~5 deg/s: progress keeps coming, the target doesn't
        ctrl.request_move("step", 90)
        self._run_for(ctrl, 6.0)
        self.assertEqual(ctrl.state, "asleep")
        self.assertTrue(any("timed out" in e["text"] for e in ctrl.events))

    def test_learned_leads_survive_a_restart(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "motion_cal.json")
            a = self._mover()
            a.load_motion_cal(path)  # nothing saved yet: defaults
            self.assertEqual(a.lead_s, jog_server.LEAD_S)
            a.request_move("step", 60)
            self._run_for(a, 3.0)
            a.cal_writer.join(2)  # written off the control loop
            learned = a.lead_s
            self.assertNotEqual(learned, jog_server.LEAD_S)
            b = self._mover()
            b.load_motion_cal(path)
            self.assertAlmostEqual(b.lead_s, learned)

    def test_motor_starts_asleep(self):
        g = FakeGpio()
        hw.Motor(g)
        self.assertEqual([g.levels[p] for p in (hw.GPIO_NSLEEP_HB, hw.GPIO_IN1, hw.GPIO_IN2)], [0, 0, 0])

    def test_no_drive_current_guard(self):
        ctrl = self._mover()
        dead = lambda pin, duty, freq=None: self.gpio.output(pin, 0)  # PWM that drives nothing (11/15 bug)
        self.gpio.pwm = dead
        ctrl.request("fwd", duty=0.5)
        self._run_for(ctrl, 1.5)
        texts = [e["text"] for e in ctrl.events]
        self.assertTrue(any("re-creating the PWM" in x for x in texts), texts)
        self.assertTrue(any("no drive current at 50%" in x and "stopped" in x for x in texts), texts)
        self.assertEqual(ctrl.state, "asleep")

    def test_hold_jog_at_speed(self):
        ctrl = self._mover()
        ctrl.request("fwd", duty=0.5)
        self._run_for(ctrl, 0.5)
        self.assertEqual((ctrl.state, ctrl.duty), ("fwd", 0.5))
        self.assertIn((hw.GPIO_IN1, ("pwm", 0.5)), self.gpio.log)

    def test_settings_clamped(self):
        ctrl = self._ctrl()
        s = ctrl.set_settings({"stall_amps": 99, "max_on_s": 0, "bogus": 1})
        self.assertEqual((s["stall_amps"], s["max_on_s"]), (4.0, 0.5))
        self.assertNotIn("bogus", s)
        with self.assertRaises(ValueError):
            ctrl.request("spin")

    def test_page_is_self_contained(self):
        for name in ("jog_page.html", "runs_page.html", "jog_move_page.html"):
            with open(os.path.join(os.path.dirname(jog_server.__file__), name)) as f:
                html = f.read()
            self.assertNotIn("http://", html, name)
            self.assertNotIn("https://", html, name)
            self.assertIn('<a href="/jog"', html, name)  # every page links the other two


class TestPot(unittest.TestCase):
    def test_scale_zero_and_flip(self):
        p, t = trusted_pot(1.0, 0.0, zero_v=None), 0.35
        self.assertEqual(p.angle, 0.0)  # first trusted reading is 0 deg until zeroed
        for i in range(1, 11):  # turned up 0.916 V (~90 deg) at ~90 deg/s
            t += 0.1
            p.update(1.0 + 0.0916 * i, t)
        self.assertTrue(p.ok)
        self.assertAlmostEqual(p.angle, 0.916 * 98.3, places=6)
        p.zero_here()
        self.assertEqual(p.angle, 0.0)
        for i in range(1, 11):
            t += 0.1
            p.update(1.916 - 0.0916 * i, t)
        self.assertAlmostEqual(p.angle, -0.916 * 98.3, places=6)
        p.flip()
        self.assertAlmostEqual(p.update(1.0, t + 0.01), 0.916 * 98.3, places=6)

    def test_no_turn_counting_across_the_gap(self):
        # ruling 1, rule 7: the turn counter is gone; a 3.35 -> 0 V jump is a dropped reading, never a turn
        p = trusted_pot(3.0, 0.0, zero_v=3.0)
        p.update(0.1, 0.36)
        self.assertEqual(p.state, "suspect")
        self.assertEqual(p.angle, 0.0)
        self.assertFalse(hasattr(p, "turns"))

    def test_cal_persists(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "pot_cal.json")
            p = pot.PotAngle(pot.load_cal(path), path=path)
            for i in range(40):
                p.update(2.0, 0.01 * i)
            p.confirm(0.39)
            p.zero_here()
            p.flip()
            cal = pot.load_cal(path)
            self.assertEqual((cal["zero_v"], cal["sign"], cal["deg_per_v"]), (2.0, -1, 98.3))

    def test_controller_reads_pot_and_tracks_angle(self):
        t = TestJog()
        ctrl = t._ctrl(pot_every=10)
        ctrl.request("fwd")
        t._run_for(ctrl, 2.0)
        rows = list(ctrl.buf)
        reads = [r for r in rows if r["pot_v"] is not None]
        self.assertAlmostEqual(len(reads) / len(rows), 0.1, delta=0.01)
        driven = [r for r in rows if r["state"] == "fwd" and r["angle"] is not None]
        # sim pot turns 0.5 V/s forward -> ~49 deg/s
        dps = (driven[-1]["angle"] - driven[0]["angle"]) / (driven[-1]["t_s"] - driven[0]["t_s"])
        self.assertAlmostEqual(dps, 0.5 * 98.3, delta=3)
        self.assertEqual(ctrl.status()["pot_errors"], 0)


def trusted_pot(v, t, zero_v=1.0, every=0.01):
    """A PotAngle held still at `v` for 0.35 s up to time `t + 0.35`, then confirmed there (the operator's word)."""
    p = pot.PotAngle(dict(pot.DEFAULT_CAL, zero_v=zero_v))
    n = int(round(0.35 / every))
    for i in range(n + 1):
        p.update(v, t + i * every)
    p.confirm(t + n * every)
    assert p.ok
    return p


class TestPotTrust(unittest.TestCase):
    """PE 018's ruling 1 (N2) and ruling 2 (start in the gap): ~/NereusData/ops/pe_specs/
    RULINGS_urchincam_pr1_2026-11-15.md @ 09a395d1c56a, tests a-h."""

    # cam02 11/15 jog_194426 samples.csv, every pot reading from t = 1166.98804 (forward from 2.58 V, the run-up) to
    # 1168.79649: the out stroke reaches the top end, brakes into the gap, the reverse stroke stalls on the 0 V end
    # terminal, then the wiper floats 0.08-0.56 V with the motor asleep (QE 019 @ 8b86874, its window 1167.79-1168.80).
    QE_1168 = (
        (1166.98804, 2.58275), (1166.99886, 2.58637), (1167.01064, 2.59987), (1167.02287, 2.62800),
        (1167.03378, 2.65000), (1167.04362, 2.67063), (1167.05320, 2.69612), (1167.06324, 2.71988),
        (1167.07403, 2.74625), (1167.08540, 2.77088), (1167.09552, 2.79113), (1167.10507, 2.81225),
        (1167.11462, 2.83225), (1167.12614, 2.85462), (1167.13714, 2.87787), (1167.14684, 2.89825),
        (1167.15662, 2.92188), (1167.16628, 2.94050), (1167.17578, 2.96525), (1167.18726, 2.99513),
        (1167.19836, 3.01925), (1167.20827, 3.03975), (1167.21780, 3.06325), (1167.23033, 3.09350),
        (1167.25128, 3.13900), (1167.26090, 3.16550), (1167.27044, 3.18975), (1167.27993, 3.21238),
        (1167.28956, 3.23663), (1167.30338, 3.27275), (1167.31304, 3.29588), (1167.32256, 3.31712),
        (1167.33217, 3.33475), (1167.34157, 3.35050), (1167.35168, 3.35162), (1167.36391, 3.35162),
        (1167.37430, 0.77200), (1167.38643, 0.59450), (1167.39849, 0.64587), (1167.41142, 0.00175),
        (1167.42183, 0.00000), (1167.43253, 0.00000), (1167.44185, -0.00013), (1167.45112, 0.00000),
        (1167.46193, -0.00013), (1167.47174, 0.00000), (1167.48283, 0.00000), (1167.49353, -0.00013),
        (1167.50325, 0.00000), (1167.51517, 0.00000), (1167.52526, -0.00013), (1167.53506, -0.00013),
        (1167.54452, -0.00013), (1167.55358, 0.00000), (1167.56290, -0.00013), (1167.57254, 0.00000),
        (1167.58201, 0.00000), (1167.59457, -0.00013), (1167.60426, -0.00013), (1167.61375, 0.00000),
        (1167.62397, 0.00000), (1167.63611, -0.00013), (1167.64599, -0.00025), (1167.65547, -0.00013),
        (1167.66473, 0.00000), (1167.67385, -0.00025), (1167.68357, 0.00000), (1167.69306, -0.00013),
        (1167.70311, -0.00013), (1167.71272, -0.00013), (1167.72188, -0.00013), (1167.73098, -0.00013),
        (1167.74095, -0.00013), (1167.75037, -0.00013), (1167.76042, -0.00013), (1167.76984, -0.00013),
        (1167.77899, 0.00000), (1167.78835, -0.00013), (1167.79816, -0.00013), (1167.80768, 0.01713),
        (1167.81763, 0.52113), (1167.83583, 0.54238), (1167.85644, 0.55175), (1167.87647, 0.55412),
        (1167.89484, 0.55575), (1167.91383, 0.55225), (1167.93385, 0.55063), (1167.95245, 0.54250),
        (1167.97159, 0.52687), (1167.99654, 0.53450), (1168.02360, 0.53363), (1168.04368, 0.53663),
        (1168.06253, 0.52213), (1168.08259, 0.51413), (1168.10266, 0.53200), (1168.12135, 0.51550),
        (1168.14145, 0.49088), (1168.16177, 0.37738), (1168.18065, 0.30588), (1168.20547, 0.19013),
        (1168.22482, 0.09525), (1168.24461, 0.08375), (1168.26569, 0.08525), (1168.28452, 0.08625),
        (1168.30450, 0.08713), (1168.32442, 0.09763), (1168.34345, 0.11050), (1168.36308, 0.16187),
        (1168.38312, 0.23137), (1168.40182, 0.36850), (1168.42796, 0.20488), (1168.44765, 0.25300),
        (1168.46679, 0.30912), (1168.48841, 0.32225), (1168.50721, 0.24525), (1168.52601, 0.18813),
        (1168.54912, 0.12625), (1168.56789, 0.10338), (1168.58667, 0.09913), (1168.61228, 0.09112),
        (1168.63172, 0.09337), (1168.65052, 0.09588), (1168.67512, 0.11025), (1168.69530, 0.12500),
        (1168.71835, 0.11375), (1168.73731, 0.10575), (1168.75570, 0.09187), (1168.77718, 0.09250),
        (1168.79649, 0.09563),
    )

    def _ctrl(self, **kw):
        t = TestJog()
        ctrl = t._ctrl(pot_every=10, **kw)
        return t, ctrl

    def _refused_everything(self, t, ctrl, root, opts):
        for f in (lambda: ctrl.request_move("step", 10), lambda: ctrl.request_move("goto", 0),
                  lambda: ctrl.request_move("wipe", 30), lambda: ctrl.request("fwd"), lambda: ctrl.request("rev"),
                  lambda: ctrl.request("cycle", 3.0, 2, 0.3)):
            with self.assertRaises(ValueError) as cm:
                f()
            self.assertIn("pot reading unreliable", str(cm.exception))
        with self.assertRaises(ValueError):
            jog_server.start_run(ctrl, {"motor": "m", "condition": "c", "stroke_s": 2, "cycles": 1},
                                 {"fsr": 1.024, "sha": "x"}, root, opts)
        self.assertEqual(os.listdir(root), [])  # the refused Run left no folder

    # --- a: QE's recorded readings ---
    def test_a_recorded_float_is_never_trusted(self):
        t, ctrl = self._ctrl(confirm=False)
        t0, v0 = self.QE_1168[0]
        for i in range(36):  # lead-in, not recorded: the arm still at the first reading for 0.35 s
            ctrl._read_pot(fast_v=v0, now=t0 - 0.36 + 0.01 * i)
        t.clock.t = t0 - 0.01
        ctrl.confirm_pot()
        trusted = []
        for tt, v in self.QE_1168:
            ctrl._read_pot(fast_v=v, now=tt)
            if ctrl.pot.ok:
                trusted.append(v)
            if tt >= 1167.79:
                self.assertFalse(ctrl.pot.ok, (tt, v))
                with self.assertRaises(ValueError):
                    ctrl.request_move("step", 10)
        self.assertTrue(trusted)
        self.assertTrue(all(pot.SAFE_V[0] <= v <= pot.SAFE_V[1] for v in trusted))
        self.assertLess(max(trusted), 3.25)
        self.assertEqual(ctrl.pot.state, "latched")

    def test_a_float_after_the_top_end_is_never_trusted(self):
        # QE 019: 1.01, 1.03, 1.22 V after leaving the top end; readings 10 ms apart (the move rate on cam02)
        p, t = trusted_pot(3.10, 0.0), 0.35
        for v in (3.15, 3.20, 3.24):
            t += 0.01
            p.update(v, t)
        for v in (1.01, 1.03, 1.22, 1.22, 1.03, 1.01):
            t += 0.01
            p.update(v, t)
            self.assertFalse(p.ok, v)
        self.assertEqual(p.state, "latched")

    def test_a_slow_sine_float_is_never_trusted(self):
        # QE 019's slow sine 0.30-0.60 V (its exact period isn't in hand: 2 s assumed), read every 20 ms, after the
        # arm runs down off the bottom end; and the same from a fresh start
        import math
        sine = [0.45 - 0.15 * math.cos(2 * math.pi * k * 0.02 / 2.0) for k in range(300)]
        p, t = trusted_pot(0.50, 0.0), 0.35
        for v in (0.45, 0.40, 0.35, 0.30, 0.25, 0.20, 0.15):  # ~490 deg/s down to 15 deg from the end
            t += 0.01
            p.update(v, t)
        self.assertTrue(p.ok)
        for v in sine:
            t += 0.02
            p.update(v, t)
            self.assertFalse(p.ok, (t, v))
        fresh = pot.PotAngle(dict(pot.DEFAULT_CAL))
        for k, v in enumerate(sine):
            fresh.update(v, k * 0.02)
            self.assertFalse(fresh.ok)

    # --- b: start in the gap (ruling 2) ---
    def test_b_start_in_the_gap_refuses_everything(self):
        for amps in (0.15, 0.30):
            for u in (3.45, 3.60):  # floating, then on the 0 V end terminal
                with self.subTest(amps=amps, u=u), tempfile.TemporaryDirectory() as root:
                    opts = os.path.join(root, "..", os.path.basename(root) + "_opts.json")
                    with open(opts, "w") as f:
                        json.dump({"motors": ["m"], "conditions": ["c"]}, f)
                    try:
                        t, ctrl = self._ctrl(confirm=False)
                        t.adc.pot_vps, t.adc.run_a, t.adc.pot_u = 0.0, amps, u  # shaft held in the gap
                        t._run_for(ctrl, 0.5)
                        self.assertEqual(ctrl.status()["pot_state"], "latched")
                        self.assertIn("The arm is on the track", ctrl.status()["pot_next"])
                        self._refused_everything(t, ctrl, root, opts)
                        t._run_for(ctrl, 1.0)
                        self.assertNotIn((hw.GPIO_NSLEEP_HB, 1), t.gpio.log)  # the bridge never woke
                    finally:
                        os.remove(opts)

    # --- c: a float that copies the track ---
    def test_c_a_float_that_copies_the_track(self):
        p = trusted_pot(0.12, 0.0)
        p.update(0.19, 0.40)  # 50 ms later: the 0 V end (12 deg away) is within the 33 deg reach
        self.assertEqual(p.state, "latched")
        p = trusted_pot(1.50, 0.0)
        p.update(1.57, 0.40)  # the same step mid-track
        self.assertEqual(p.state, "trusted")
        self.assertAlmostEqual(p.angle, (1.57 - 1.0) * 98.3, places=6)

    # --- d: outside SAFE_V at each end ---
    def test_d_a_reading_outside_the_safe_track(self):
        for good, bad in ((3.20, 3.27), (0.15, 0.08)):
            with self.subTest(bad=bad):
                p = trusted_pot(good, 0.0)
                p.update(bad, 0.36)  # a 7 deg step, inside the 9 deg reach, but outside SAFE_V
                self.assertEqual(p.state, "suspect")
                self.assertIn("outside the safe track", p.why)
                self.assertAlmostEqual(p.angle, (good - 1.0) * 98.3, places=6)
                p.update(bad, 0.37)  # 20 ms after the last trusted reading the end is within reach
                self.assertEqual(p.state, "latched")

    def _drive_then_bad_reading(self, kind, good, bad):
        t, ctrl = self._ctrl()
        t.adc.pot_vps = 0.0  # the shaft doesn't turn: the readings are only what the test sets
        t._place(ctrl, good)
        root = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(root))
        if kind == "move":
            ctrl.request_move("step", -30 if good > 1 else 30)
        elif kind == "hold":
            ctrl.request("fwd" if good < 1 else "rev")
        elif kind == "cycle":
            ctrl.request("cycle", 3.0, 2, 0.3)
        else:
            opts = os.path.join(root, "opts.json")
            with open(opts, "w") as f:
                json.dump({"motors": ["m"], "conditions": ["c"]}, f)
            jog_server.start_run(ctrl, {"motor": "m", "condition": "c", "stroke_s": 3, "cycles": 2},
                                 {"fsr": 1.024, "sha": "x"}, os.path.join(root, "runs"), opts)
            run = ctrl.run
            self.addCleanup(lambda: run.wait(5))  # its summary is written before the folder goes
        t._run_for(ctrl, 0.2)
        self.assertIn(ctrl.state, ("fwd", "rev"))
        t.adc.pot_u = bad
        t._run_for(ctrl, 0.3)
        return t, ctrl

    def test_d_moves_holds_cycles_and_runs_stop_with_the_reason(self):
        # the sim reads the pot every tick during moves (small reach) and every 10th tick otherwise; the bad step is
        # small enough for both
        cases = {"move": ((3.23, 3.26), (0.12, 0.09)), "hold": ((3.20, 3.27), (0.15, 0.08)),
                 "cycle": ((3.20, 3.27), (0.15, 0.08)), "run": ((3.20, 3.27), (0.15, 0.08))}
        for kind, ends in cases.items():
            for good, bad in ends:
                with self.subTest(kind=kind, bad=bad):
                    t, ctrl = self._drive_then_bad_reading(kind, good, bad)
                    self.assertIn(ctrl.state, ("brake", "asleep"))
                    texts = [e["text"] for e in ctrl.events]
                    self.assertTrue(any("pot reading unreliable" in x and "outside the safe track" in x
                                        for x in texts), texts)
                    self.assertIsNone(ctrl.move)
                    self.assertIsNone(ctrl.cycle)
                    if kind in ("cycle", "run"):
                        self.assertFalse(ctrl.cycle_result["complete"])

    def test_d_a_latch_in_a_cycle_pause_stops_the_next_stroke(self):
        t, ctrl = self._ctrl()
        t.adc.pot_vps = 0.0
        ctrl.request("cycle", 0.5, 3, 2.0)
        t._run_for(ctrl, 1.5)  # first cycle done, resting
        self.assertEqual(ctrl.cycle["stage"], "pause")
        seq0, log0 = ctrl.seq, len(t.gpio.log)
        t.adc.pot_u += 1.6  # moved by hand ~157 deg during the rest, further than the 147 deg to the 0 V end: latched
        t._run_for(ctrl, 2.5)
        self.assertEqual(ctrl.pot.state, "latched")
        self.assertFalse([r for r in ctrl.buf if r["seq"] > seq0 and r["state"] in ("fwd", "rev")])
        self.assertNotIn((hw.GPIO_NSLEEP_HB, 1), t.gpio.log[log0:])  # the bridge never woke for the next stroke
        self.assertIsNone(ctrl.cycle)
        self.assertIn("pot reading unreliable", ctrl.cycle_result["reason"])

    def test_d_a_latch_in_the_brake_stops_the_reverse_stroke(self):
        t, ctrl = self._ctrl()
        t.adc.pot_vps = 0.0
        ctrl.request("cycle", 0.5, 1, 0.3)
        t._run_for(ctrl, 0.45)
        self.assertEqual(ctrl.state, "fwd")
        while ctrl.state == "fwd":  # to the end of the forward stroke
            t._run_for(ctrl, 0.001)
        log0 = len(t.gpio.log)
        ctrl.pot.state, ctrl.pot.why = "latched", "test"  # latched during the brake
        t._run_for(ctrl, 0.5)
        self.assertNotIn((hw.GPIO_IN2, 1), t.gpio.log[log0:])  # reverse never driven
        self.assertIsNone(ctrl.cycle)
        self.assertTrue(any("rev not started: pot reading unreliable" in e["text"] for e in ctrl.events))

    # --- e: suspect, then trusted again or latched ---
    def test_e_one_bad_reading_mid_track_then_good_ones(self):
        p = trusted_pot(1.70, 0.0)
        p.update(2.70, 0.36)  # one impossible reading (98 deg in 10 ms)
        self.assertEqual(p.state, "suspect")
        self.assertAlmostEqual(p.angle, 0.70 * 98.3, places=6)
        p.update(1.71, 0.37)
        self.assertEqual(p.state, "trusted")  # no confirm needed
        self.assertAlmostEqual(p.angle, 0.71 * 98.3, places=6)

    def test_e_a_suspect_pot_that_outlasts_the_reach_latches(self):
        p = trusted_pot(1.00, 0.0)  # 98 deg from the 0 V end: the reach gets there in ~0.16 s
        t = 0.35
        while p.state != "latched":  # 2.50 V is 147 deg away: never within reach before the end is
            t += 0.01
            p.update(2.50, t)
            self.assertNotEqual(p.state, "trusted")
        self.assertLess(t, 0.55)
        for _ in range(50):  # good readings after the latch don't bring it back
            t += 0.01
            p.update(1.00, t)
            self.assertEqual(p.state, "latched")

    def test_e_a_loop_stall_mid_move_stays_trusted(self):
        # cam02 11/15: readings 6 ms apart, then a 51 ms stall while the shaft kept turning at ~280 deg/s
        p = trusted_pot(1.38688, 8.20062 - 0.36, zero_v=0.774)
        for t, v in [(8.20062, 1.38688), (8.20671, 1.40600), (8.21270, 1.42063), (8.26370, 1.5655),
                     (8.2675, 1.5755), (8.2713, 1.5860)]:
            p.update(v, t)
            self.assertTrue(p.ok, (t, v))
        self.assertAlmostEqual(p.angle, (1.5860 - 0.774) * 98.3, places=6)

    # --- f: the confirm ---
    def test_f_confirm_is_refused_while_driving_or_queued(self):
        t, ctrl = self._ctrl()
        ctrl.request("fwd")
        t._run_for(ctrl, 0.2)
        with self.assertRaises(ValueError) as cm:
            ctrl.confirm_pot()
        self.assertIn("stop the motor first", str(cm.exception))
        ctrl.request("stop")
        t._run_for(ctrl, 0.5)
        ctrl.request_move("wipe", 20, cycles=2, pause_s=5.0)
        t._run_for(ctrl, 1.5)  # resting between moves: asleep, more queued
        self.assertEqual(ctrl.state, "asleep")
        self.assertTrue(ctrl.moves)
        with self.assertRaises(ValueError) as cm:
            ctrl.confirm_pot()
        self.assertIn("stop the motor first", str(cm.exception))

    def test_f_confirm_is_refused_outside_the_safe_track(self):
        t, ctrl = self._ctrl(confirm=False)
        t.adc.pot_u = 3.60  # on the 0 V end terminal, in the gap
        t._run_for(ctrl, 0.5)
        with self.assertRaises(ValueError) as cm:
            ctrl.confirm_pot()
        self.assertIn("outside the safe track", str(cm.exception))
        self.assertEqual(ctrl.pot.state, "latched")

    def test_f_confirm_is_refused_while_the_reading_moves(self):
        t, ctrl = self._ctrl(confirm=False)
        t.adc.pot_jitter = (0.0, 0.006)  # 6 mV read to read: the arm is being turned
        t._run_for(ctrl, 0.5)
        with self.assertRaises(ValueError) as cm:
            ctrl.confirm_pot()
        self.assertIn("hold the arm still", str(cm.exception))

    def test_f_confirm_accepted_logged_and_keeps_the_zero(self):
        t, ctrl = self._ctrl(confirm=False)
        zero = ctrl.pot.cal["zero_v"] = 1.20
        t._run_for(ctrl, 0.5)
        self.assertIsNone(ctrl.angle)
        out = ctrl.confirm_pot()
        self.assertEqual(out["pot_state"], "trusted")
        self.assertEqual(ctrl.pot.cal["zero_v"], zero)
        self.assertAlmostEqual(ctrl.angle, (1.5 - 1.20) * 98.3, delta=0.01)
        self.assertIn("pot confirmed on the track by the operator", ctrl.events[-1]["text"])
        ctrl.request_move("step", 10)  # moves allowed again

    def test_f_confirm_window_and_band(self):
        def confirmable(change_ago=None, noise=0.0):
            p = pot.PotAngle(dict(pot.DEFAULT_CAL, zero_v=1.0))
            now = 1.0
            for i in range(101):
                tt = round(i * 0.01, 2)
                v = 1.5 + (noise if i % 2 else 0.0)
                if change_ago is not None and tt < round(now - change_ago, 2):
                    v = 1.45  # where the arm was before it was put here
                p.update(v, tt)
            try:
                p.confirm(now)
                return True
            except ValueError:
                return False
        self.assertTrue(confirmable(change_ago=0.32))  # pins CONFIRM_S = 0.3 from above...
        self.assertFalse(confirmable(change_ago=0.28))  # ...and below
        self.assertTrue(confirmable(noise=0.0049))  # pins CONFIRM_BAND_V = 5 mV
        self.assertFalse(confirmable(noise=0.0051))

    # --- g: the two stated limits ---
    def test_g_limit_a_confirm_in_the_gap_on_a_still_float_is_accepted(self):
        # the confirm is the operator's word: given with the arm in the gap while the float sits still inside SAFE_V
        # it is accepted, and a jam there is then stopped by the no-motion stop only if the float holds within 5 mV;
        # with the float wandering as recorded, only the 0.5 A cut-off or the max run time stops a hold
        t, ctrl = self._ctrl(confirm=False, max_on_s=3.0)
        t.adc.pot_vps, t.adc.run_a, t.adc.pot_u = 0.0, 0.30, 3.45
        t.adc.FLOAT_SEQ = (0.57,)  # a float that sits still
        t._run_for(ctrl, 0.5)
        ctrl.confirm_pot()
        self.assertTrue(ctrl.pot.ok)  # the limit: trusted in the gap
        t.adc.FLOAT_SEQ = SimMotorADC.FLOAT_SEQ  # then it wanders 20-30 mV, as recorded
        ctrl.request("fwd")
        t._run_for(ctrl, 2.5)
        self.assertEqual(ctrl.state, "fwd")  # past the 0.72 s no-motion window: still driving into the jam
        self.assertTrue(ctrl.pot.ok)
        t._run_for(ctrl, 1.0)
        self.assertTrue(any("max run time" in e["text"] for e in ctrl.events), [e["text"] for e in ctrl.events])

    def test_g_limit_a_turn_faster_than_max_dps_is_unseen(self):
        # the proof assumes the arm never turns faster than MAX_DPS (600 deg/s): turned by hand ~120 deg into the gap in
        # 50 ms (2400 deg/s), the float reads 0.95 V, inside the 33 deg reach of the last trusted 1.00 V: trusted
        p = trusted_pot(1.00, 0.0)
        p.update(0.95, 0.40)
        self.assertTrue(p.ok)

    # --- h: boundaries that pin each constant ---
    def test_h_safe_track_edges(self):
        for good, edge, past in ((0.15, 0.10, 0.099), (3.20, 3.25, 3.251)):
            with self.subTest(edge=edge):
                p = trusted_pot(good, 0.0)
                p.update(edge, 0.36)
                self.assertEqual(p.state, "trusted")
                p = trusted_pot(good, 0.0)
                p.update(past, 0.36)
                self.assertEqual(p.state, "suspect")

    def test_h_reach_against_the_track_end(self):
        # last trusted 3.20 V: (3.35 - 3.20) x 98.3 = 14.745 deg to the top end; reach = 600 x dt + 3 deg
        # just under it the reading is trusted, just over it latched (pins TRACK_V, MAX_DPS and the 3 deg)
        dist = (3.35 - 3.20) * 98.3
        for margin, want in ((-0.01, "trusted"), (0.01, "latched")):
            with self.subTest(margin=margin):
                p = trusted_pot(3.20, 0.0)
                p.update(3.20, 0.35 + (dist + margin - 3.0) / 600.0)
                self.assertEqual(p.state, want)
        # the bottom end, the same way (last trusted 0.15 V: 14.745 deg)
        for margin, want in ((-0.01, "trusted"), (0.01, "latched")):
            with self.subTest(end="bottom", margin=margin):
                p = trusted_pot(0.15, 0.0)
                p.update(0.15, 0.35 + (0.15 * 98.3 + margin - 3.0) / 600.0)
                self.assertEqual(p.state, want)

    def test_h_step_within_reach(self):
        # mid-track (the gap far away): a step of reach - 0.01 deg is trusted, reach + 0.01 deg suspect
        dt = 0.01
        reach = 600 * dt + 3.0
        for margin, want in ((-0.01, "trusted"), (0.01, "suspect")):
            with self.subTest(margin=margin):
                p = trusted_pot(1.70, 0.0)
                p.update(1.70 + (reach + margin) / 98.3, 0.35 + dt)
                self.assertEqual(p.state, want)

    def test_h_every_start_is_latched(self):
        p = pot.PotAngle(dict(pot.DEFAULT_CAL, zero_v=1.0))
        self.assertEqual(p.state, "latched")
        for i in range(100):
            p.update(1.5, i * 0.01)  # good, still readings: still latched until the operator confirms
        self.assertFalse(p.ok)
        self.assertIsNone(p.angle)
        with self.assertRaises(ValueError):
            p.zero_here()  # no zero on an untrusted pot


class TestHttpGuard(unittest.TestCase):
    """QE 019 B4: only JSON POSTs, to an allowed Host, from that same origin, can drive the motor."""

    def setUp(self):
        import http.client
        import threading
        from http.server import ThreadingHTTPServer
        self.http = http.client
        self.t = TestJog()
        self.ctrl = self.t._ctrl()
        hosts = set()
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), jog_server.make_handler(self.ctrl, {}, hosts=hosts))
        self.port = self.srv.server_address[1]
        hosts |= jog_server.allowed_hosts(["127.0.0.1"], self.port)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def post(self, path, body, ctype="application/json", origin=None, host=None):
        c = self.http.HTTPConnection("127.0.0.1", self.port, timeout=5)
        h = {"Content-Type": ctype}
        if origin:
            h["Origin"] = origin
        if host:
            h["Host"] = host
        c.request("POST", path, body=body, headers=h)
        r = c.getresponse()
        r.read()
        c.close()
        return r.status

    def test_same_origin_json_is_accepted(self):
        st = self.post("/cmd", '{"action": "fwd"}', origin=f"http://127.0.0.1:{self.port}")
        self.assertEqual(st, 200)
        self.assertEqual(self.ctrl.want, "fwd")

    def test_text_plain_is_refused(self):
        self.assertEqual(self.post("/cmd", '{"action": "fwd"}', ctype="text/plain"), 415)
        self.assertEqual(self.ctrl.want, "stop")

    def test_foreign_origin_is_refused(self):
        self.assertEqual(self.post("/cmd", '{"action": "fwd"}', origin="http://evil.example"), 403)
        self.assertEqual(self.post("/settings", '{"stall_amps": 4}', origin="http://evil.example"), 403)
        self.assertEqual(self.ctrl.settings["stall_amps"], jog_server.LIMITS["stall_amps"][0])

    def test_unknown_host_is_refused(self):  # DNS rebinding
        self.assertEqual(self.post("/cmd", '{"action": "fwd"}', host="evil.example:8765"), 403)

    def test_get_with_an_unknown_host_is_refused(self):
        c = self.http.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("GET", "/api/moves", headers={"Host": "evil.example:8765"})
        r = c.getresponse()
        r.read()
        self.assertEqual(r.status, 403)
        c.request("GET", "/api/moves")
        r = c.getresponse()
        r.read()
        self.assertEqual(r.status, 200)
        c.close()

    def test_bad_bodies_get_400_not_a_dropped_connection(self):
        self.assertEqual(self.post("/cmd", '["fwd"]'), 400)
        self.assertEqual(self.post("/move", '{"kind": "step"}'), 400)
        big = '{"x": "' + "a" * (jog_server.MAX_BODY + 10) + '"}'
        self.assertEqual(self.post("/cmd", big), 413)


class TestWipeRepeat(unittest.TestCase):
    def test_stage10_summary(self):
        mk = lambda e, j: {"error_deg": e, "energy_j": j}
        moves = [mk(-3.5, 0.7), mk(0.4, 0.72), mk(1.0, 0.71), mk(-6.0, 0.73)]
        s = wipe_repeat.summarize(moves)
        self.assertEqual((s["strokes"], s["within_bar"], s["over_worst"]), (4, 2, 1))
        self.assertEqual(s["first_stroke_deg"], -3.5)
        self.assertAlmostEqual(s["energy_per_wipe_j"]["median"], (1.42 + 1.44) / 2)

    def test_refuses_to_overwrite_a_run(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(SystemExit) as cm:
                wipe_repeat.main(["--host", "127.0.0.1:1", "--motor", "m", "--expect-sha", "ad04f80", "--out", d])
            self.assertIn("exists", str(cm.exception.code))


class FakeBench:
    """Stands in for wipe_repeat.Bench: a jog bench that finishes each queued move after a few heartbeats."""

    def __init__(self, sha="ad04f80", state="asleep", stop_angle=0.4, settings=None, n0=600, events_per_stroke=2):
        self.status = {"state": state, "move": None, "sha": sha, "angle": 12.0, "pot_v": 1.2, "throttled": "0x0",
                       "lead_s": jog_server.LEAD_S, "leads": {},
                       "settings": settings or {"stall_amps": 0.5, "stall_ms": 250.0, "max_on_s": 10.0},
                       "events": []}
        self.moves = [{"n": i, "error_deg": 0.0, "energy_j": 0.7} for i in range(n0 - 499, n0 + 1)]  # 500 kept
        self.stop_angle, self.events_per_stroke = stop_angle, events_per_stroke
        self.posts, self.queue, self.events, self.rows, self._seen = [], [], [], [], set()
        self.stop = threading.Event()
        self.t = 0.0

    def get(self, path):
        return {"/api/moves": lambda: list(self.moves)[-500:], "/api/loop": lambda: {"pauses": 0}}[path]()

    def _event(self, text):
        self.t += 0.01
        e = {"t": round(self.t, 3), "text": text}
        self.status["events"] = (self.status["events"] + [e])[-20:]  # the server keeps 20
        wipe_repeat.Bench.take(self, {"status": self.status, "t": [], "a": [], "s": [], "g": []})

    def post(self, path, body=None):
        self.posts.append((path, body))
        if path == "/move":
            if body["kind"] == "goto":
                self.queue = [("go to", self.stop_angle)]
            else:
                self.queue = [(f"wipe {i // 2 + 1} {'out' if i % 2 == 0 else 'back'}", None)
                              for i in range(2 * body["cycles"])]
            self.status["move"] = {"label": self.queue[0][0]}
        elif path == "/hb" and self.queue:
            label, angle = self.queue.pop(0)
            for _ in range(self.events_per_stroke - 1):
                self._event(f"fwd stopped: {label}: at target (peak 800 mA)")
            self._event(f"{label}: at 90.0° (target 90.0°, error +0.0°)")
            n = self.moves[-1]["n"] + 1
            self.moves.append({"n": n, "label": label, "error_deg": 0.5, "energy_j": 0.7})
            if angle is not None:
                self.status["angle"] = angle
            if not self.queue:
                self.status["move"] = None
        elif path == "/cmd" and body.get("action") == "stop":
            self.queue = []
            self.status["move"] = None
        return {}

    take = wipe_repeat.Bench.take


class TestWipeRepeatRun(unittest.TestCase):
    """QE 019 (11/15) on tools/wipe_repeat.py: idle check, heartbeat, Ctrl-C, wipes cap, 500-move log, full events."""

    def go(self, bench, *extra, **kw):
        d = tempfile.mkdtemp()
        a = wipe_repeat.parse_args(["--host", "h", "--motor", "m", "--expect-sha", "ad04f80", "--wipes", "15",
                                    "--out", d, *extra])
        s = wipe_repeat.run(a, bench, d, sleep=lambda s: None, clock=kw.get("clock", time.monotonic),
                            isatty=lambda: "zero" in kw, ask=lambda prompt: kw.get("zero", ""))
        return s, d

    def test_a_full_run(self):
        b = FakeBench()
        s, d = self.go(b, zero="1.5")
        self.assertTrue(s["complete"])
        self.assertTrue(s["conditions_ok"] and s["untrained_at_start"])
        self.assertEqual((s["end_angle"], len(s["end_move"])), (0.4, 1))  # sent back to the start for the reading
        self.assertEqual((s["strokes"], s["strokes_expected"]), (30, 30))  # moves found by n past the 500-move log
        self.assertEqual(s["stops"], [])
        self.assertTrue(s["cutoff_ok"])
        self.assertEqual(s["zero_check_deg"], 1.5)
        with open(os.path.join(d, "events.json")) as f:
            self.assertEqual(len(json.load(f)), 2 * 32)  # every event, not the server's last 20
        with open(os.path.join(d, "meta.json")) as f:
            meta = json.load(f)
        self.assertEqual((meta["sha"], meta["start_angle"], len(meta["start_move"])), ("ad04f80", 0.4, 1))
        self.assertGreaterEqual(sum(1 for p, _ in b.posts if p == "/hb"), 31)  # one heartbeat per stroke at least

    def test_refuses_a_busy_bench(self):
        with self.assertRaises(SystemExit) as cm:
            self.go(FakeBench(state="fwd"))
        self.assertIn("isn't idle", str(cm.exception.code))

    def test_refuses_another_sha(self):
        for sha in ("29d446c", "unknown", "ad04", ""):
            with self.subTest(sha=sha):
                with self.assertRaises(SystemExit) as cm:
                    self.go(FakeBench(sha=sha))
                self.assertIn("doesn't count", str(cm.exception.code))

    def test_refuses_a_start_off_zero(self):
        b = FakeBench(stop_angle=4.0)
        with self.assertRaises(SystemExit) as cm:
            self.go(b)
        self.assertIn("not within 3.0 deg", str(cm.exception.code))
        self.assertFalse(any(body and body.get("kind") == "wipe" for _, body in b.posts))

    def test_no_heartbeat_no_progress(self):
        b = FakeBench()
        b.post = lambda path, body=None, post=b.post: {} if path == "/hb" else post(path, body)
        t = [0.0]

        def clock():
            t[0] += 1.0
            return t[0]
        with self.assertRaises(SystemExit) as cm:
            self.go(b, clock=clock)
        self.assertIn("didn't finish", str(cm.exception.code))
        self.assertIn(("/cmd", {"action": "stop"}), b.posts)

    def test_ctrl_c_stops_the_motor(self):
        b = FakeBench()
        hb = [0]

        def post(path, body=None, post=b.post):
            if path == "/hb" and any(x and x.get("kind") == "wipe" for _, x in b.posts):
                hb[0] += 1
                if hb[0] == 5:
                    raise KeyboardInterrupt
            return post(path, body)
        b.post = post
        s, _ = self.go(b)
        self.assertTrue(s["aborted"])
        self.assertFalse(s["complete"])
        self.assertIn(("/cmd", {"action": "stop"}), b.posts)

    def test_a_safety_stop_is_reported(self):
        b = FakeBench()
        post = b.post

        def stalled(path, body=None):
            r = post(path, body)
            if path == "/hb" and len(b.moves) == 500 + 1 + 7:  # 500 old, the start move, 7 strokes
                b._event("rev stopped: wipe 4 back: no motion for 0.7 s (stalled or too slow) (peak 480 mA)")
                b._event("moves stopped (no motion); 23 not finished")
                b.queue, b.status["move"] = [], None
            return r
        b.post = stalled
        s, _ = self.go(b)
        self.assertFalse(s["complete"])
        self.assertEqual(len(s["stops"]), 2)

    def test_a_changed_cutoff_does_not_count(self):
        s, _ = self.go(FakeBench(settings={"stall_amps": 0.8, "stall_ms": 250.0}))
        self.assertFalse(s["cutoff_ok"])

    def test_ctrl_c_in_the_start_move_stops_the_motor(self):
        b = FakeBench()

        def post(path, body=None, post=b.post):
            if path == "/hb":
                raise KeyboardInterrupt
            return post(path, body)
        b.post = post
        s, d = self.go(b)
        self.assertTrue(s["aborted"])
        self.assertIn(("/cmd", {"action": "stop"}), b.posts)
        self.assertFalse(any(body and body.get("kind") == "wipe" for _, body in b.posts))
        self.assertTrue(os.path.exists(os.path.join(d, "meta.json")))

    def test_events_from_before_the_run_are_not_its_stops(self):
        b = FakeBench()
        b._event("moves stopped (stop pressed); 3 not finished")  # left in the bench's last 20 from earlier
        s, d = self.go(b)
        self.assertEqual(s["stops"], [])
        self.assertTrue(s["complete"])

    def test_a_trained_lead_is_not_untrained(self):
        b = FakeBench()
        b.status["lead_s"] = 0.055  # the simple method's lead, learned earlier; `leads` stays empty
        s, _ = self.go(b)
        self.assertFalse(s["untrained_at_start"])
        self.assertEqual(wipe_repeat.UNTRAINED_LEAD_S, jog_server.LEAD_S)

    def test_other_run_conditions_are_flagged(self):
        s, _ = self.go(FakeBench(), "--pause-s", "0.5")
        self.assertFalse(s["conditions_ok"])

    def test_a_bench_fault_is_not_complete(self):
        b = FakeBench()
        b.status["fault"] = "controller stopped: RuntimeError('I2C bus gone')"
        s, _ = self.go(b)
        self.assertFalse(s["complete"])

    def test_no_zero_reading_is_recorded_as_none(self):
        s, _ = self.go(FakeBench())
        self.assertIsNone(s["zero_check_deg"])

    def test_wipes_over_the_bench_cap_are_refused(self):
        with self.assertRaises(SystemExit):
            wipe_repeat.parse_args(["--host", "h", "--motor", "m", "--expect-sha", "ad04f80", "--wipes", "101",
                                    "--out", "o"])
        with self.assertRaises(SystemExit):  # a short sha would match too much
            wipe_repeat.parse_args(["--host", "h", "--motor", "m", "--expect-sha", "a", "--out", "o"])


class TestScriptBounds(unittest.TestCase):
    def test_wipe_logger_bounds(self):
        base = ["--run", "x", "--motor", "m", "--fwd-s", "1", "--rev-s", "1"]
        with self.assertRaises(SystemExit):
            wipe_logger.parse_args(base + ["--wipes", "5000"])
        with self.assertRaises(SystemExit):
            wipe_logger.parse_args(base + ["--stall-amps", "5"])
        with self.assertRaises(SystemExit):
            wipe_logger.parse_args(base + ["--stall-ms", "100000"])
        self.assertEqual(wipe_logger.parse_args(base + ["--wipes", "10"]).wipes, 10)

    def test_board_check_motor_open_seconds_bound(self):
        with self.assertRaises(SystemExit) as cm:
            board_check.main(["motor-open", "--seconds", "60"])
        self.assertEqual(cm.exception.code, 2)  # argparse error, before any GPIO


class TestLoopStats(unittest.TestCase):
    def test_names_the_cause_of_a_pause(self):
        ls = jog_server.LoopStats()
        ls.record(1.0, 0.003, (0.001, 0.0, 0.0), 0.0, 0.0, 0.001, 0.001, 3)  # a normal step: not kept
        ls.record(2.0, 0.040, (0.001, 0.0, 0.0), 0.0, 0.030, 0.001, 0.001, 3)  # garbage collection
        ls.record(3.0, 0.030, (0.026, 0.0, 0.0), 0.0, 0.0, 0.001, 0.001, 3)  # slow ADC / I2C
        ls.record(4.0, 0.025, (0.001, 0.0, 0.0), 0.0, 0.0, 0.001, 0.022, 5)  # woke late (other threads / OS)
        s = ls.summary()
        self.assertEqual((s["steps"], s["stalls"]), (4, 3))
        self.assertEqual([x["cause"] for x in ls.stalls], ["gc", "adc", "late_wake"])
        self.assertEqual(s["max_gap_ms"], 40.0)

    def test_status_cache_shared(self):
        t = TestJog()
        ctrl = t._ctrl()
        calls = {"n": 0}
        real = ctrl.status

        def counted():
            calls["n"] += 1
            return real()
        ctrl.status = counted
        cache = jog_server.StatusCache(ctrl, {"throttled": "0x0"})
        a, b = cache.json(), cache.json()  # two clients within STATUS_MAX_AGE_S
        self.assertEqual(calls["n"], 1)
        self.assertEqual(json.loads(a)["throttled"], "0x0")
        self.assertEqual(a, b)

    def test_recorder_writes_off_thread(self):
        with tempfile.TemporaryDirectory() as d:
            rec = jog_server.Recorder(d)
            for i in range(50):
                rec.write({"t_s": i / 860, "state": "fwd", "amps": 0.3, "visen_v": 0.06, "raw": 1900, "clipped": False,
                           "pot_v": 1.2 if i % 2 else None, "angle": 40.0, "pot_ok": 1})
            rec.close()
            rows = list(csv.DictReader(open(rec.path)))
        self.assertEqual(len(rows), 50)
        self.assertEqual(rows[1]["pot_v"], "1.20000")


class TestBenchRuns(unittest.TestCase):
    MOTOR, COND = "260:1 test motor", "Free spinning"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.opts = os.path.join(self.root, "opts.json")
        with open(self.opts, "w") as f:
            json.dump({"motors": [self.MOTOR, "1000:1 test motor"], "conditions": [self.COND, "On shaft"]}, f)
        self.runs = os.path.join(self.root, "runs")
        os.makedirs(self.runs)

    def tearDown(self):
        self.tmp.cleanup()

    def _ctrl(self):
        t = TestJog()
        ctrl = t._ctrl()
        return t, ctrl

    def _run(self, stroke_s=1.0, cycles=3, motor=None, stop_after=None, k=0, pot_every=0):
        t = TestJog()
        ctrl = t._ctrl(pot_every=pot_every)
        meta = {"motor": motor or self.MOTOR, "condition": self.COND, "stroke_s": stroke_s, "cycles": cycles,
                "pause_s": 0.5, "notes": "", "settings": ctrl.status()["settings"]}
        import datetime as dt
        run = bench_runs.RunLogger(meta, root=self.runs, threaded=False,
                                   now_utc=dt.datetime(2026, 11, 13, 19, 0, k, tzinfo=dt.timezone.utc))
        ctrl.request("cycle", stroke_s, cycles, 0.5, run=run)
        if stop_after is None:
            t._run_for(ctrl, cycles * (2 * stroke_s + 1.0) + 1.0)
        else:
            t._run_for(ctrl, stop_after)
            ctrl.request("stop")
            t._run_for(ctrl, 0.5)
        self.assertIsNone(ctrl.run)
        return run, ctrl

    def test_complete_run_logs_every_stroke(self):
        run, ctrl = self._run()
        self.assertEqual(ctrl.state, "asleep")
        loaded = bench_runs.load_run(self.runs, run.meta["id"])
        self.assertEqual(loaded["meta"]["status"], "complete")
        strokes = loaded["summary"]["strokes"]
        self.assertEqual([(s["cycle"], s["dir"]) for s in strokes],
                         [(c, d) for c in (1, 2, 3) for d in ("fwd", "rev")])
        self.assertTrue(all(s["complete"] for s in strokes))
        self.assertAlmostEqual(strokes[0]["median_ma"], 60.0, delta=1.0)  # SimMotorADC runs at 60 mA
        self.assertEqual(len(loaded["summary"]["cycles"]), 3)
        # 60 mA x 5 V x 2 s per cycle = 0.6 J
        self.assertAlmostEqual(loaded["summary"]["headline"]["cycle_energy_j"]["median"], 0.6, delta=0.02)

    def test_stopped_run_keeps_only_complete_strokes(self):
        run, ctrl = self._run(stroke_s=1.0, cycles=5, stop_after=3.2)  # stopped in cycle 2
        meta = bench_runs.load_run(self.runs, run.meta["id"])["meta"]
        self.assertEqual(meta["status"], "stopped")
        self.assertEqual(meta["cycles_done"], 1)
        self.assertEqual(meta["ended_reason"], "stop pressed")
        rows = bench_runs.group_stats(self.runs)
        fwd = next(r for r in rows if r["dir"] == "fwd")
        self.assertEqual((fwd["runs"], fwd["stopped_runs"]), (1, 1))
        self.assertEqual(fwd["n"], 1)  # cycle 2's forward stroke was cut short at 3.2 s
        self.assertEqual(next(r for r in rows if r["dir"] == "rev")["n"], 1)

    def test_group_stats_and_include_checkbox(self):
        a, _ = self._run(cycles=2, k=1)
        b, _ = self._run(cycles=2, k=2)
        c, _ = self._run(cycles=2, motor="1000:1 test motor", k=3)
        rows = bench_runs.group_stats(self.runs)
        mine = [r for r in rows if r["motor"] == self.MOTOR]
        self.assertEqual({r["dir"] for r in mine}, {"fwd", "rev", "cycle"})
        fwd = next(r for r in mine if r["dir"] == "fwd")
        self.assertEqual((fwd["runs"], fwd["n"]), (2, 4))
        for k in ("median", "mean", "max", "min"):
            self.assertAlmostEqual(fwd["median_ma"][k], 60.0, delta=1.0)
        bench_runs.set_excluded(self.runs, b.meta["id"], True)  # untick run b
        fwd = next(r for r in bench_runs.group_stats(self.runs) if r["motor"] == self.MOTOR and r["dir"] == "fwd")
        self.assertEqual((fwd["runs"], fwd["n"]), (1, 2))
        listed = {r["id"]: r for r in bench_runs.list_runs(self.runs)}
        self.assertTrue(listed[b.meta["id"]]["excluded"])
        self.assertFalse(listed[a.meta["id"]]["excluded"])
        self.assertEqual(list(listed)[0], c.meta["id"])  # newest first

    def test_strokes_get_sweep_and_speed(self):
        run, _ = self._run(stroke_s=2.0, cycles=2, pot_every=10)
        strokes = bench_runs.load_run(self.runs, run.meta["id"])["summary"]["strokes"]
        fwd = [s for s in strokes if s["dir"] == "fwd"]
        rev = [s for s in strokes if s["dir"] == "rev"]
        for s in fwd:
            self.assertAlmostEqual(s["sweep_deg"], 2.0 * 0.5 * 98.3, delta=5)  # 2 s at ~49 deg/s
            self.assertAlmostEqual(s["speed_dps"], 0.5 * 98.3, delta=3)
        for s in rev:
            self.assertAlmostEqual(s["sweep_deg"], -2.0 * 0.5 * 98.3, delta=5)
        row = next(r for r in bench_runs.group_stats(self.runs) if r["dir"] == "fwd")
        self.assertEqual(row["sweep_deg"]["n"], 2)
        tr = bench_runs.trace(self.runs, run.meta["id"])
        self.assertTrue(any(g is not None for g in tr["g"]))

    def test_trace_keeps_spikes(self):
        run, _ = self._run(cycles=1)
        tr = bench_runs.trace(self.runs, run.meta["id"], max_points=200)
        self.assertLessEqual(len(tr["t"]), 220)
        self.assertEqual(tr["t"], sorted(tr["t"]))
        self.assertIn("f", tr["s"])
        self.assertIn("r", tr["s"])

    def test_run_id_is_checked(self):
        for bad in ("../etc", "2026-11-13T190000Z_x/../../y", ""):
            with self.assertRaises(ValueError):
                bench_runs.run_dir(self.runs, bad)
        with self.assertRaises(KeyError):
            bench_runs.run_dir(self.runs, "2026-11-13T190000Z_nope")

    def test_start_run_checks_the_pick(self):
        t, ctrl = self._ctrl()
        extra = {"fsr": 1.024, "sha": "abc1234"}
        with self.assertRaises(ValueError):
            jog_server.start_run(ctrl, {"motor": "bogus", "condition": self.COND, "stroke_s": 1, "cycles": 1},
                                 extra, self.runs, self.opts)
        rid = jog_server.start_run(ctrl, {"motor": self.MOTOR, "condition": self.COND, "stroke_s": 2,
                                          "cycles": 3, "notes": "x"}, extra, self.runs, self.opts)
        self.assertEqual(ctrl.cycle["n"], 3)
        with self.assertRaises(ValueError):  # one run at a time
            jog_server.start_run(ctrl, {"motor": self.MOTOR, "condition": self.COND, "stroke_s": 2, "cycles": 1},
                                 extra, self.runs, self.opts)
        self.assertEqual(len(os.listdir(self.runs)), 1)  # the refused start left no folder
        meta = bench_runs.load_run(self.runs, rid)["meta"]
        self.assertEqual((meta["status"], meta["source_sha"], meta["mode"]), ("running", "abc1234", "time"))
        run = ctrl.run
        ctrl.request("stop")
        t._run_for(ctrl, 0.3)
        self.assertTrue(run.wait(5))  # the worker wrote summary.json before the temp dir goes
        self.assertEqual(bench_runs.load_run(self.runs, rid)["meta"]["status"], "stopped")

    def test_options_file_ships_valid(self):
        opts = bench_runs.load_options()
        self.assertEqual(len(opts["motors"]), 2)
        self.assertEqual(len(opts["conditions"]), 3)


class TestI2C(unittest.TestCase):
    SCAN = """     0  1  2  3  4  5  6  7  8  9  a  b  c  d  e  f
00:                         -- -- -- -- -- -- -- --
40: -- -- -- -- -- -- -- -- 48 -- 4a -- -- -- -- --
70: -- -- -- -- -- -- -- --                         """

    def test_parse(self):
        self.assertEqual(board_check.parse_i2cdetect(self.SCAN), [0x48, 0x4A])

    def test_judge(self):
        ok, _ = board_check.judge_i2c([0x48, 0x4A])
        self.assertTrue(ok)
        self.assertFalse(board_check.judge_i2c([0x48])[0])
        self.assertFalse(board_check.judge_i2c([0x48, 0x4A, 0x4B])[0])
        self.assertFalse(board_check.judge_i2c([0x48, 0x4B, 0x20])[0])


class TestPlot(unittest.TestCase):
    def test_html_is_self_contained_and_escaped(self):
        samples = [{"t_s": i / 860, "wipe": 1, "phase": "fwd" if i < 400 else "rev", "amps": 0.06,
                    "vbus_v": None} for i in range(800)]
        strokes = [{"wipe": 1, "phase": "fwd", "end": "time", "stall_t": None},
                   {"wipe": 1, "phase": "rev", "end": "time", "stall_t": None}]
        summary = wa.summarize(samples, strokes, 5.0, 0.05)
        summary["args"] = {"motor": "</script><b>x"}
        page = plot_run.build_html("2026-11-12_test", samples, summary)
        self.assertNotIn("</script><b>", page)
        self.assertNotIn("http://", page.replace("http://www.w3.org/2000/svg", ""))
        self.assertNotIn("https://", page)
        data = json.loads(page.split("const D = ", 1)[1].split(";\nconst NS", 1)[0].replace("<\\/", "</"))
        self.assertEqual([b["phase"] for b in data["bands"]], ["fwd", "rev"])

    def test_downsample_keeps_peak(self):
        samples = [{"t_s": i / 860, "amps": 0.05} for i in range(50000)]
        samples[31234]["amps"] = 0.9
        out = plot_run.downsample(samples, 2000)
        self.assertLessEqual(len(out), 2100)
        self.assertIn(0.9, [s["amps"] for s in out])


if __name__ == "__main__":
    unittest.main()
