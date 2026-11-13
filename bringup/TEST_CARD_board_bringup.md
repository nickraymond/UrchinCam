# Test card: UrchinCam expander board bring-up + wiper power bench

Pre-registered before any power-on. Status: **DRAFT, waiting for Nick's OK** (approval names this file `@ <sha>`).
Board: `UrchinCam_Expander_PCBA_01` (UrchinCam `d8776c2`). Scale math and datasheet pages: [VISEN_SCALE.md](VISEN_SCALE.md).
Nick does all wiring, power and mechanical steps. Scripts are in [pi/](pi/); no script runs on a Pi until Nick
gives the host and says go.

- **Question:** Does the board work (rails, I2C, Ethernet, motor driver), and how much energy does one wipe take, and
  what are the stall current and torque margin with the real seal?
- **Conditions:** one board (the only one), one Pi (Nick records model + OS below), one Pololu gearmotor (Nick records
  the part number), the Zebra-Tech brush assembly with its real seal for stages 8–9. Motor supply = the board's +5V
  (there is no other motor rail on this board). Room temperature, dry.
- **n:** stages 0–7 are pass/fail functional checks on n = 1 board (all we have; no design recommendation is made
  from them). Stage 8: **10 wipes** per condition, reported as median and min–max. Stage 9: **3** short stalls.
- **Stop rules (any one, immediately, power off):** smoke, smell, a part too hot to touch; the bench supply goes into
  current limit; a rail outside its bar; the Pi reboots or browns out; motor stalls longer than the cut-off. Record
  what happened and stop the card; don't retry until the cause is understood.
- **Cost:** about 2–3 h of Nick's bench time; no purchases. Optional: two 10 kΩ resistors (stage 8b), an R3 swap.
- **Worth it:** E2/E3 need the drive's energy per wipe and stall behavior with the real seal; a proven on-board
  Ethernet lets the bench Pi stay on Ethernet instead of an Ethernet HAT.

Record sheet: copy the "Result" lines into `~/NereusData/UrchinCam/<YYYY-MM-DD>_bringup/RESULTS.md`.

Pi model: ______  OS (`cat /etc/os-release`): ______  Motor (Pololu #): ______  Bench supply: ______

## Power rule (read first)

`+5V` joins the Pi header 5V (J1.2/J1.4), **J6 pin 1** and the unfitted U4. **Use exactly one 5 V source at a time:**
either J6 from a bench supply with the board **off the Pi**, or the Pi's own supply with **J6 unplugged**.
Never connect anything to J4 (it only feeds U4, which isn't fitted). J6: pin 1 = +5V, pin 2 = GND.

## Stage 0: unpowered (DMM)

| # | Step | Pass | Result |
|---|---|---|---|
| 0.1 | Look: solder bridges on U1 (QFN-12), U2, U3, U5 (QFN-24), U6; parts present per BOM; U4 empty | nothing seen | |
| 0.2 | Ohms TP4 (+5V) → TP2 (GND), both probe polarities | > 100 Ω after the caps charge (a short is < 1 Ω) | |
| 0.3 | Ohms TP3 (+3V3) → TP2 | > 100 Ω | |
| 0.4 | Ohms TP4 → TP3 (5V to 3V3 short would kill the Pi's 3V3) | > 1 kΩ | |
| 0.5 | Ohms J3.1 → J3.3 (OUT1 to OUT2) | not a short (> 100 Ω) | |

## Stage 1: board alone on the bench supply (J6), no Pi

Supply 5.00 V, current limit **150 mA**, connect to J6 with the supply off, then turn it on.
Without the Pi there is no +3V3 (it only comes from J1.1/J1.17), so only U1 and U5 are powered.

| # | Step | Pass | Result |
|---|---|---|---|
| 1.1 | Supply current at turn-on and after 10 s | no current limit; record mA (the RTL8152B idle current is unknown; this run sets it) | |
| 1.2 | TP4 (+5V) | 4.90–5.10 V | |
| 1.3 | TP3 (+3V3) | < 0.3 V (nothing should back-feed 3V3) | |
| 1.4 | RTL8152B 3.3 V: either pad of C20 or C21 that isn't GND | 3.1–3.5 V | |
| 1.5 | RTL8152B 1.0 V: C18 or C19 non-GND pad | 0.9–1.1 V; record | |
| 1.6 | RTL8152B DVDD10_UPS: C24 or C25 non-GND pad | record (no bar: I couldn't read the RTL8152B datasheet) | |
| 1.7 | MP6550 V3P3 (C6 non-GND pad) and VCC (C5) | V3P3 ≈ 0 V is **expected** (nSLEEP_LDO floats low); VCC: record | |
| 1.8 | Touch test after 2 min: U1, U5, FB1 | none hot | |

Why 1.4–1.6 matter: the netlist has no external 3.3 V or 1.0 V source for U5. The schematic symbol marks DVDD33 (pin 13)
and DVDD10 (pin 16) as power outputs, so the chip should make its own rails from VDD5. I could not get the Realtek
datasheet to confirm that, so these readings are the check. If 1.4 fails, stop: Ethernet can't work, and the HAT plan stands.

## Stage 2: on the Pi, Pi's own supply only

Power off. Unplug J6. Seat the board on the Pi header. Power the Pi from its normal supply.

| # | Step | Pass | Result |
|---|---|---|---|
| 2.1 | Pi boots, `vcgencmd get_throttled` | boots; `throttled=0x0` | |
| 2.2 | TP4 (+5V) / TP3 (+3V3) | 4.75–5.25 V / 3.20–3.40 V | |
| 2.3 | Repeat 1.4 (C20/C21) | 3.1–3.5 V | |

## Stage 3: I2C

Enable I2C (`sudo raspi-config nonint do_i2c 0`) if it isn't on. Then `python3 pi/board_check.py i2c`.

| # | Step | Pass | Result |
|---|---|---|---|
| 3.1 | Bus scan `/dev/i2c-1` | **0x48** (ADS1115, ADDR to GND, TI p. 20) and one device at **0x4A or 0x4B** (BNO085, SA0 to GND on this board; BNO085 datasheet not read, the scan confirms which). Nothing else. | |
| 3.2 | `python3 pi/board_check.py ads` (reads AIN0–AIN3 at ±4.096 V, bridge asleep) | AIN0 (VISEN) < 5 mV; AIN1–3 record (floating TPs) | |

Note: BNO085 H_INTN goes only to TP10 and NRST is tied to +3V3, so the Pi can't see its interrupt or reset it. Not
needed for bring-up; flag for the IMU work. ADS1115 ALERT/RDY goes only to TP8, so the logger polls.

## Stage 4: LEDs

`python3 pi/board_check.py leds` lights each color for 1 s at 2 % brightness (`--level 1` for full on). LEDs are common-anode on +3V3; a GPIO low lights it.

| # | Step | Pass | Result |
|---|---|---|---|
| 4.1 | LED1 G/R/B = GPIO17/27/22; LED2 G/R/B = GPIO6/19/26 | each of the 6 colors lights alone, in that order | |

## Stage 5: Ethernet (RTL8152B on J5)

Cable: Pi USB to J5 (JST GH: 1 = 5V_USB, not connected on the board; 2 = D−; 3 = D+; 4 = GND). The chip runs from
the board's +5V, not from the cable. A Pi Zero 2W needs its micro-USB OTG port. J2 to an RJ45 jack: T+/T− as one
twisted pair, R+/R− as the other (RJ45 pins 1/2 and 3/6), to a switch with DHCP. Then `bash pi/eth_check.sh`.

| # | Step | Pass | Result |
|---|---|---|---|
| 5.1 | `lsusb` | a new device **0bda:8152** (Realtek; the ID the Linux `r8152` driver matches, kernel `drivers/net/usb/r8152.c`) | |
| 5.2 | `dmesg`, driver of the new interface | bound to **r8152**, no errors | |
| 5.3 | Link | `operstate` up, speed **100** Mb/s, full duplex | |
| 5.4 | DHCP | an IPv4 address on the new interface within 30 s | |
| 5.5 | `ping -I <if>` the gateway, 100 pings | 0 % loss | |
| 5.6 | Throughput, 30 s each way (`iperf3` if it's on both ends; installing it is Nick's call) | ≥ 80 Mb/s each way | |
| 5.7 | Soak: 10 min ping while stages 6–7 run | 0 link drops (`eth_check.sh` logs carrier changes) | |

If 5.1 fails: re-check 1.4–1.5 and the crystal (Y1, 25 MHz). If 5.3 fails: swap the pairs on J2.

## Stage 6: motor driver, no motor (J3 open)

`python3 pi/board_check.py motor-open` steps the truth table (MP6550 p. 11, Table 1), 2 s per state, and prints
VISEN. Nick reads volts across J3 (red on J3.1, black on J3.3).

| # | State (IN1, IN2) | Pass (DMM J3.1 − J3.3) | Result |
|---|---|---|---|
| 6.1 | nSLEEP_HB low (asleep) | ≈ 0 V, both outputs floating | |
| 6.2 | Forward (1, 0) | +4.7 V to +5.1 V | |
| 6.3 | Reverse (0, 1) | −4.7 V to −5.1 V | |
| 6.4 | Brake (1, 1) | ≈ 0 V | |
| 6.5 | VISEN in all states | < 2 mV (no load current) | |

## Stage 7: motor on J3, free shaft (no seal), DMM in series

Motor leads to J3.1/2 and J3.3/4. DMM on its mA range in series with one lead.
`python3 pi/wipe_logger.py --run motor_free --fwd-s 3 --rev-s 3 --wipes 3 --motor <part#>`

| # | Step | Pass | Result |
|---|---|---|---|
| 7.1 | Shaft turns forward then back each wipe | yes, both directions | |
| 7.2 | Logger mean current vs DMM, steady part of a stroke | within ±15 % or ±5 mA, whichever is bigger (VISEN is below the datasheet's ±5 % range here; this sets the calibration) | |
| 7.3 | Pi stays up, `vcgencmd get_throttled` | `0x0` | |
| 7.4 | Free-run current (median of 3 wipes) | record: it's the no-load current for the torque margin | |

## Stage 8: real seal and brush (energy per wipe)

Motor coupled to the Zebra-Tech assembly with the real seal. Nick sets the stroke times so the brush covers the port
without hitting an end stop (or the run stops on stall and that is recorded).
`python3 pi/wipe_logger.py --run seal --fwd-s <t> --rev-s <t> --wipes 10 --motor <part#> --noload-amps <7.4>`

| # | Step | Pass | Result |
|---|---|---|---|
| 8.1 | 10 wipes, energy per wipe (J) | all 10 complete; report median and min–max | |
| 8.2 | Peak and mean current per stroke | peak < the stall cut-off (no false stalls) | |
| 8.3 | Torque margin estimate = (I_stall − I_noload) / (I_peak − I_noload) | report; E2 asked for ≥ 2× (card `p_e2_drive`: "A gives ~2.2×") | |
| 8b | Optional, only if Nick fits the 10 k + 10 k divider on TP5 (see VISEN_SCALE.md): rerun 8.1 with `--vbus-divider 2.0` | record the minimum +5V during a stroke; ≥ 4.75 V | |

## Stage 9: stall (short)

Nick holds the brush still by hand (or a fixed stop) and runs one stroke.
`python3 pi/wipe_logger.py --run stall --fwd-s 2 --rev-s 0 --wipes 3 --max-stalls 3 --motor <part#>`

| # | Step | Pass | Result |
|---|---|---|---|
| 9.1 | Software cut-off fires | motor off within `--stall-ms` + 50 ms of the current crossing `--stall-amps` | |
| 9.2 | Stall current (median of 3) | record; feeds 8.3 | |
| 9.3 | Motor and gearbox after 3 stalls | still run freely (repeat 7.1) | |

Defaults: `--stall-amps 0.25`, `--stall-ms 150`. They fit a Pololu #4791 at 5 V (stall ≈ 0.30 A if stall current
scales with voltage from Pololu's 0.36 A at 6 V; an estimate). Nick changes them for another motor.

## Outputs

Each logger run writes `~/NereusData/UrchinCam/<YYYY-MM-DD>_<run>/` on the Pi: `samples.csv`, `summary.json`,
`MANIFEST.md`. Copy the folder to the Mac's `~/NereusData/UrchinCam/`, then
`python3 pi/plot_run.py <run folder>` writes `plot.html` next to it. Nothing from a run goes into git.
