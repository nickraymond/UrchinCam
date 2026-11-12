# Expander board bring-up and wiper power bench

The UrchinCam expander board (`UrchinCam_Expander_PCBA_01`) used as the wiper power bench (E2/E3).

| File | What |
|---|---|
| [TEST_CARD_board_bringup.md](TEST_CARD_board_bringup.md) | Pre-registered test card: numbered steps with pass bars, stages 0–9 |
| [VISEN_SCALE.md](VISEN_SCALE.md) | Motor current-sense math with datasheet pages, ADC setting, flags and proposals |
| [pi/board_check.py](pi/board_check.py) | I2C scan, ADC read, LED test, motor driver truth table with J3 open |
| [pi/eth_check.sh](pi/eth_check.sh) | RTL8152B Ethernet: USB ID, driver, link, DHCP, ping, iperf3, soak |
| [pi/wipe_logger.py](pi/wipe_logger.py) | Drives wipes, logs current at up to 860 S/s, energy per wipe, stall current, torque margin |
| [pi/plot_run.py](pi/plot_run.py) | `plot.html` for one run folder (self-contained, light and dark) |
| [pi/wipe_analysis.py](pi/wipe_analysis.py), [pi/urchin_hw.py](pi/urchin_hw.py) | Math and hardware layers |

On the Pi: Python 3 standard library, plus gpiozero or `pinctrl` for GPIO (both come with Raspberry Pi OS), and
`i2c-tools` for the scan. Copy the `bringup/` folder to the Pi and run from inside it.

Desk tests (no hardware), from the repo root:

    python3 -m unittest discover -s bringup/tests -v

Run outputs go to `~/NereusData/UrchinCam/<YYYY-MM-DD>_<run>/`, never into git.
