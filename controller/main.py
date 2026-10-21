"""Fermentation controller main loop.

    python3 main.py            # on the Pi (real hardware)
    python3 main.py --sim      # on any computer (simulated fermentors)

Contract with the Node UI (all in data/):
    command.json  Node writes (atomically), Python reads every tick.
    status.json   Python writes (atomically) every tick, Node reads.
    temp_log.csv  Python appends one row per log_interval_s (for graphs).
"""
import argparse
import csv
import json
import os
import signal
import time
from datetime import datetime

from control import Controller

DEFAULT_COMMAND = {
    "fermentors": {},          # nothing set => every fermentor is "off"
    "chiller": {"enabled": False, "setpoint_f": 28.0},
}


def write_json_atomic(path: str, obj: dict):
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(obj, fh, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)  # atomic: readers never see a half-written file


def load_command(path: str, last_good: dict) -> dict:
    """Read command.json; on any problem keep using the last good command."""
    try:
        with open(path) as fh:
            cmd = json.load(fh)
        if not isinstance(cmd, dict):
            raise ValueError("command.json must be an object")
        return cmd
    except FileNotFoundError:
        return last_good
    except (OSError, ValueError) as exc:
        print(f"[warn] bad command.json, keeping previous command: {exc}")
        return last_good


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--sim", action="store_true", help="use simulated hardware")
    args = ap.parse_args()

    with open(args.config) as fh:
        cfg = json.load(fh)

    data_dir = cfg["data_dir"]
    os.makedirs(data_dir, exist_ok=True)
    cmd_path = os.path.join(data_dir, "command.json")
    status_path = os.path.join(data_dir, "status.json")
    log_path = os.path.join(data_dir, "temp_log.csv")

    if args.sim:
        from hardware import SimHardware
        hw = SimHardware(cfg)
    else:
        from hardware import RealHardware
        hw = RealHardware(cfg)

    if not os.path.exists(cmd_path):
        write_json_atomic(cmd_path, DEFAULT_COMMAND)

    running = True

    def stop(*_):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    ctrl = Controller(cfg, time.monotonic())
    command = DEFAULT_COMMAND
    interval = cfg["loop_interval_s"]
    last_log = 0.0
    started = time.monotonic()
    last_tick = started

    try:
        while running:
            tick = time.monotonic()
            command = load_command(cmd_path, command)
            temps = hw.read_temps_f()
            outputs, status = ctrl.update(tick, temps, command)
            hw.set_outputs(outputs)

            status["ts"] = datetime.now().isoformat(timespec="seconds")
            status["uptime_s"] = int(tick - started)
            write_json_atomic(status_path, status)

            if tick - last_log >= cfg["log_interval_s"]:
                last_log = tick
                new = not os.path.exists(log_path)
                with open(log_path, "a", newline="") as fh:
                    w = csv.writer(fh)
                    cols = list(temps) + list(outputs)
                    if new:
                        w.writerow(["ts"] + cols)
                    w.writerow([status["ts"]]
                               + [None if temps.get(c) is None else round(temps[c], 2)
                                  for c in temps]
                               + [int(outputs[c]) for c in outputs])

            if args.sim:
                hw.advance(tick - last_tick)
            last_tick = tick

            time.sleep(max(0.0, interval - (time.monotonic() - tick)))
    finally:
        hw.all_off()  # whatever happens, leave everything off
        print("controller stopped, all outputs off")


if __name__ == "__main__":
    main()
