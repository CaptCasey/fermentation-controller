# Fermentation controller (Python side)

## Files
| File | Purpose |
|---|---|
| `config.json` | Pins, sensor IDs, all thresholds. Edit this, not the code. |
| `control.py` | Pure control logic (hysteresis, safety, compressor protection). No I/O. |
| `hardware.py` | Real Pi GPIO + DS18B20 reading, and a thermal simulator. |
| `profiles.py` | Pure profile logic: validation, step math, run advancement. |
| `runner.py` | Turns "run profile X on fermentor f1" into setpoints; saves progress. |
| `main.py` | The loop: command.json -> profiles -> control -> outputs + status.json. |
| `util.py` | Atomic JSON write / safe read helpers. |
| `profiles.example.json` | Starter profiles (copied to `data/profiles.json` on first run). |
| `sim.py` | Control-loop tests in simulated time. |
| `test_profiles.py` | Profile-engine tests, including a closed loop against the simulator. |

Run both test files after any change: `python3 sim.py` and `python3 test_profiles.py`.

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

DS18B20 wiring: all four share one bus (3.3V, GND, DATA), with a single 4.7k
resistor between DATA and 3.3V.

## First run on your computer (no hardware)
```
python3 sim.py              # control-loop scenarios
python3 test_profiles.py    # profile-engine tests
python3 main.py --sim       # the real loop against the simulator (real time)
```
Then edit `data/command.json` while it runs and watch `data/status.json` change.

## On the Pi
1. Add `dtoverlay=w1-gpio` to `/boot/firmware/config.txt`, reboot.
2. Wire the sensors, then run `python3 hardware.py --list-sensors`.
3. Paste each `28-xxxxxxxx` ID into `config.json` (label the physical sensors!).
4. Test SSRs one at a time with nothing mains-connected first.
5. `python3 main.py`

## Contract with the Node UI (all in `data/`)

### `command.json` (Node writes atomically: temp file + rename)
```json
{
  "fermentors": {
    "f1": { "mode": "profile", "profile": "ale-standard",
            "run_id": "2026-10-21T20:15:00", "paused": false },
    "f2": { "mode": "auto", "setpoint_f": 62.0 }
  },
  "chiller": { "enabled": true, "setpoint_f": 28.0 }
}
```
Fermentor modes: `off`, `auto` (hold a fixed setpoint), `profile` (follow a profile).

Profile fields:
- `run_id`: any unique string. **A new value starts a new run; the same value keeps
  the current run going**, so the UI can rewrite this file freely without
  restarting anything. Generate a fresh one (e.g. a timestamp) on "Start".
- `paused`: `true` freezes the profile clock and holds the current setpoint.
- `start_step`: optional, zero-based, begin partway through the profile.
- Switching a fermentor to `auto` or `off` cancels its run.

### `profiles.json` (the profile editor writes this)
```json
{
  "ale-standard": {
    "name": "Standard Ale",
    "steps": [
      { "type": "hold", "temp_f": 60, "duration_h": 12 },
      { "type": "ramp", "to_f": 72, "step_f": 1, "every_h": 1 },
      { "type": "hold", "temp_f": 72, "duration_h": 336 },
      { "type": "hold", "temp_f": 35, "duration_h": null }
    ]
  }
}
```
Step types:
- **hold**: `temp_f`, `duration_h` (`null` = hold until stopped, last step only).
  Optional `wait_for_temp: true` (+ `tolerance_f`, default 1.0): the timer only
  starts once the beer is actually at temperature.
- **ramp** (staircase): `to_f`, `step_f`, `every_h`. Changes by `step_f` at the end
  of each `every_h`, landing exactly on `to_f`.
- **ramp** (smooth): `to_f`, `rate_f_per_h`.

A ramp starts from where the previous step ended (or from the actual beer
temperature if it is the first step). Cooling "to 35" is just a hold at 35.
When a profile finishes, the controller keeps holding the last setpoint.

Invalid profiles are rejected with a readable reason (shown in `status.json`),
and the UI should validate too. Editing a profile while a run is using it takes
effect immediately, so duplicate it first if you want to experiment.

### `status.json` (Python writes every tick)
Temps, setpoints, per-fermentor state (`idle`, `cooling`, `heating`,
`waiting_for_glycol`, `fault`, `off`), chiller state, output states, `alarms`,
and a timestamp. For profile runs, `profiles.runs.<fermentor>` has: `status`
(`running`, `paused`, `waiting_for_temp`, `complete`, `error`), current step and
label, time elapsed/remaining in the step, total time remaining (`null` if the
profile ends in a hold-forever step), and the current setpoint.
`profiles.resumed_after_gap_s` shows how long the controller was down, if it was.
If `ts` is stale, the controller is not running, so the UI should show that prominently.

## Power loss and restarts
- `command.json` and `profiles.json` stay on disk, so setpoints and profiles
  survive a reboot.
- Profile progress is saved to `runs.json` on every step change and once a minute,
  and on clean shutdown. After a restart the run resumes from the last save.
- By default, **downtime is not counted**: the profile resumes where it left off,
  and the outage length is reported. Set `"count_downtime": true` in the
  `profile` section of `config.json` to make the profile clock keep running through
  outages (capped by `max_gap_credit_s`, default 24 h). This relies on the Pi's
  clock being right, which needs network time or an RTC module.

## Safety behavior built in
- Heat and cool can never be on together; 5 min lockout when switching between them.
- Pump only runs if glycol is at least 2F colder than the beer.
- Sensor missing, invalid, or stale (60 s): that fermentor's outputs turn off and an alarm is raised.
- Glycol sensor fault: chiller and cooling turn off.
- Chiller: min 5 min off, min 3 min on, 5 min wait after boot, hard cutoff at 24F glycol.
- Fermentor hard limits (30F / 90F) regardless of setpoint or profile.
- Any exit or crash of `main.py` turns every output off.

These are software protections only. Add a hardware backstop (a mechanical
thermostat or high-limit on the heat blankets) before running unattended.
