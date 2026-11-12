"""Hardware access for the UrchinCam expander board (UrchinCam_Expander_PCBA_01).

Pin and address facts come from the board netlist (see ../VISEN_SCALE.md):
  MP6550 IN1 = GPIO12, IN2 = GPIO13, nSLEEP_HB = GPIO5, VISEN -> ADS1115 AIN0.
  ADS1115 at 0x48 on /dev/i2c-1 (ADDR to GND, TI SBAS444E p. 20).
  LEDs are common-anode on +3V3: driving a GPIO low lights that color.

Standard library only, plus gpiozero or the `pinctrl` tool for GPIO (both ship with
Raspberry Pi OS). Every class takes its backend as an argument so tests can pass fakes.
"""

import fcntl
import os
import shutil
import subprocess
import time

I2C_BUS = 1
I2C_SLAVE = 0x0703  # linux/i2c-dev.h

ADS1115_ADDR = 0x48
GPIO_IN1 = 12
GPIO_IN2 = 13
GPIO_NSLEEP_HB = 5

# (GPIO, label) in the order board_check lights them.
LEDS = [
    (17, "LED1 green"), (27, "LED1 red"), (22, "LED1 blue"),
    (6, "LED2 green"), (19, "LED2 red"), (26, "LED2 blue"),
]

# ADS1115 Config register fields (TI SBAS444E Table 8-3, pp. 25-26).
PGA_BITS = {6.144: 0b000, 4.096: 0b001, 2.048: 0b010, 1.024: 0b011, 0.512: 0b100, 0.256: 0b101}
DR_BITS = {8: 0b000, 16: 0b001, 32: 0b010, 64: 0b011, 128: 0b100, 250: 0b101, 475: 0b110, 860: 0b111}
REG_CONVERSION = 0x00
REG_CONFIG = 0x01


def ads_config_word(channel, fsr, rate, single_shot):
    """Config register value for a single-ended read of AIN<channel> against GND."""
    if channel not in (0, 1, 2, 3):
        raise ValueError("channel must be 0-3")
    mux = 0b100 + channel  # 100b..111b = AINx vs GND
    word = (1 << 15) if single_shot else 0  # OS: start a conversion (single-shot only)
    word |= mux << 12
    word |= PGA_BITS[fsr] << 9
    word |= (1 if single_shot else 0) << 8  # MODE: 1 = single-shot, 0 = continuous
    word |= DR_BITS[rate] << 5
    word |= 0b11  # COMP_QUE = 11b: comparator off (reset value)
    return word


def raw_to_volts(raw, fsr):
    """Two's-complement 16-bit conversion result to volts."""
    if raw >= 0x8000:
        raw -= 0x10000
    return raw * fsr / 32768.0


class I2CDevice:
    """One 7-bit address on /dev/i2c-N, via plain read()/write()."""

    def __init__(self, addr, bus=I2C_BUS):
        self.fd = os.open(f"/dev/i2c-{bus}", os.O_RDWR)
        fcntl.ioctl(self.fd, I2C_SLAVE, addr)

    def write(self, data):
        os.write(self.fd, bytes(data))

    def read(self, n):
        return os.read(self.fd, n)

    def close(self):
        os.close(self.fd)


class ADS1115:
    def __init__(self, dev, fsr=0.256, rate=860):
        self.dev = dev
        self.fsr = fsr
        self.rate = rate
        self.channel = None

    def _write_config(self, word):
        self.dev.write([REG_CONFIG, word >> 8, word & 0xFF])

    def _read_reg(self, reg):
        self.dev.write([reg])
        hi, lo = self.dev.read(2)
        return (hi << 8) | lo

    def start_continuous(self, channel, fsr=None):
        """Convert AIN<channel> back to back; read_raw() then returns the latest result."""
        self.fsr = fsr or self.fsr
        self._write_config(ads_config_word(channel, self.fsr, self.rate, single_shot=False))
        self.dev.write([REG_CONVERSION])  # leave the pointer on the Conversion register
        self.channel = channel
        time.sleep(2.0 / self.rate)  # first result is ready after one conversion (p. 15)

    def read_raw(self):
        """Latest conversion; the pointer already sits on the Conversion register."""
        hi, lo = self.dev.read(2)
        return (hi << 8) | lo

    def single_shot(self, channel, fsr, timeout_s=0.05):
        """One conversion of AIN<channel> at the given range; returns (raw, volts)."""
        self._write_config(ads_config_word(channel, fsr, self.rate, single_shot=True))
        deadline = time.monotonic() + timeout_s
        while not self._read_reg(REG_CONFIG) & 0x8000:  # OS reads 1 when idle
            if time.monotonic() > deadline:
                raise TimeoutError("ADS1115 conversion did not finish")
        raw = self._read_reg(REG_CONVERSION)
        self.channel = None
        return raw, raw_to_volts(raw, fsr)

    def stop(self):
        """Back to single-shot (power-down) mode."""
        self._write_config(ads_config_word(0, self.fsr, self.rate, single_shot=True) & 0x7FFF)


class GpiozeroBackend:
    def __init__(self):
        from gpiozero import DigitalOutputDevice
        self._cls = DigitalOutputDevice
        self.devs = {}

    def output(self, pin, level):
        if pin not in self.devs:
            self.devs[pin] = self._cls(pin, initial_value=bool(level))
        else:
            self.devs[pin].value = bool(level)

    def close(self):
        for dev in self.devs.values():
            dev.close()  # back to input; the MP6550's pull-downs then hold it off
        self.devs.clear()


class PinctrlBackend:
    def __init__(self):
        self.pins = set()

    def output(self, pin, level):
        subprocess.run(["pinctrl", "set", str(pin), "op", "dh" if level else "dl"], check=True)
        self.pins.add(pin)

    def close(self):
        for pin in self.pins:
            subprocess.run(["pinctrl", "set", str(pin), "ip", "pn"], check=False)
        self.pins.clear()


def open_gpio():
    try:
        return GpiozeroBackend()
    except ImportError:
        if shutil.which("pinctrl"):
            return PinctrlBackend()
        raise RuntimeError("no GPIO backend: install python3-gpiozero or use Raspberry Pi OS Bookworm (pinctrl)")


class Motor:
    """MP6550 truth table (datasheet p. 11, Table 1): IN1/IN2 = 10 forward, 01 reverse, 11 brake, 00 coast."""

    def __init__(self, gpio, in1=GPIO_IN1, in2=GPIO_IN2, nsleep=GPIO_NSLEEP_HB):
        self.gpio, self.in1, self.in2, self.nsleep = gpio, in1, in2, nsleep
        self._set(0, 0)
        self.gpio.output(self.nsleep, 0)

    def _set(self, a, b):
        self.gpio.output(self.in1, a)
        self.gpio.output(self.in2, b)

    def wake(self):
        self._set(0, 0)
        self.gpio.output(self.nsleep, 1)
        time.sleep(0.001)

    def forward(self):
        self._set(1, 0)

    def reverse(self):
        self._set(0, 1)

    def brake(self):
        self._set(1, 1)

    def coast(self):
        self._set(0, 0)

    def sleep(self):
        self.coast()
        self.gpio.output(self.nsleep, 0)
