"""Desk tests for bringup/pi (no hardware). Run from the repo root:
    python3 -m unittest discover -s bringup/tests -v
"""

import csv
import json
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pi"))

import bench_runs  # noqa: E402
import board_check  # noqa: E402
import jog_server  # noqa: E402
import plot_run  # noqa: E402
import pot  # noqa: E402
import urchin_hw as hw  # noqa: E402
import wipe_analysis as wa  # noqa: E402
import wipe_logger  # noqa: E402


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
    # wiper first floats (~0.57 V, like an unused input), then sits on the 0 V end terminal.
    pot_u, pot_vps, pot_last_t = 1.5, 0.5, None
    POT_TURN_V, POT_TRACK_V = 360 / 98.3, 3.35

    @property
    def pot_v(self):
        v = self.pot_u % self.POT_TURN_V
        if v <= self.POT_TRACK_V:
            return v
        return 0.57 if v < self.POT_TRACK_V + 0.2 else 0.0

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
        return int(self.pot_v / fsr * 32768), self.pot_v

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
    def _ctrl(self, pot_every=0, **settings):
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
        return ctrl

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
        self.adc.pot_u = 1.0  # 180 deg up from here stays on the track
        self._run_for(ctrl, 0.1)
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
        self.adc.pot_u = 2.58  # where Nick had zeroed on 11/15: ~66 deg below the top of SAFE_V
        self._run_for(ctrl, 0.2)
        room = ctrl.status()["room"]
        self.assertAlmostEqual(room["plus"], (3.25 - 2.58) * 98.3, delta=1)
        with self.assertRaises(ValueError) as cm:
            ctrl.request_move("wipe", 90, cycles=5)
        self.assertIn("dead zone", str(cm.exception))
        ctrl.request_move("step", -90)  # the other way is fine
        self._run_for(ctrl, 3.0)
        self.assertTrue(ctrl.pot.ok)

    def test_dead_zone_readings_are_not_trusted(self):
        p = pot.PotAngle(dict(pot.DEFAULT_CAL, zero_v=3.0))
        t = 0.0
        for v in (3.20, 3.27, 3.33):  # shaft turning up at ~70 deg/s, read every 0.1 s
            t += 0.1
            a = p.update(v, t)
        good = a
        for v in (0.749, 0.575, 0.649, 0.57, 0.57, 0.57, 0.57):  # floats in the dead zone, then sits still
            t += 0.01
            self.assertEqual(p.update(v, t), good)
            self.assertFalse(p.ok)
        t += 1.0  # a long rest must not make 0.57 V "plausible"
        self.assertEqual(p.update(0.57, t), good)
        self.assertFalse(p.ok)
        for v in (0.0, 0.0, 0.0):  # comes out on the 0 V end of the track: trusted again, counted past the gap
            t += 0.01
            a = p.update(v, t)
        self.assertTrue(p.ok)
        self.assertAlmostEqual(a, (360 / 98.3 - 3.0) * 98.3, delta=0.1)

    def test_loop_stall_mid_move_is_re_trusted(self):
        # cam02 11/15: readings 6 ms apart, then a 51 ms stall while the shaft kept turning at ~280 deg/s
        p = pot.PotAngle(dict(pot.DEFAULT_CAL, zero_v=0.774))
        pts = [(8.20062, 1.38688), (8.20671, 1.40600), (8.21270, 1.42063), (8.21280, 1.5655),  # mis-stamped
               (8.2165, 1.5755), (8.2203, 1.5860)]
        for t, v in pts:
            p.update(v, t)
        self.assertTrue(p.ok)  # a possible path after the jump: trusted again without stopping
        self.assertAlmostEqual(p.angle, (1.5860 - 0.774) * 98.3, delta=0.1)

    def test_fast_hand_turn_mid_track_is_re_trusted(self):
        p = pot.PotAngle(dict(pot.DEFAULT_CAL, zero_v=1.0))
        p.update(1.0, 0.0)
        for i, v in enumerate((2.0, 2.0, 2.0)):  # jumped 98 deg in 10 ms (too fast), then stayed there
            p.update(v, 0.01 * (i + 1))
        self.assertTrue(p.ok)
        self.assertAlmostEqual(p.angle, 98.3, delta=0.1)

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

    def test_free_spin_with_pot_uncoupled_keeps_running(self):
        ctrl = self._mover(vps=0.0)  # pot not turning, motor free: ~60 mA, under NO_MOTION_MIN_A
        ctrl.request("cycle", 3.0, 1, 0.5)
        self._run_for(ctrl, 2.5)
        self.assertEqual(ctrl.state, "fwd")
        self.assertFalse(any("no motion" in e["text"] for e in ctrl.events))

    def test_hold_stops_at_the_end_of_the_safe_track(self):
        ctrl = self._mover()
        self.adc.pot_u = 3.0  # 25 deg below the top of SAFE_V; forward raises the voltage
        self._run_for(ctrl, 0.2)
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
        ctrl.pot.ok = False
        with self.assertRaises(ValueError) as cm:
            ctrl.request_move("step", 10)
        self.assertIn("dead zone", str(cm.exception))

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
        p = pot.PotAngle(dict(pot.DEFAULT_CAL))
        self.assertEqual(p.update(1.0), 0.0)  # first reading is 0 deg until zeroed
        self.assertAlmostEqual(p.update(1.916), 0.916 * 98.3, places=6)  # ~90 deg
        p.zero_here()
        self.assertEqual(p.angle, 0.0)
        self.assertAlmostEqual(p.update(1.0), -0.916 * 98.3, places=6)
        p.flip()
        self.assertAlmostEqual(p.update(1.0), 0.916 * 98.3, places=6)

    def test_unwraps_across_the_gap(self):
        p = pot.PotAngle(dict(pot.DEFAULT_CAL, zero_v=3.0))
        p.update(3.0)
        a = p.update(0.1)  # 3.35 -> 0 jump: kept counting forward, not a full turn back
        self.assertGreater(a, 0)
        self.assertAlmostEqual(a, (0.1 + 360 / 98.3 - 3.0) * 98.3, places=6)
        self.assertAlmostEqual(p.update(3.0), 0.0, places=6)  # and back again

    def test_cal_persists(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "pot_cal.json")
            p = pot.PotAngle(pot.load_cal(path), path=path)
            p.update(2.0)
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

    def test_bad_bodies_get_400_not_a_dropped_connection(self):
        self.assertEqual(self.post("/cmd", '["fwd"]'), 400)
        self.assertEqual(self.post("/move", '{"kind": "step"}'), 400)
        big = '{"x": "' + "a" * (jog_server.MAX_BODY + 10) + '"}'
        self.assertEqual(self.post("/cmd", big), 413)


class TestScriptBounds(unittest.TestCase):
    def test_wipe_logger_bounds(self):
        base = ["--run", "x", "--motor", "m", "--fwd-s", "1", "--rev-s", "1"]
        with self.assertRaises(SystemExit):
            wipe_logger.parse_args(base + ["--wipes", "5000"])
        with self.assertRaises(SystemExit):
            wipe_logger.parse_args(base + ["--stall-amps", "5"])
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
