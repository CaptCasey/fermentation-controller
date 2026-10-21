"""Profile runner: turns "run profile X on fermentor f1" into setpoints.

Sits between command.json and the controller. Each tick, main.py calls
resolve(); fermentors in mode "profile" come back as ordinary
{"mode": "auto", "setpoint_f": ...} commands, everything else passes through.

command.json entry for a profile run:
    "f1": {"mode": "profile", "profile": "ale-standard",
           "run_id": "2026-10-21T20:15:00",     # new value = start a new run
           "paused": false,                      # true = freeze the clock + setpoint
           "start_step": 0}                      # optional: begin partway through

Because command.json is desired-state and run_id is an idempotent token, the
UI can rewrite the same command forever without restarting the run.

Progress is saved to data/runs.json so a restart resumes where it left off.
"""
import os
import time

from profiles import (advance, current_setpoint, describe, new_run,
                      validate_profile)
from util import read_json, write_json_atomic

MAX_DT = 300.0   # never credit more than this to a single tick


class ProfileRunner:
    def __init__(self, cfg: dict, data_dir: str, wall=time.time):
        pc = cfg.get("profile", {})
        self.profiles_path = os.path.join(data_dir, "profiles.json")
        self.runs_path = os.path.join(data_dir, "runs.json")
        self.wall = wall
        self.count_downtime = bool(pc.get("count_downtime", False))
        self.max_gap_credit_s = float(pc.get("max_gap_credit_s", 86400))
        self.save_interval_s = float(pc.get("save_interval_s", 60))
        self.min_f = cfg["fermentor"]["safe_min_f"]
        self.max_f = cfg["fermentor"]["safe_max_f"]

        self.profiles = {}
        self.profile_errors = {}
        self._mtime = None

        self.runs = {}
        self.gap_s = None            # how long we were down (if we can tell)
        self._gap_credit = 0.0
        self._gap_pending = set()

        self._last_now = None
        self._last_save = None
        self._save_now = False
        self._load_runs()

    # ------------------------------------------------------------ loading
    def _load_runs(self):
        data = read_json(self.runs_path, None)
        if not isinstance(data, dict) or not isinstance(data.get("runs"), dict):
            return
        self.runs = data["runs"]
        saved = data.get("saved_wall")
        if isinstance(saved, (int, float)):
            gap = self.wall() - saved
            if gap > 120:
                self.gap_s = round(gap)
            if self.count_downtime and gap > 0:
                self._gap_credit = min(gap, self.max_gap_credit_s)
        self._gap_pending = set(self.runs)

    def _load_profiles(self):
        try:
            mtime = os.stat(self.profiles_path).st_mtime
        except OSError:
            self.profiles, self.profile_errors, self._mtime = {}, {}, None
            return
        if mtime == self._mtime:
            return
        self._mtime = mtime
        data = read_json(self.profiles_path, None)
        if not isinstance(data, dict):
            # keep the previous profiles; surface the problem
            self.profile_errors = {"_file": ["profiles.json is missing or not valid JSON"]}
            return
        valid, errors = {}, {}
        for pid, prof in data.items():
            errs = validate_profile(prof, self.min_f, self.max_f)
            if errs:
                errors[pid] = errs
            else:
                valid[pid] = prof
        self.profiles, self.profile_errors = valid, errors

    # ------------------------------------------------------------ saving
    def _save(self, now):
        write_json_atomic(self.runs_path,
                          {"saved_wall": self.wall(), "runs": self.runs})
        self._last_save = now
        self._save_now = False

    def flush(self):
        """Save immediately (call on shutdown)."""
        self._save(self._last_now if self._last_now is not None else 0.0)

    # ------------------------------------------------------------ main entry
    def resolve(self, now: float, temps: dict, command: dict):
        """Returns (effective_command, profile_status)."""
        self._load_profiles()
        dt = 0.0 if self._last_now is None else min(max(now - self._last_now, 0.0), MAX_DT)
        self._last_now = now

        eff = dict(command)
        eff["fermentors"] = {}
        runs_status = {}

        for name, cmd in command.get("fermentors", {}).items():
            if cmd.get("mode") != "profile":
                eff["fermentors"][name] = cmd
                if name in self.runs:           # leaving profile mode cancels the run
                    del self.runs[name]
                    self._save_now = True
                continue

            pid, run_id = cmd.get("profile"), cmd.get("run_id")
            profile = self.profiles.get(pid)
            run = self.runs.get(name)

            if profile is None or not run_id:
                # Can't run. Keep holding the last setpoint if we have one, else off.
                if not run_id:
                    err = "command has no run_id"
                elif pid in self.profile_errors:
                    err = "profile is invalid: " + "; ".join(self.profile_errors[pid])
                else:
                    err = f"profile '{pid}' not found"
                last = run.get("last_setpoint_f") if run else None
                eff["fermentors"][name] = (
                    {"mode": "auto", "setpoint_f": last} if last is not None
                    else {"mode": "off"})
                runs_status[name] = {"profile": pid, "status": "error", "error": err,
                                     "setpoint_f": last}
                continue

            fresh = False
            if run is None or run.get("run_id") != run_id or run.get("profile") != pid:
                run = new_run(pid, run_id, profile, cmd.get("start_step", 0), temps.get(name))
                self.runs[name] = run
                self._gap_pending.discard(name)
                self._save_now = True
                fresh = True

            paused = bool(cmd.get("paused"))
            if not paused and not fresh:
                before = (run["step"], run["status"])
                if name in self._gap_pending:
                    self._gap_pending.discard(name)
                    if self._gap_credit > 0:
                        advance(run, profile, self._gap_credit, None)
                advance(run, profile, dt, temps.get(name))
                if (run["step"], run["status"]) != before:
                    self._save_now = True        # step change / completion: save at once
            elif paused:
                self._gap_pending.discard(name)

            sp = current_setpoint(run, profile)
            run["last_setpoint_f"] = round(sp, 3)
            eff["fermentors"][name] = {"mode": "auto", "setpoint_f": round(sp, 3)}
            info = describe(run, profile, paused)
            info.update({"profile": pid, "run_id": run_id})
            runs_status[name] = info

        if self._save_now or (self.runs and (
                self._last_save is None or now - self._last_save >= self.save_interval_s)):
            self._save(now)

        status = {"runs": runs_status,
                  "profile_errors": self.profile_errors,
                  "resumed_after_gap_s": self.gap_s}
        return eff, status
