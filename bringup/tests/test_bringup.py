"""Desk tests for bringup/pi (no hardware). Run from the repo root:
    python3 -m unittest discover -s bringup/tests -v
"""

import json
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pi"))

import board_check  # noqa: E402
import plot_run  # noqa: E402
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
            if (a, b) == (0, 1):
                self.rev_latched = True
            elif (a, b) in ((0, 0), (1, 0)):
                self.rev_latched = False
        gpio.output = output
        self.wipe_phase = None

    def start_continuous(self, channel, fsr=None):
        pass

    def read_raw(self):
        self.clock.t += 0.0003  # an I2C read takes time
        g = self.gpio.levels
        if not g.get(hw.GPIO_NSLEEP_HB):
            amps = 0.0
        else:
            a, b = g.get(hw.GPIO_IN1, 0), g.get(hw.GPIO_IN2, 0)
            driving = a != b
            amps = (self.stall_a if self.wipe_phase in self.jam else self.run_a) if driving else 0.0
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
    def _run(self, jam=(), offset_v=(0.0, 0.0), **kw):
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
        samples, strokes, self.zeros = wipe_logger.run_wipes(motor, sampler, a, clock=clock, sleep=clock.sleep)
        return samples, strokes, gpio, a

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
