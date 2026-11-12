# Motor current sense (VISEN) on the UrchinCam expander board

Board: `UrchinCam_Expander_PCBA_01` at UrchinCam `d8776c2`. Netlist exported with
`kicad-cli sch export netlist` (KiCad 10.0.7) on 2026-11-12.

## What the board has (from the netlist)

| Net | Pins |
|---|---|
| `+5V` | U1.9 (MP6550 VIN), J1.2 / J1.4 (Pi 5V), J6.1, TP4, U4.3 (TSR 3-2450N Vout, not fitted), FB1 → U5 VDD5 |
| `Net-(U1-ISET)` | U1.7 ISET → R3 = **2 kΩ 1 %** → GND |
| `Net-(U1-VISEN)` | U1.11 VISEN → U2.4 ADS1115 **AIN0** (nothing else on the net) |
| `Net-(U2-ADDR)` | U2.1 ADDR → R5 10 kΩ → GND |
| `Net-(U1-~{SLEEP_LDO})` | U1.5 nSLEEP_LDO → TP1 only |
| `/PI_GPIO12`, `/PI_GPIO13`, `/PI_GPIO5` | IN1 (U1.1), IN2 (U1.10), nSLEEP_HB (U1.12) |
| J3 | pins 1+2 = OUT1, pins 3+4 = OUT2 |

## Datasheet facts used

MP6550, MPS, Rev. 1.0, 7/16/2019 (PDF from monolithicpower.com, document_id 6810):

- p. 11, "Current Sensing": VISEN scaling is set by the resistor from ISET to ground; "For 1A of output current,
  100μA of current is sourced into the resistor". Current is sensed in the low-side MOSFETs, "including during slow
  decay (brake) mode". VISEN load must stay "below 2mA, with no more than 500pF of capacitance".
- p. 4, Electrical Characteristics: ISET current 95 / 100 / 105 µA/A (min / typ / max); current trip voltage at the
  VISEN pin, rising 475 / 500 / 525 mV, falling 380 / 400 / 420 mV.
- p. 5: VISEN output voltage accuracy ±5 %, **condition VISET > 0.1 V**; OCP current limit 4 / 5 / 6 A.
- p. 11, "Current Limit / Regulation": the trip happens when VISEN reaches 0.5 V; the bridge then goes to slow decay
  and re-enables once the current is 20 % below the threshold.
- p. 1: 2 A continuous; p. 3: VIN 1.8 V to 22 V; all logic pins abs. max −0.3 V to +5 V.
- p. 3, pin table: IN1, IN2, nSLEEP_HB and nSLEEP_LDO all have internal pull-downs. p. 12: the 3.3 V LDO and the
  H-bridge sleep separately. So with nSLEEP_LDO floating (TP1 only), V3P3 (U1.4) stays off; that does not stop the
  bridge.

ADS1115, TI SBAS444E (Dec 2024):

- p. 20, Table 7-2: ADDR to GND gives address 1001000b = **0x48**.
- p. 26, Table 8-3: PGA settings ±6.144 / 4.096 / 2.048 / 1.024 / 0.512 / 0.256 V; data rates 8 to 860 SPS.
- p. 11, Table 6-1: noise at 860 SPS, ±0.256 V FSR is 7.81 µV RMS (40.78 µV p-p); at ±1.024 V, 31.25 µV RMS
  (64.26 µV p-p).
- p. 4: analog inputs must stay between GND − 0.3 V and VDD + 0.3 V (VDD = +3V3 here, so 3.6 V max).
- p. 5: common-mode input impedance 100 MΩ at ±0.512 / ±0.256 V; 6 MΩ at ±4.096 / ±2.048 V.

## The scale with R3 = 2 kΩ

    VISEN = I_motor × 100 µA/A × R3 = I_motor × 0.2 V/A        (5 mA per mV)

    I_trip = 0.5 V / (100 µA/A × 2 kΩ) = 2.5 A typ
             range 0.475 / (105 µA/A × 2.02 kΩ) = 2.24 A  to  0.525 / (95 µA/A × 1.98 kΩ) = 2.79 A
    release (falling trip, 0.4 V) ≈ 2.0 A

So the current limit is **not too low**: 2.5 A is at or above the MP6550's own 2 A continuous rating.

## Flag: for a Pololu micro metal gearmotor the limit is far too *high*, and the sense signal is small

The drive shortlist (board card `p_e2_drive`, Option A) names **Pololu #4791**, 380:1 LP 6 V. Which two Pololu
motors Nick has in hand is not written anywhere I could read; **Nick to confirm the part number.** For #4791,
pololu.com/product/4791 (read 2026-11-12) gives: stall 0.36 A and 2.9 kg·cm at 6 V (extrapolated), no-load
40 mA at 36 RPM, recommended instantaneous torque limit 2.5 kg·cm, "25% or less of the stall current" for continuous
operation, and stalls "could damage the motor or gearbox".

What that means on this board:

1. **No hardware protection for the motor.** The chip trips at ~2.5 A; the motor stalls at ~0.36 A (6 V). A jammed
   wiper will sit at stall current until software stops it. The logger therefore has a software stall cut-off
   (`--stall-amps`, `--stall-ms`), on by default.
2. **VISEN is below the datasheet's accuracy condition.** ±5 % holds only for VISET > 0.1 V, i.e. above 0.5 A with
   2 kΩ. This motor's whole range (40 mA no-load to 0.36 A stall) gives 8 mV to 72 mV. The number is still useful
   but its accuracy is unspecified, so the test card calibrates it against a DMM in series with the motor.

## ADS1115 setting

| Option | FSR | LSB | Clips at | Noise at 860 SPS (p-p) | Use when |
|---|---|---|---|---|---|
| **A (default)** | ±0.256 V | 7.8 µV = 39 µA | 1.28 A | 40.78 µV = 0.20 mA | Micro metal gearmotor (stall < 1.28 A), R3 = 2 kΩ as built |
| B | ±1.024 V | 31.25 µV = 156 µA | 5.1 A | 64.26 µV = 0.32 mA | A bigger motor, or to see the full 2.5 A trip |

Default: **±0.256 V at 860 SPS**, single-ended AIN0 to GND. The logger flags any sample at full scale as clipped.

## Optional rework (Nick's call, not assumed)

Changing R3 from 2 kΩ to **10 kΩ 1 % (0402)** gives 1.0 V/A, a 0.5 A typ hardware limit, and VISEN > 0.1 V from
100 mA up, which is inside the datasheet's ±5 % condition for most of a wipe. Use ADS1115 ±1.024 V with it, and pass
`--r-iset 10000` to the scripts. The 0.5 A limit still sits above a #4791 stall, so the software cut-off stays.

## Measuring the 5 V dip (needs a part that is not on the board)

AIN1–AIN3 are free on TP5–TP7, but 5 V is above the ADS1115's 3.6 V input limit, so it needs a divider.
**Proposal for Nick:** 10 kΩ + 10 kΩ 1 % from TP4 (+5V) to TP5 (AIN1) to TP2 (GND). That gives 2.5 V at 5 V; read
with ±4.096 V FSR; the 5 kΩ source against the 6 MΩ input impedance is a 0.08 % error. Then run the logger with
`--vbus-divider 2.0`. Without it, the logger uses `--vbus-nominal` (default 5.0 V) for energy and says so in the
summary.
