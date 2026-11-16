# Wiper bench: what we know (cam02, 2026-11-12 to 11-15)

The expander board on cam02 (Pi Zero 2 W) drives the wiper motor through the MP6550 and reads its current (VISEN on
ADS1115 AIN0) and the shaft angle (rotary pot on AIN2). Everything below was measured on **one board, one hand-held
rig, one motor**: the 1000:1 gearmotor on the wiper shaft (Nick, 11/15: "500mA is working well for my 1000:1
motor"; exact part number not recorded). Raw data is on the Mac under `~/NereusData/UrchinCam/` (cam02's own copy
mirrored to `from_cam02/`); each claim names its folder, or says where it was only printed to the session.

## Status: a candidate method, not a pass

Nick's stated preference (11/15): the **simple** method, because reliability, simplicity and consistent results
matter most in production. Simple = drive at the set speed toward the target and brake one learned lead before it
(lead time x current speed); no slow-down phase. Since 13:18 the pot is streamed during moves (~420 readings/s,
~0.7 deg of travel apart at 300 deg/s) and the current is read every 8th sample.

The data does **not** yet show it meets a production bar: the bar below was not written before the runs, each code
configuration has one 30-wipe run (n = 1) on a hand-held rig, and 4 of the 5 streamed-pot runs miss their first
wipe by 4-7 deg. A pre-registered repeat is proposed in the test card (stage 10): QE spec PASS, then Nick's OK.

### Every comparison run on 11/15

0 -> +90 -> 0 deg at 100 % speed, 0.3 s rest at each end, brush on the raised surface (0-90 deg), 1000:1 motor,
current cut-off at its default 0.5 A / 250 ms (settings aren't saved across restarts, and every run except 1247
followed one; a change on a page would not have been logged). Folders `~/NereusData/UrchinCam/2026-11-15_<time>_compare/` hold
`moves.json` and per-method `samples_*.csv`.

| folder | code | cam02 services | method | strokes | mean | median | p95 | max | within 3 deg | 1st stroke | J / wipe |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `_compare` (12:3x) | dced42d | all on | simple | 1 | (stopped: braked at 45 deg, see fixes) | | | | | -33.2 | |
| `_compare` (12:3x) | dced42d | all on | approach | 16 | 1.15 | 0.82 | 3.35 | 4.88 | 14 / 16 | -4.88 | 1.377 |
| `1242` | cf06a39 | all on | simple | 16 | 1.29 | 1.00 | 3.18 | 4.20 | 14 / 16 | -4.20 | 1.443 |
| `1242` | cf06a39 | all on | approach | 16 | 1.13 | 0.74 | 3.62 | 3.69 | 14 / 16 | -3.62 | 1.344 |
| `1247` | cf06a39 | all on | simple | 60 | 1.29 | 1.08 | 2.49 | 3.88 | 58 / 60 | -1.71 | 1.433 |
| `1247` | cf06a39 | all on | approach | 60 | 1.32 | 1.17 | 2.80 | 3.51 | 57 / 60 | +1.44 | 1.406 |
| `1318` | 44480cb | all on | simple, pot streamed | 60 | 0.95 | 0.70 | 2.84 | 3.95 | 58 / 60 | -3.95 | 1.471 |
| `1323` | c3555f7 | all on | simple | 60 | 1.12 | 0.86 | 2.50 | 7.02 | 57 / 60 | -7.02 | 1.456 |
| `1325` | 6254b8f | all on | simple | 60 | 1.16 | 1.05 | 2.48 | 6.96 | 58 / 60 | -6.96 | 1.442 |
| `1331` | 6254b8f | power_logger off | simple | 60 | 1.19 | 1.08 | 2.45 | 5.89 | 58 / 60 | -5.89 | 1.451 |
| `1331` | 6254b8f | power_logger off | approach | 60 | 1.25 | 0.90 | 2.89 | 6.49 | 58 / 60 | -5.19 | 1.369 |
| `1334` | 6254b8f | 11 services off | simple | 60 | 1.02 | 0.88 | 2.37 | 2.81 | 60 / 60 | -1.22 | 1.433 |
| `1334` | 6254b8f | 11 services off | approach | 60 | 0.93 | 0.46 | 3.00 | 7.80 | 57 / 60 | -7.80 | 1.341 |

Pooled simple strokes from the 30-wipe runs: 349 of 360 within 3 deg. Errors are in degrees. On the n = 16 rows
the p95 is the 15th of 16 values (interpolated it would be 3.73 / 3.44 / 3.64).

- **Lost run:** the first comparison (about 12:31, code 97c1458, sub-100 % PWM dropping out) wrote to
  `2026-11-15_compare/`, and the next run (12:3x, dced42d) overwrote its `moves.json`. Its per-move numbers quoted in
  the session (simple 1.50 J, approach 1.56 J per wipe) can't be recomputed; its samples survive in the session log
  `from_cam02/2026-11-15_jog_203215`. `bringup/tools/wipe_repeat.py` now refuses to write into an existing folder.
- **First stroke after a restart:** in 4 of the 5 streamed-pot simple runs the first stroke missed by 4-7 deg
  (the brake lead restarts untrained; `1247` ran without a restart and missed by 1.7). Leaving out the first wipe,
  1318-1334 range 57-58 of 58 within 3 deg, max 2.78-4.68 deg (diagnostic only, not a result). Since this PR the
  learned leads are saved on the Pi (`~/NereusData/UrchinCam/motion_cal.json`) and reloaded at start; not yet
  re-run on hardware.
- **How error is measured:** the same pot's angle 0.25 s after the brake (0.1 s brake + 0.15 s settle) minus the
  target. The pot scale is a hand calibration (98.3 deg/V, about +-10 %), so the error is in pot degrees; an
  external reference (a marked stop or a camera) has not been used.
- **Energy:** motor current x 5 V (assumed; no +5V divider fitted) over the move, corrections included; the Pi's
  own draw is not included.

The "approach" method (set speed until 25 deg out, then 60 % with a speed floor; per-direction and per-region learned
leads) is selectable on the Jog page as an experiment. In 1247 it never actually slowed down (~300 deg/s at the brake
for both methods, from the samples); its leads also start untrained (first strokes -5.19 and -7.80 deg).

### Power and energy, simple method (1334 run, 30 wipes = 60 strokes)

Motor only, 5 V assumed. Move time includes the 0.1 s brake and 0.15 s settle, where the motor draws ~0.

| quantity | forward stroke (0 -> 90) | reverse stroke (90 -> 0) | full wipe (out + back) |
|---|---|---|---|
| energy | 0.706 +- 0.030 J | 0.728 +- 0.030 J | 1.433 +- 0.046 J (1.358-1.532) = 0.398 mWh |
| move time | 0.563 s | 0.565 s | 1.128 s (~0.62 s of it driving) |
| average power over the move | 1.25 W | 1.29 W | 1.27 W |
| current while driving | | | median 390 mA, mean 408 mA |
| power while driving | | | median 1.95 W, mean 2.04 W |
| peak current (stroke start, < 250 ms) | 1254 mA max, 855 mA median | 1194 mA max, 870 mA median | 1.25 A max |

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
  the motor: 0.6 s strokes read 194-197 deg on the pot where Nick estimated ~180 deg by eye (printed to the session;
  samples in `from_cam02/2026-11-14_jog_050943`).
- It turns all the way round (no end stops). The track reads **0 to 3.35 V** over ~330 deg.
- **Dead zone** (~30 deg, inferred from 360 - 3.35 V x 98.3): past 3.35 V the wiper first **floats** (reads ~0.57 V,
  like an unconnected ADC input) and then sits on the **0 V end terminal** (reads 0.000 V). Both look like real track
  positions. Seen on 11/15 when a +90 deg wipe started at 2.58 V ran past the top (`2026-11-15_jog_*`): the out
  stroke "landed" at 106 deg and the return stopped as "moving away".
- What the software does about it (`pi/pot.py`):
  - moves stay inside **0.10-3.25 V**; a move or wipe whose target would leave it is refused, with the room left each
    way (Jog page shows it);
  - three states (PE 018's rulings (`~/NereusData/ops/pe_specs/RULINGS_urchincam_pr1_2026-11-15.md` @ 09a395d1c56a), ruling 1): **trusted** only while the gap is out of reach (600 deg/s x time
    since the last trusted reading + 3 deg is less than that reading's distance to the nearer track end), the reading
    is inside 0.10-3.25 V and its step is within that reach; **suspect** when the gap is out of reach but the reading
    fails a test (dropped; a later good reading trusts it again); **latched** when a reading arrives with the gap
    within reach, and at every start of the server (ruling 2). Only the operator's confirm leaves latched;
  - no turn counting: nothing drives through the gap, so the angle never crosses it.
- **Set-up rule:** mount the pot so the wiper's whole working sweep sits mid-track. For the 0-90 deg wipe the zero is
  at ~0.77 V on cam02 today: 243 deg of room forward, 66 deg back.
- Open: once (11/15) the pot reading jumped 21 deg in 10 ms while the motor was only starting to reverse, and stayed
  there. The motor can't turn the shaft that fast (~3.6 deg in 10 ms). Cause unknown (rotor slip on the shaft, or the
  filter). Every jog-log sample now records `pot_ok` so the next one can be traced.

## The app: jog bench on cam02

`pi/jog_server.py`, systemd unit `jog-bench` (starts at boot, restarts on failure). Pages on port 8765, on 127.0.0.1
and the Pi's Tailscale address:

| page | what |
|---|---|
| `/` Drive | hold or tap-to-latch forward / reverse, time-based wipe cycle, live current and angle plots, current cut-off |
| `/runs` Runs | tagged multi-cycle runs (motor, condition, stroke time, cycles), one folder each under `~/NereusData/UrchinCam/bench_runs/`, include/exclude checkbox per run, summary stats |
| `/jog` Jog | angle steps (1/5/15/45/90 deg or typed), speed 10-100 %, hold to jog, zero / go to 0, wipe by angle (sweep, cycles), method picker, current vs angle, per-move error / energy / time table |

API: `/stream` (live samples + status), `/cmd`, `/move`, `/settings`, `/pot/zero`, `/pot/flip`, `/pot/confirm`, `/api/runs`,
`/api/summary`, `/api/moves`, `/api/loop` (control-loop pauses with their cause).

Safety (each has a desk test): the motor stops on the current cut-off (default 0.5 A / 250 ms, editable), after the
max run time (holds), when no page heartbeat arrives for 1.5 s (also between cycles and between queued moves), on no
motion (moves: 0.6 s plus gap time; holds, timed wipe cycles and Runs: the same window while drawing >= 0.15 A), at
3 deg from either end of the pot's safe track during a hold, on a suspect or latched pot (moves, holds, timed cycles and
Runs; none of them starts on one either), on moving
away from the target, on a move timeout, on 20 failed ADC reads in a row, on no drive current below 100 % (PWM
re-created once first), and on any control-thread exception (bridge put to sleep, pages show Stopped). POSTs must be
JSON, to the bench's own address, from the same origin. LED1 shows green forward, red reverse.

The heartbeat is global: any open page that is driving or moving keeps it alive.

Limits of the no-motion stop (QE 019): it needs the pot (`--pot-every` > 0, the default) and a current of at least
0.15 A; below 0.15 A, or with no pot, a jammed shaft is driven for the whole stroke time of a timed cycle (up to 30 s)
and only the 0.5 A cut-off can stop it. The stall current at reduced duty is not measured (stage 9 not run). Only
trusted pot readings count as motion. A timed cycle or Run can no longer pass through the pot's gap: the pot latches
first and the cycle stops.

Limits of the pot's trust (ruling 1, each pinned by a desk test): (1) the confirm is the operator's word. Given with
the arm in the gap while the float sits still inside 0.10-3.25 V, it is accepted; a jam there is then stopped by the
no-motion stop only while the float holds within 5 mV, and once it wanders (20-30 mV, as recorded) only the 0.5 A
cut-off or the max run time stops it. (2) The proof assumes the arm never turns faster than 600 deg/s; a faster turn
by hand can carry it into the gap unseen. Near the ends the pot latches instead of guessing: on cam02 the pot was read
every 19 ms (median) with the motor asleep and every 10 ms while driving (`from_cam02/2026-11-15_jog_194426`), a reach
of about 14 and 9 deg, so an arm resting within about 14 deg (0.15 V) of a track end latches at the next reading and a
confirm there doesn't hold.

Getting back from the dead zone, and after every restart: the pages show the pot's state and what to do next. Put
the motor to sleep (STOP), turn the shaft by hand onto the track, well away from both ends, hold it still, and press
"The arm is on the track" (`/pot/confirm`). It is taken only with the bridge asleep, nothing running or queued, the
reading inside 0.10-3.25 V and every reading of the last 0.3 s within 5 mV; it keeps the zero and is logged as an
event. Zero here needs a trusted pot. When the service stops it puts the bridge to sleep and releases the pins; the MP6550's
internal pull-downs (datasheet p. 3) then hold IN1/IN2 low, so it can't drive (nSLEEP may still read high from the
Pi's default pull, as seen at boot: the bridge coasts). So `jog-bench.service` has no `ExecStopPost`.

## Things that bit us, and the fixes

| problem | cause (evidence) | fix |
|---|---|---|
| Pot read followed motor current | ADS1115 ignores OS=1 written in continuous mode; register kept VISEN, read at 4x scale (0.146 V = 36.6 mV x 4) | power down, then start (`urchin_hw.ADS1115.single_shot`) |
| 0 mA below 100 % speed | gpiozero device swaps + LED PWM: lgpio queues `tx_pwm` per GPIO and an endless entry never ends (29 dropouts in 25 trials) | `LgpioBackend`: pins claimed once, PWM stopped and idle before each change (0 in 25). Stress output printed to the session only; the test script was `/tmp/pwm_stress.py` on cam02 |
| Wipe stopped mid-move | 51 ms loop stall mis-stamped the next pot reading as a 14 deg jump in <1 ms | stamp readings at step start; re-trust a possible path |
| Brake lead grew to ~0.14 s | slow correction moves taught it (overshoot / low speed) | learn only from first strokes braked at >= 100 deg/s |
| Control-loop pauses 15-56 ms | `vcgencmd` subprocess and SD writes in the loop; 5 ms interpreter-lock switch; crash-looping services | polling + logging off-loop, 0.5 ms switch, shared status; services below |
| First stroke after a restart misses 4-7 deg | learned brake lead starts untrained (QE 019) | leads saved to `motion_cal.json` and reloaded (not yet re-run on hardware) |
| Stall under the cut-off could drive for minutes in cycle mode | no-motion stop existed only in moves (QE 019) | no-motion stop in every drive mode while drawing current |
| Any page on the tailnet could drive the motor | POSTs took any Origin and content type (QE 019) | JSON only, allowed Host, same origin |
| Floating wiper trusted as a turning shaft (29d446c) | the floating wiper drifts 20-30 mV one way on its own (jog_194426 t=908.8: 0.591, 0.598, 0.619 V; QE 019 N2) | ruling 1: the re-trust paths and `FLOAT_V` are gone; trust is kept only while the gap is provably out of reach and comes back only on the operator's confirm. QE's recorded readings (t=1166.99-1168.80, with the run-up) are never trusted once the arm nears the top end (desk test a). Limits above |
| Jam in the dead zone never stopped (29d446c) | the float restarted the 5 mV no-motion timer on 189 of 217 reads while driving (QE 019 N3) | only trusted pot changes count as motion, and since ruling 1 nothing drives on an untrusted pot, so a cycle stops as the pot latches near the gap. A start in the gap is fixed by ruling 2: the server starts latched and refuses moves, holds, cycles and Runs until the operator confirms (desk test b) |

## cam02 is a bench now, not a camera

Stopped and disabled on 11/15 with Nick's OK in the builder chat ("you can terminate any other services on this
rig"): power_logger, camstream, cam-control (all three crash-looping every 3-6 s), cam-status, mediamtx,
nereus_power_dashboard, lifepo4wered-daemon, bluetooth, ModemManager, avahi-daemon, triggerhappy. List and one-line
restore command: cam02 `~/NereusData/UrchinCam/services_disabled_2026-11-15.txt` (copy in `2026-11-15_1334_compare/`).
The system journal is kept across reboots (`/etc/systemd/journald.conf.d/90-persistent.conf`).

Control-loop pauses over 15 ms, read from `/api/loop` after each run and printed to the session only (not saved):
1323: 25 (longest 31 ms); 1325: 13 (32 ms); 1331: 21 (37 ms); 1334: 10 (56 ms). These counts cover the whole server
session, positioning moves and idle time included, not just the 30 wipes; my "5 during moves" for 1334 does not
reproduce: QE 019 found 3 in that run's window in the later saved snapshot
(`2026-11-15_1334_compare/api_loop_after_run.json`, which also covers an hour of idle time and an `apt install` with a
1.17 s gap), none in a drive segment. Stage 10 saves `loop.json` per run.

## Motor and load (same rig)

- Free shaft, 5 V: 260:1 78-97 mA (`from_cam02/2026-11-13_motor_free_260`); 1000:1 90-133 mA
  (`_motor_free_1000`) and 102-142 mA after Nick cleared a rub (`_motor_free_1000_r2`) (11/13). Reverse draws more than forward on both; Nick's meter read 4.92-4.94 V at
  the motor in both directions (chat, 11/13, `_vcheck_1000` holds the current), so it's the motor/gearbox.
- On the shaft with the seal, 1000:1 (`2026-11-15_char/results.json`, one 0.3 s pulse per duty per direction):
  speed 50 % ~30 deg/s, 60 % ~96-115, 80 % ~185-206, 100 % ~351-374 deg/s; coast after the brake 0.3 deg (50 %) to
  ~9 deg (100 %). The 90 % forward row reads speed 0.0 / moved -18.7 deg: a measurement glitch, left out. "Doesn't
  turn below ~45 %" is an extrapolation from the 50 % row, not a measured point. The run before this one read 0 mA
  below 100 % (the PWM bug), so it was discarded.
- Brush on the surface (0-90 deg): median current forward ~450 mA at 20-40 deg, reverse ~475 mA at 60-70 deg,
  ~310-380 mA elsewhere; stroke-start peaks 1.2-1.5 A for < 250 ms. ~1.43 J per 0-90-0 wipe.
- Motor part numbers still unknown (Nick: treat as 6 V versions).

## Open

1. The pre-registered repeat (test card stage 10, Nick's OK first), on the rigid printed mount: how much of the
   ~1.1 deg scatter is mechanical, and whether saved leads fix the first stroke.
2. The idle pot read (between moves) sometimes stalls up to 56 ms; moves use the streamed path and aren't affected.
3. The 21 deg pot jump above.
4. Stall test (card stage 9) not run yet; motor part numbers and stall current unknown. Ethernet (stage 5) 5.1-5.7
   passed 11/15 (test card; 5.7 run differently); 5.8 (does the random MAC change at boot) is PROPOSED, not run.
5. Slow cycles: at 50 % the seal turns the shaft only ~30 deg/s (char run above). No cycle passes through the gap
   now (ruling 1), so this matters only for the no-motion window on the track.
6. Angle error has no external reference (a marked stop or a camera) yet.
8. Short `go to` moves miss: from -1.28 deg, `go to 0` ended at +3.93 after 3 corrections (11/15 5:16 PM,
   `2026-11-15_eth_soak_wipes_run2/meta.json`); the end-of-run `go to 0` from 4.5 deg ended at -1.27 after 3. A
   correction from rest at 75 % overshoots a 1-4 deg target. Stage 10's start gate (3 deg) refused that run.
7. The floating wiper (QE 019 N2): in the gap the wiper floats anywhere from 0.08 to 1.22 V (QE 019) and drifts
   slowly (t=1168, motor asleep: 0.55 V down to 0.08 V), overlapping real track voltages at both ends. Handled in
   software by ruling 1 (latch, operator's confirm); limits above. The pull-down (100 kOhm, TP6 to TP2) is ruled the
   right direction for the board but not this PR: it needs its own registered stage, QE's spec verdict and Nick's OK
   before any run with it fitted, and every calibration and zero from before the fit is void until re-measured.
