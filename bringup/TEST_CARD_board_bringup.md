# Test card: UrchinCam expander board bring-up + wiper power bench

Pre-registered before any power-on. Status: **the card as a whole is not approved**; only stage 5 is (quoted below,
run 11/15); stage 8 was closed on existing data and stage 10 is proposed (approval names this file `@ <sha>`;
board card `nick_wiper_bringup_ok`). Stages 2-4, 6 and 7 **ran anyway**, on Nick's go in the builder chat, not on
an approved card (QE 019, 11/15, B2). Stage 5 (5.1-5.6) ran 11/15 3:21 PM on his stage 5 approval and his QE
override (~3:02 PM: "I am over ridinging QE - work with me - run some test"), before QE's stage 5 spec PASS @ 29d446c.
His words, read in that chat:
- 11/12: "lets jump to pi bring up first, i have "cam002" plugged in ... can you ssh it", then "I went rouge and just
  plugged the board ontop of the pi, it is rebooting. Please try to communicate with the ADC and the motor driver
  ... see how far you can get without me". Stages 0-1 (bench supply, board off the Pi) were skipped; 1.5 was
  measured on the Pi's power instead.
- 11/13: "ok i have the two motors, tell me when to plug one in for a test"; "you are allowed to access the pi, stop
  asking me".
- 11/15: the jog bench comparisons in [WIPER_BENCH.md](WIPER_BENCH.md) ran outside this card; Nick later had stage 8 closed on
  their 1334 run (see stage 8).
- 11/15, about 2:32 PM: "Yes stage 5 approved" (asked whether his "Approved" covered this card @ 89fdd53 with
  stage 10; his answer names stage 5 only). Stage 5's text has not changed since, except its Result cells.
Per QE 019 this card is the PREREG file (tier a): the bars below must stand, QE spec PASS and Nick's OK on the sha
the board card names, before the next hardware run. Results marked "(not saved)" were read live in the session or on
Nick's meter; no file holds them.
Board: `UrchinCam_Expander_PCBA_01` (UrchinCam `d8776c2`). Scale math and datasheet pages: [VISEN_SCALE.md](VISEN_SCALE.md).
Nick does all wiring, power and mechanical steps. Scripts are in [pi/](pi/). Rule going forward: the builder may
run bench scripts on cam02 (Nick, 11/13: "you are allowed to access the pi, stop asking me"), but a hardware stage
of this card runs only once the card is approved at the sha on the board card; field units and Spotters never.

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

Pi model: Raspberry Pi Zero 2 W Rev 1.0 (cam02)  OS: Debian 12 bookworm, kernel 6.12.47+rpt-rpi-v8
Motor (Pololu #): not recorded; 260:1 and 1000:1 micro metal gearmotors, treated as 6 V versions (Nick, 11/13)
Bench supply: none; everything on the Pi's USB 5 V (stages 0-1 skipped; 1.5 measured on the Pi's power instead).

## Power rule (read first)

`+5V` joins the Pi header 5V (J1.2/J1.4), **J6 pin 1** and the unfitted U4. **Use exactly one 5 V source at a time:**
either J6 from a bench supply with the board **off the Pi**, or the Pi's own supply with **J6 unplugged**.
Never connect anything to J4 (it only feeds U4, which isn't fitted). J6: pin 1 = +5V, pin 2 = GND.

## Stage 0: unpowered (DMM)

| # | Step | Pass | Result |
|---|---|---|---|
| 0.1 | Look: solder bridges on U1 (QFN-12), U2, U3, U5 (QFN-24), U6; parts present per BOM; U4 empty | nothing seen | not run: the board went straight onto the Pi (11/12) |
| 0.2 | Ohms TP4 (+5V) → TP2 (GND), both probe polarities | > 100 Ω after the caps charge (a short is < 1 Ω) | not run: the board went straight onto the Pi (11/12) |
| 0.3 | Ohms TP3 (+3V3) → TP2 | > 100 Ω | not run: the board went straight onto the Pi (11/12) |
| 0.4 | Ohms TP4 → TP3 (5V to 3V3 short would kill the Pi's 3V3) | > 1 kΩ | not run: the board went straight onto the Pi (11/12) |
| 0.5 | Ohms J3.1 → J3.3 (OUT1 to OUT2) | not a short (> 100 Ω) | not run: the board went straight onto the Pi (11/12) |

## Stage 1: board alone on the bench supply (J6), no Pi

Supply 5.00 V, current limit **150 mA**, connect to J6 with the supply off, then turn it on.
Without the Pi there is no +3V3 (it only comes from J1.1/J1.17), so only U1 and U5 are powered.

| # | Step | Pass | Result |
|---|---|---|---|
| 1.1 | Supply current at turn-on and after 10 s | no current limit; record mA (the RTL8152B idle current is unknown; this run sets it) | not run: the board went straight onto the Pi (11/12) |
| 1.2 | TP4 (+5V) | 4.90–5.10 V | not run: the board went straight onto the Pi (11/12) |
| 1.3 | TP3 (+3V3) | < 0.3 V (nothing should back-feed 3V3) | not run: the board went straight onto the Pi (11/12) |
| 1.4 | RTL8152B 3.3 V: either pad of C20 or C21 that isn't GND | 3.1–3.5 V | not run: the board went straight onto the Pi (11/12) |
| 1.5 | RTL8152B 1.0 V: C18 or C19 non-GND pad | 0.9–1.1 V; record | PASS 11/12: C18/C19 1.04 V (Nick, on the Pi's power, not a bench supply) (not saved) |
| 1.6 | RTL8152B DVDD10_UPS: C24 or C25 non-GND pad | record (no bar: I couldn't read the RTL8152B datasheet) | not run: the board went straight onto the Pi (11/12) |
| 1.7 | MP6550 V3P3 (C6 non-GND pad) and VCC (C5) | V3P3 ≈ 0 V is **expected** (nSLEEP_LDO floats low); VCC: record | not run: the board went straight onto the Pi (11/12) |
| 1.8 | Touch test after 2 min: U1, U5, FB1 | none hot | not run: the board went straight onto the Pi (11/12) |

Why 1.4–1.6 matter: the netlist has no external 3.3 V or 1.0 V source for U5. The schematic symbol marks DVDD33 (pin 13)
and DVDD10 (pin 16) as power outputs, so the chip should make its own rails from VDD5. I could not get the Realtek
datasheet to confirm that, so these readings are the check. If 1.4 fails, stop: Ethernet can't work, and the HAT plan stands.

## Stage 2: on the Pi, Pi's own supply only

Power off. Unplug J6. Seat the board on the Pi header. Power the Pi from its normal supply.

| # | Step | Pass | Result |
|---|---|---|---|
| 2.1 | Pi boots, `vcgencmd get_throttled` | boots; `throttled=0x0` | PASS 11/12: boots with the board fitted; throttled=0x0 before and after every test (not saved) |
| 2.2 | TP4 (+5V) / TP3 (+3V3) | 4.75–5.25 V / 3.20–3.40 V | PASS 11/12: TP4 4.99 V / TP3 3.33 V (Nick) (not saved) |
| 2.3 | Repeat 1.4 (C20/C21) | 3.1–3.5 V | not run: C20/C21 face the Pi; needs the board off the Pi |

## Stage 3: I2C

Enable I2C (`sudo raspi-config nonint do_i2c 0`) if it isn't on. Then `python3 pi/board_check.py i2c`.

| # | Step | Pass | Result |
|---|---|---|---|
| 3.1 | Bus scan `/dev/i2c-1` | **0x48** (ADS1115, ADDR to GND, TI p. 20) and one device at **0x4A or 0x4B** (BNO085, SA0 to GND on this board; BNO085 datasheet not read, the scan confirms which). Nothing else. | PASS 11/12: 0x48 only; BNO085 not fitted on this board (Nick), so 0x4A/0x4B absent is expected (not saved) |
| 3.2 | `python3 pi/board_check.py ads` (reads AIN0–AIN3 at ±4.096 V, bridge asleep) | AIN0 (VISEN) < 5 mV; AIN1–3 record (floating TPs) | PASS 11/12: AIN0 4.38 mV with the driver awake at boot, 0.20 mV asleep; AIN1-3 float ~572 mV (not saved) |

Note: BNO085 H_INTN goes only to TP10 and NRST is tied to +3V3, so the Pi can't see its interrupt or reset it. Not
needed for bring-up; flag for the IMU work. ADS1115 ALERT/RDY goes only to TP8, so the logger polls.

## Stage 4: LEDs

`python3 pi/board_check.py leds` lights each color for 1 s at 2 % brightness (`--level 1` for full on). LEDs are common-anode on +3V3; a GPIO low lights it.

| # | Step | Pass | Result |
|---|---|---|---|
| 4.1 | LED1 G/R/B = GPIO17/27/22; LED2 G/R/B = GPIO6/19/26 | each of the 6 colors lights alone, in that order | PASS 11/12: Nick "G/R/B on LED1 then 2" (full brightness; 2 % default since 8e05f9f) (not saved) |

## Stage 5: Ethernet (RTL8152B on J5)

Cable: Pi USB to J5 (JST GH: 1 = 5V_USB, not connected on the board; 2 = D−; 3 = D+; 4 = GND). The chip runs from
the board's +5V, not from the cable. A Pi Zero 2W needs its micro-USB OTG port. J2 to an RJ45 jack: T+/T− as one
twisted pair, R+/R− as the other (RJ45 pins 1/2 and 3/6), to a switch with DHCP. Then `bash pi/eth_check.sh`.

| # | Step | Pass | Result |
|---|---|---|---|
| 5.1 | `lsusb` | a new device **0bda:8152** (Realtek; the ID the Linux `r8152` driver matches, kernel `drivers/net/usb/r8152.c`) | PASS 11/15 3:21 PM: 0bda:8152 on USB (`~/NereusData/UrchinCam/2026-11-15_eth/eth_check_232112.txt`) |
| 5.2 | `dmesg`, driver of the new interface | bound to **r8152**, no errors | PASS: eth0 bound to r8152 |
| 5.3 | Link | `operstate` up, speed **100** Mb/s, full duplex | PASS: link up 100 Mb/s full. Earlier checks up to 3:18 PM had no carrier; it came up after Nick re-pinned the GH connector to the Blue Robotics RJ45 adapter (its GH pinout differs from J2): J2.1→adapter 1 (TX+), J2.2→4 (TX−), J2.3→2 (RX+), J2.4→3 (RX−). Nick, builder chat 11/15: "I reworked the pins on the GH connector per your instructions and that fixed it." |
| 5.4 | DHCP | an IPv4 address on the new interface within 30 s | PASS: 192.168.1.238/24 |
| 5.5 | `ping -I <if>` the gateway, 100 pings | 0 % loss | PASS: 100 pings to 192.168.1.1, 0 % loss |
| 5.6 | Throughput, 30 s each way (`iperf3` if it's on both ends; installing it is Nick's call) | ≥ 80 Mb/s each way | PASS: Pi→Mac 90, Mac→Pi 94 Mb/s (iperf3; the Mac on Wi-Fi) |
| 5.7 | Soak: 10 min ping while stages 6–7 run | 0 link drops (`eth_check.sh` logs carrier changes) | |

If 5.1 fails: re-check 1.4–1.5 and the crystal (Y1, 25 MHz). If 5.3 fails: swap the pairs on J2.

## Stage 6: motor driver, no motor (J3 open)

`python3 pi/board_check.py motor-open` steps the truth table (MP6550 p. 11, Table 1), 2 s per state, and prints
VISEN. Nick reads volts across J3 (red on J3.1, black on J3.3).

| # | State (IN1, IN2) | Pass (DMM J3.1 − J3.3) | Result |
|---|---|---|---|
| 6.1 | nSLEEP_HB low (asleep) | ≈ 0 V, both outputs floating | PARTIAL 11/12: asleep TP16 0.001 V, TP21/TP18 0.000 V (Nick), VISEN 0.02 mV; J3 not read (not saved) |
| 6.2 | Forward (1, 0) | +4.7 V to +5.1 V | PASS 11/12: J3.1-J3.3 +4.9 V (Nick); VISEN 4.37 mV, no load (not saved) |
| 6.3 | Reverse (0, 1) | −4.7 V to −5.1 V | PASS 11/12: Nick "Yes, I agree with your expected values" (exact J3 value not given); VISEN 5.44 mV live (not saved) |
| 6.4 | Brake (1, 1) | ≈ 0 V | PASS 11/12: Nick "Yes I agree with you expectations" (exact values not given); VISEN 5.44 mV live, brake entered from reverse (saved J3-open logger runs: brake 4.35-5.49 mV, ~4.4 after forward, ~5.5 after reverse) (not saved) |
| 6.5 | VISEN in all states | asleep < 2 mV; awake: record the no-load offset per state (cam02 11/12: ~4.4 mV, ~5.5 mV in reverse; see VISEN_SCALE.md) | RECORDED 11/12: asleep 0.02 mV; awake 4.36 mV fwd, 5.44-5.49 mV rev. **Bar changed after the data** (was "< 2 mV" in all states; 5399ad9, minutes after these runs) |

## Stage 7: motor on J3, free shaft (no seal), DMM in series

Motor leads to J3.1/2 and J3.3/4. DMM on its mA range in series with one lead.
`python3 pi/wipe_logger.py --run motor_free --fwd-s 3 --rev-s 3 --wipes 3 --motor <part#>`

| # | Step | Pass | Result |
|---|---|---|---|
| 7.1 | Shaft turns forward then back each wipe | yes, both directions | PASS 11/13: 260:1 and 1000:1 both turned both ways (Nick); reverse sounds slower on both (not saved) |
| 7.2 | Logger mean current vs DMM, steady part of a stroke | within ±15 % or ±5 mA, whichever is bigger (VISEN is below the datasheet's ±5 % range here; this sets the calibration) | SKIPPED 11/13: no DMM in series (motor leads already on the connector, Nick); VISEN not calibrated |
| 7.3 | Pi stays up, `vcgencmd get_throttled` | `0x0` | PASS 11/13: throttled=0x0 before and after, both motors (not saved) |
| 7.4 | Free-run current (median of 3 wipes) | record: it's the no-load current for the torque margin | RECORDED 11/13, free shaft, 3 s + 3 s x 3: 260:1 fwd 78-84 / rev 88-97 mA; 1000:1 fwd 90-108 / rev 121-133 mA (`2026-11-13_motor_free_260`, `_1000`) |

## Stage 8: real seal and brush (energy per wipe)

**Closed on the jog bench data, after the fact** (not run as written here). Nick, builder chat 11/15: "Stage 8 - we
are doing that already, all wiper hardware tests have been with the actual Zebra brush + motor assembly stack
(bearings, seals, coulers) over a plastic surface that is rougher than my machined housing and glass will be. Can we
call this done now since we already have teh data?" The plan below (`wipe_logger.py`, 0.25 A / 150 ms cut-off) was
never run: that cut-off is under the brush's running current and would stop every stroke (QE 019).

Data: the 11/15 `1334` run, `~/NereusData/UrchinCam/2026-11-15_1334_compare/` (`moves.json` simple, `samples_simple.csv`),
code 6254b8f: 30 wipes 0 -> +90 -> 0 deg, simple method, 100 %, 0.3 s rest, 1000:1 motor at 5 V, the Zebra-Tech
brush and seal stack on the plastic surface, hand-held rig, jog bench cut-off 0.5 A / 250 ms. One run (n = 1); the
other 30-wipe runs are in [WIPER_BENCH.md](WIPER_BENCH.md). During moves the current is read every 8th sample, so a
peak shorter than ~20 ms may be missed.

| # | Step | Pass | Result |
|---|---|---|---|
| 8.1 | Energy per wipe (J) | all wipes complete; report median and min–max | DONE (after the fact): 30 / 30 wipes, 60 / 60 strokes, 0 corrections; **1.435 J** median, 1.358–1.532 J (motor only, 5 V assumed) |
| 8.2 | Peak and mean current per stroke | no false stalls | DONE (after the fact): 0 cut-off stops in 60 strokes. Peak per stroke median 868 mA (571–1254); above 0.5 A for at most 0.039 s against the 0.25 s window. Mean while driving median 0.407 A (0.375–0.456) |
| 8.3 | Torque margin estimate = (I_stall − I_noload) / (I_peak − I_noload) | report; E2 asked for ≥ 2× (card `p_e2_drive`: "A gives ~2.2×") | waits on stage 9 (stall current) |
| 8b | Optional: minimum +5V during a stroke, with the 10 k + 10 k divider on TP5 (see VISEN_SCALE.md) | ≥ 4.75 V | not run (no divider fitted) |

The final housing (machined) and glass are smoother than this surface (Nick, above), so these currents and energies
are expected to be on the high side; not measured.

## Stage 9: stall (short)

**To be rewritten before it runs** (QE 019): as written it uses `wipe_logger.py` with the 0.25 A / 150 ms cut-off,
below the brush's ~390 mA running current on cam02 (11/15), so it would stop every stroke. The rewrite sets the
cut-off from measured current and runs on the jog bench.

Nick holds the brush still by hand (or a fixed stop) and runs one stroke.
`python3 pi/wipe_logger.py --run stall --fwd-s 2 --rev-s 0 --wipes 3 --max-stalls 3 --motor <part#>`

| # | Step | Pass | Result |
|---|---|---|---|
| 9.1 | Software cut-off fires | motor off within `--stall-ms` + 50 ms of the current crossing `--stall-amps` | not run |
| 9.2 | Stall current (median of 3) | record; feeds 8.3 | not run |
| 9.3 | Motor and gearbox after 3 stalls | still run freely (repeat 7.1) | not run |

Defaults: `--stall-amps 0.25`, `--stall-ms 150`. They fit a Pololu #4791 at 5 V (stall ≈ 0.30 A if stall current
scales with voltage from Pololu's 0.36 A at 6 V; an estimate). Nick changes them for another motor.

## Stage 10 (PROPOSED, needs QE spec PASS, then Nick's OK): repeat for the candidate method

Pre-registered here before any run (QE 019, B1 and spec reviews). Drafted by the builder; the bar numbers are the
builder's proposal from the 11/15 runs, **not a stated product need**: Nick confirms or sets them. Against this bar
the 11/15 restarted simple runs would pass 10.1 and 10.2 together in 1 of 5.

- **Question:** does the simple method (pot streamed during moves, learned brake leads saved across restarts) land
  0 -> +90 -> 0 deg wipes within the bar, run after run, from a fresh restart, on the rigid mount?
- **What a pass allows:** "on this rig, motor and board, the simple method met the bar". It is not a recommendation
  for other units, motors or boards (n = 1 rig), and not a production qualification.
- **Run conditions (every run):** sweep **+90 deg** from the start angle and back; speed **100 %**; **0.3 s** rest at
  each end; method simple; the rigid printed mount (not hand-held); the 1000:1 motor (Nick records its Pololu part
  number in each run); real brush on the raised surface; cam02 with the services in `services_disabled_2026-11-15.txt`
  stopped; current cut-off **0.5 A / 250 ms** (the default; nobody changes it during the stage).
- **Error of a stroke:** the pot angle 0.25 s after the brake (0.1 s brake + 0.15 s settle) minus the target, in pot
  degrees at the hand-calibrated scale **98.3 deg/V** (about +-10 %, `pot.DEFAULT_CAL`, cam02 11/13).
- **One sha:** the stage runs on the sha QE gives spec PASS (this card's head). `wipe_repeat.py --expect-sha` refuses
  any other; a run whose `meta.json` sha is another or "unknown" doesn't count.
- **Before stage 10:** a rewritten stage 9 (stall at the brush load) has run and its cut-off tripped, so the cut-off
  is known to work on this motor.
- **Order at the start:** (1) delete `~/NereusData/UrchinCam/motion_cal.json` on the Pi, (2) `sudo systemctl restart
  jog-bench`, (3) one **training run of 5 wipes** with the tool (`--out .._stage10_train`): recorded, not scored,
  and its `meta.json` must show no learned leads (`leads` empty). It stands in for commissioning: a unit would carry
  its learned leads from then on. Runs 1-3 and the restart sets then use what the earlier runs taught. This is decided
  before the data: run 1's first stroke counts against 10.1 like any other.
- **Start move:** after each restart the tool sends the arm to **0 deg** (`go to 0` at 100 %; the bench may then
  correct up to 3 times at <= 75 %), records that move in `meta.json` (`start_move`, `start_angle`), and refuses to
  start unless the arm stopped within **3 deg** of 0. No other move is allowed between a restart and stroke 1. The
  wipes are relative to where the arm stopped.
- **Independent check (10.4):** before the training run, with the arm at 0 deg after its start move, Nick draws the
  mark on the mount (a line on the arm against a line on the housing). After each of runs 1-3 and after the last
  restart set, the arm is sent to 0 the same way and Nick reads the arm against the mark by eye with a printed
  protractor scale; the tool asks for the reading at the end of the run (or `--zero-check-deg`) and writes it to
  `summary.json` `zero_check_deg`.
- **Tool:** `bringup/tools/wipe_repeat.py --host <pi>:8765 --motor "<part #>" --expect-sha <sha> --wipes <n> --out
  <dir>`; its defaults are the run conditions above. It writes meta.json, moves.json, events.json (every bench event
  in the run), samples.csv, loop.json and summary.json. Restart `jog-bench` before each run.
- **n:** 3 runs x 30 wipes for 10.1, plus **10 restarts x 3 wipes** for 10.2 (stroke 1 of each scored). Ten clean
  first strokes show the miss rate is under about 26 % (95 %: 0.74^10 = 0.05); showing under 1 in 10 would take 29
  clean restarts (0.9^29 = 0.05). Ten is chosen because 4 of 5 restarts missed on 11/15: ten clean is a clear change
  from that, not proof of a low rate.
- **Cost:** about 50 min of Nick's bench time (mount, training run, 3 runs of ~1 min, 10 restarts, 4 zero readings),
  no purchases.
- **Stop rule:** stop at the first failed run or restart set and record it; no retries to get a pass.

| # | Step | Pass | Result |
|---|---|---|---|
| 10.1 | 3 runs x 30 wipes (60 strokes each), each after a restart | each run: at least 58 of 60 strokes within 3 deg (`summary.json` `within_bar`); no stroke over 5 deg (`over_worst` 0) | |
| 10.2 | 10 restarts x 3 wipes: first stroke after each restart | `first_stroke_deg` within 3 deg in all 10 | |
| 10.3 | Safety, every run | `summary.json`: `complete` true (`strokes` = `strokes_expected`), `stops` empty (no cut-off, no motion, heartbeat, pot, track end or cancelled move in `events.json`), `cutoff_ok` true (0.5 A / 250 ms before and after); `throttled_before` and `throttled_after` 0x0 | |
| 10.4 | Independent zero check after runs 1-3 and the last restart set | `zero_check_deg` within +-3 deg | |
| 10.5 | Energy per wipe | record median and min-max per run (no bar) | |
| 10.6 | Control-loop pauses, `loop.json` per run | record (no bar) | |

10.1-10.4 are the bars; 10.5 and 10.6 are recorded only.

## Outputs

Each logger run writes `~/NereusData/UrchinCam/<YYYY-MM-DD>_<run>/` on the Pi: `samples.csv`, `summary.json`,
`MANIFEST.md`. Copy the folder to the Mac's `~/NereusData/UrchinCam/`, then
`python3 pi/plot_run.py <run folder>` writes `plot.html` next to it. Nothing from a run goes into git.
