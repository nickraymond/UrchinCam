# Wiper bench: what we know (cam02, 2026-11-12 to 11-15)

The expander board on cam02 (Pi Zero 2 W) drives the wiper motor through the MP6550 and reads its current (VISEN on
ADS1115 AIN0) and the shaft angle (rotary pot on AIN2). Everything below was measured on that one board and one
hand-held rig (n = 1 rig, no rigid mount yet). Raw data: `~/NereusData/UrchinCam/` on the Mac and on cam02; each
section names its folder.

## Production pick: the simple method, with the pot streamed during moves

Drive at full speed toward the target, brake one learned lead before it (lead time x current speed), no slow-down
phase. The pot is streamed during moves (~420 readings/s, ~0.7 deg of travel apart at 300 deg/s); the current is
read every 8th sample. Nick (11/15): reliability, simplicity and consistent results matter most in production.

30 wipes each, 0 -> +90 -> 0 deg at 100 %, brush on the raised surface (`2026-11-15_*_compare/SUMMARY.md`):

| run | mean | median | p95 | max | sd | within 1 / 2 / 3 deg | J / wipe | s / wipe |
|---|---|---|---|---|---|---|---|---|
| 12:47 simple, pot read every ~10 ms | 1.29 | 1.08 | 2.49 | 3.88 | 1.56 | 26 / 45 / 58 of 60 | 1.433 | 1.13 |
| 13:18 simple, pot streamed | 0.95 | 0.70 | 2.84 | 3.95 | 1.26 | 39 / 53 / 58 | 1.471 | 1.14 |
| **13:34 simple, control-loop pauses removed** | **1.02** | **0.88** | **2.37** | **2.81** | **1.14** | 32 / 49 / **60** | 1.433 | 1.13 |
| 13:34 approach (see below), same conditions | 0.93 | 0.46 | 3.00 | 7.80 | 1.58 | 44 / 54 / 57 | 1.341 | 1.20 |

Energy is motor current x 5 V (assumed: no +5V divider fitted) over the move, corrections included.

The "approach" method (set speed until 25 deg out, then 60 % with a speed floor, per-direction and per-region learned
leads) is still selectable on the Jog page. Its median is lower but its tail is worse (its leads start untrained), and
in the first 30-wipe run it never actually slowed down (~300 deg/s at the brake, the same as simple). It stays an
experiment, not the production method.

### Power and energy, simple method (13:34 run, 30 wipes = 60 strokes)

Motor only (VISEN), 5 V assumed (no +5V divider fitted), so power = current x 5 V; the Pi's own draw is not included.
Move time includes the 0.1 s brake and 0.15 s settle, where the motor draws ~0.

| quantity | forward stroke (0 -> 90) | reverse stroke (90 -> 0) | full wipe (out + back) |
|---|---|---|---|
| energy | 0.706 +- 0.030 J | 0.728 +- 0.030 J | **1.433 +- 0.046 J** (1.358-1.532) = 0.398 mWh |
| move time | 0.563 s | 0.565 s | 1.128 s (~0.62 s of it driving) |
| average power over the move | 1.25 W | 1.29 W | 1.27 W |
| current while driving | | | median 390 mA, mean 408 mA |
| power while driving | | | median 1.95 W, mean 2.04 W |
| peak current (stroke start, < 250 ms) | 1254 mA max, 855 mA median | 1194 mA max, 870 mA median | 1.25 A max (6.3 W) |

| wipes per day | energy | at 5 V |
|---|---|---|
| 10 | 14.3 J = 3.98 mWh | 0.80 mAh |
| 100 | 143 J = 39.8 mWh | 7.96 mAh |
| 1000 | 1433 J = 398 mWh | 79.6 mAh |

## The pot (AIN2 / TP6)

- Wiring: wiper = **white** -> TP6 (AIN2); ends **green** -> TP3 (+3V3), **blue** -> TP2 (GND). Found with a meter:
  green-blue stays ~4.96 kOhm while turning; white sweeps between them. Value ~5 kOhm (body code 502); make not
  confirmed (logo looks like Amphenol Piher).
- Scale: **98.3 deg/V** (11 hand turns of ~90 deg, median 0.916 V, range 0.82-1.07 V, about +-10 %). Check against
  the motor: 0.6 s strokes read 194-197 deg on the pot where Nick estimated ~180 deg by eye.
- It turns all the way round (no end stops). The track reads **0 to 3.35 V** over ~330 deg.
- **Dead zone** (~30 deg, inferred from 360 - 3.35 V x 98.3): past 3.35 V the wiper first **floats** (reads ~0.57 V,
  like an unconnected ADC input) and then sits on the **0 V end terminal** (reads 0.000 V). Both look like real track
  positions. Seen on 11/15 when a +90 deg wipe started at 2.58 V ran past the top (`2026-11-15_jog_*`): the out
  stroke "landed" at 106 deg and the return stopped as "moving away".
- What the software does about it (`pi/pot.py`):
  - moves stay inside **0.10-3.25 V**; a move or wipe whose target would leave it is refused, with the room left each
    way (Jog page shows it);
  - a reading that jumps further than the shaft can turn (600 deg/s + 3 deg) is held as untrusted; trusted again when
    three readings agree near a track end, or (mid-track) form a possible path;
  - the angle counts turns across the gap (unwrapped), so the readout is 0-360 with the total turned under it.
- **Set-up rule:** mount the pot so the wiper's whole working sweep sits mid-track. For the 0-90 deg wipe the zero is
  at ~0.77 V on cam02 today: 243 deg of room forward, 66 deg back.
- Open: once (11/15) the pot reading jumped 21 deg in 10 ms while the motor was only starting to reverse, and stayed
  there. The motor can't turn the shaft that fast (~3.6 deg in 10 ms). Cause unknown (rotor slip on the shaft, or the
  filter). Every jog-log sample now records `pot_ok` so the next one can be traced.

## The app: jog bench on cam02

`pi/jog_server.py`, systemd unit `jog-bench` (starts at boot, restarts on failure). Pages on port 8765, on 127.0.0.1
and the Pi's Tailscale address (`http://100.69.129.59:8765`):

| page | what |
|---|---|
| `/` Drive | hold or tap-to-latch forward / reverse, time-based wipe cycle, live current and angle plots, current cut-off |
| `/runs` Runs | tagged multi-cycle runs (motor, condition, stroke time, cycles), one folder each under `~/NereusData/UrchinCam/bench_runs/`, include/exclude checkbox per run, summary stats |
| `/jog` Jog | angle steps (1/5/15/45/90 deg or typed), speed 10-100 %, hold to jog, zero / go to 0, wipe by angle (sweep, cycles), method picker, current vs angle, per-move error / energy / time table |

API: `/stream` (live samples + status), `/cmd`, `/move`, `/settings`, `/pot/zero`, `/pot/flip`, `/api/runs`,
`/api/summary`, `/api/moves`, `/api/loop` (control-loop pauses with their cause).

Safety: the motor stops on the current cut-off (default 0.5 A / 250 ms, editable), after the max run time, when no
page heartbeat arrives for 1.5 s, on no motion (0.6 s plus gap time), on moving away from the target, on a move
timeout, on 20 failed ADC reads in a row, on no drive current below 100 % (PWM re-created once first), and on any
control-thread exception (bridge put to sleep). LED1 shows green forward, red reverse.

## Things that bit us, and the fixes

| problem | cause (evidence) | fix |
|---|---|---|
| Pot read followed motor current | ADS1115 ignores OS=1 written in continuous mode; register kept VISEN, read at 4x scale (0.146 V = 36.6 mV x 4) | power down, then start (`urchin_hw.ADS1115.single_shot`) |
| 0 mA below 100 % speed | gpiozero device swaps + LED PWM: lgpio queues `tx_pwm` per GPIO and an endless entry never ends (29 dropouts in 25 trials) | `LgpioBackend`: pins claimed once, PWM stopped and idle before each change (0 in 25) |
| Wipe stopped mid-move | 51 ms loop stall mis-stamped the next pot reading as a 14 deg jump in <1 ms | stamp readings at step start; re-trust a possible path |
| Brake lead grew to ~0.14 s | slow correction moves taught it (overshoot / low speed) | learn only from first strokes braked at >= 100 deg/s |
| Control-loop pauses 15-56 ms | `vcgencmd` subprocess and SD writes in the loop; 5 ms interpreter-lock switch; crash-looping services | polling + logging off-loop, 0.5 ms switch, shared status; services below |

## cam02 is a bench now, not a camera

Stopped and disabled on 11/15 with Nick's OK ("you can terminate any other services on this rig"): power_logger,
camstream, cam-control (all three crash-looping every 3-6 s), cam-status, mediamtx, nereus_power_dashboard,
lifepo4wered-daemon, bluetooth, ModemManager, avahi-daemon, triggerhappy. List and one-line restore command:
cam02 `~/NereusData/UrchinCam/services_disabled_2026-11-15.txt`. Pauses over 15 ms per 30-wipe run: 25 -> 10
(5 during moves, 11-18 ms). The system journal is now kept across reboots (`/etc/systemd/journald.conf.d/90-persistent.conf`).

## Motor and load (same rig)

- Free shaft, 5 V: 260:1 78-97 mA, 1000:1 90-133 mA (11/13). Reverse draws more than forward on both; the driver gives
  4.92-4.94 V either way, so it's the motor/gearbox.
- On the shaft with the seal: speed 50 % ~30 deg/s, 60 % ~105, 80 % ~195, 100 % ~360 deg/s; below ~45 % it doesn't
  turn; coast after the brake 0.3 deg (50 %) to ~9 deg (100 %) (`2026-11-15_char/`).
- Brush on the surface (0-90 deg): median current forward ~450 mA at 20-40 deg, reverse ~475 mA at 60-70 deg,
  ~310-380 mA elsewhere; stroke-start peaks 1.2-1.5 A for < 250 ms. ~1.43 J per 0-90-0 wipe.
- Motor part numbers still unknown (Nick: treat as 6 V versions).

## Open

1. Repeat the 30-wipe run on the rigid printed mount (how much of the ~1.1 deg scatter is mechanical).
2. The idle pot read (between moves) sometimes stalls up to 56 ms; moves use the streamed path and aren't affected.
3. The 21 deg pot jump above.
4. Stall test (card stage 9) and Ethernet (stage 5) not run yet.
