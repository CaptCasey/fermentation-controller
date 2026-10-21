# Fermentation controller (Python side)

## Files
| File | Purpose |
|---|---|
| `config.json` | Pins, sensor IDs, all thresholds. Edit this, not the code. |
| `control.py` | Pure control logic (hysteresis, safety, compressor protection). No I/O. |
| `hardware.py` | Real Pi GPIO + DS18B20 reading, and a thermal simulator. |
| `main.py` | The loop: read `command.json` -> control -> write outputs + `status.json`. |
| `sim.py` | Fast simulated-time tests. Run these after any change to `control.py`. |

## GPIO assignments (BCM numbering)
| Function | BCM GPIO | Physical pin |
|---|---|---|
| DS18B20 data bus (all 4 sensors, in parallel) | GPIO 4 | 7 |
| Fermentor 1 pump SSR | GPIO 17 | 11 |
| Fermentor 2 pump SSR | GPIO 27 | 13 |
| Fermentor 1 heat blanket SSR | GPIO 22 | 15 |
| Fermentor 2 heat blanket SSR | GPIO 23 | 16 |
| Chiller (AC unit) SSR | GPIO 24 | 18 |
| SSR input grounds | GND | 9, 14, 20 |

GPIO 4 is fixed by the 1-Wire driver. The others avoid the I2C/UART pins, so you
can still add a display, buttons, or an I2C device later.

DS18B20 wiring: all four share one bus (3.3V, GND, DATA), with a single 4.7k
resistor between DATA and 3.3V.

## First run on your computer (no hardware)
```
python3 sim.py            # automated scenarios, finishes in seconds
python3 main.py --sim     # runs the real loop against the simulator (real time)
```
Then edit `data/command.json` while it runs and watch `data/status.json` change.

## On the Pi
1. Add `dtoverlay=w1-gpio` to `/boot/firmware/config.txt`, reboot.
2. Wire the sensors, then run `python3 hardware.py --list-sensors`.
3. Paste each `28-xxxxxxxx` ID into `config.json` (label the physical sensors!).
4. Test SSRs one at a time with nothing mains-connected first.
5. `python3 main.py`

## Contract with the Node UI (in `data/`)
`command.json` (Node writes, atomically via temp file + rename):
```json
{
  "fermentors": {
    "f1": { "mode": "auto", "setpoint_f": 62.0 },
    "f2": { "mode": "off" }
  },
  "chiller": { "enabled": true, "setpoint_f": 28.0 }
}
```
`status.json` (Python writes every tick): temps, setpoints, per-fermentor state
(`idle`, `cooling`, `heating`, `waiting_for_glycol`, `fault`, `off`), chiller
state, output states, active `alarms`, and a timestamp. If `ts` is stale, the
controller is not running, so the UI should show that prominently.

`command.json` stays on disk, so after a power loss the controller resumes the
last setpoints on boot. (Profile progress will need its own saved state, which
comes with the profile engine.)

## Safety behavior built in
- Heat and cool can never be on together; 5 min lockout when switching between them.
- Pump only runs if glycol is at least 2F colder than the beer.
- Sensor missing, invalid, or stale (60 s): that fermentor's outputs turn off and an alarm is raised.
- Glycol sensor fault: chiller and cooling turn off.
- Chiller: min 5 min off, min 3 min on, 5 min wait after boot, hard cutoff at 24F glycol.
- Fermentor hard limits (30F / 90F) regardless of setpoint.
- Any exit or crash of `main.py` turns every output off.

These are software protections only. Add a hardware backstop (a mechanical
thermostat or high-limit on the heat blankets) before running unattended.
