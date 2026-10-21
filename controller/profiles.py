"""Pure fermentation-profile logic. No files, no clock, no GPIO.

A profile is an ordered list of steps:

  {"type": "hold", "temp_f": 60, "duration_h": 12}
        Hold a temperature. duration_h may be null = hold forever (last step only).
        Optional: "wait_for_temp": true  -> the timer only starts counting once the
        beer is within "tolerance_f" (default 1.0) of temp_f.

  {"type": "ramp", "to_f": 72, "step_f": 1, "every_h": 1}
        Staircase: setpoint rises (or falls) by step_f at the end of each every_h.
  {"type": "ramp", "to_f": 72, "rate_f_per_h": 0.5}
        Smooth linear ramp at a fixed rate.

A ramp starts from wherever the previous step ended (or from the actual beer
temperature if it is the first step) and finishes exactly at to_f.

A "run" is the saved progress of one profile on one fermentor (a plain dict so
it can be stored as JSON):
  profile, run_id, step, step_elapsed_s, step_start_f, temp_reached, status
"""
import math
from typing import Optional

HOUR = 3600.0
EPS = 1e-6


# ------------------------------------------------------------------ validation
def _num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _check_temp(val, label, lo, hi):
    if not _num(val):
        return [f"{label} must be a number"]
    if not lo <= val <= hi:
        return [f"{label} {val} is outside the allowed range {lo:g}-{hi:g} F"]
    return []


def validate_profile(profile, min_f: float = 30.0, max_f: float = 90.0) -> list:
    """Return a list of human-readable problems (empty list = valid)."""
    if not isinstance(profile, dict):
        return ["profile must be an object"]
    steps = profile.get("steps")
    if not isinstance(steps, list) or not steps:
        return ["profile needs at least one step"]

    errs = []
    for i, s in enumerate(steps, 1):
        p = f"step {i}: "
        if not isinstance(s, dict):
            errs.append(p + "must be an object")
            continue
        kind = s.get("type")
        if kind == "hold":
            errs += _check_temp(s.get("temp_f"), p + "temp_f", min_f, max_f)
            d = s.get("duration_h")
            if d is not None and not (_num(d) and d > 0):
                errs.append(p + "duration_h must be a number > 0, or null to hold forever")
            if d is None and i != len(steps):
                errs.append(p + "only the last step can hold forever")
            if "wait_for_temp" in s and not isinstance(s["wait_for_temp"], bool):
                errs.append(p + "wait_for_temp must be true or false")
            if "tolerance_f" in s and not (_num(s["tolerance_f"]) and s["tolerance_f"] > 0):
                errs.append(p + "tolerance_f must be a number > 0")
        elif kind == "ramp":
            errs += _check_temp(s.get("to_f"), p + "to_f", min_f, max_f)
            has_rate = s.get("rate_f_per_h") is not None
            has_stair = s.get("step_f") is not None or s.get("every_h") is not None
            if has_rate == has_stair:
                errs.append(p + "ramp needs either rate_f_per_h, or both step_f and every_h")
            elif has_rate:
                if not (_num(s["rate_f_per_h"]) and s["rate_f_per_h"] > 0):
                    errs.append(p + "rate_f_per_h must be a number > 0")
            else:
                for key in ("step_f", "every_h"):
                    if not (_num(s.get(key)) and s[key] > 0):
                        errs.append(p + f"{key} must be a number > 0")
        else:
            errs.append(p + "type must be 'hold' or 'ramp'")
    return errs


# ------------------------------------------------------------------ step math
def final_setpoint(step: dict) -> float:
    return step["temp_f"] if step["type"] == "hold" else step["to_f"]


def step_duration_s(step: dict, from_f: float) -> Optional[float]:
    """Length of a step in seconds, or None if it lasts forever."""
    if step["type"] == "hold":
        d = step.get("duration_h")
        return None if d is None else d * HOUR
    delta = abs(step["to_f"] - from_f)
    if delta < EPS:
        return 0.0
    if step.get("rate_f_per_h") is not None:
        return delta / step["rate_f_per_h"] * HOUR
    n = math.ceil(delta / step["step_f"] - 1e-9)
    return n * step["every_h"] * HOUR


def step_setpoint(step: dict, from_f: float, elapsed_s: float) -> float:
    """Setpoint at a given time into a step."""
    if step["type"] == "hold":
        return step["temp_f"]
    to = step["to_f"]
    delta = abs(to - from_f)
    direction = 1.0 if to >= from_f else -1.0
    if step.get("rate_f_per_h") is not None:
        moved = min(delta, step["rate_f_per_h"] * elapsed_s / HOUR)
    else:
        done = math.floor(elapsed_s / (step["every_h"] * HOUR) + 1e-9)
        moved = min(delta, done * step["step_f"])
    return from_f + direction * moved


def step_label(step: dict) -> str:
    if step["type"] == "hold":
        d = step.get("duration_h")
        tail = "until stopped" if d is None else f"for {d:g} h"
        return f"Hold {step['temp_f']:g}\u00b0F {tail}"
    return f"Ramp to {step['to_f']:g}\u00b0F"


# ------------------------------------------------------------------ runs
def new_run(profile_id: str, run_id: str, profile: dict, start_step: int,
            temp: Optional[float]) -> dict:
    steps = profile["steps"]
    i = max(0, min(int(start_step or 0), len(steps) - 1))
    # A ramp begins from the actual beer temperature when a run starts.
    from_f = temp if temp is not None else final_setpoint(steps[i])
    return {
        "profile": profile_id,
        "run_id": run_id,
        "step": i,
        "step_elapsed_s": 0.0,
        "step_start_f": from_f,
        "temp_reached": False,
        "waiting": False,
        "status": "running",
    }


def _next_step(run: dict, steps: list) -> None:
    i = run["step"]
    prev_final = final_setpoint(steps[i])
    if i + 1 >= len(steps):
        run["status"] = "complete"   # last setpoint keeps being held
        return
    run["step"] = i + 1
    run["step_elapsed_s"] = 0.0
    run["step_start_f"] = prev_final
    run["temp_reached"] = False


def advance(run: dict, profile: dict, dt: float, temp: Optional[float]) -> None:
    """Move a run forward by dt seconds (mutates `run`)."""
    steps = profile["steps"]
    if run["step"] >= len(steps):          # profile was shortened while running
        run["step"] = len(steps) - 1
        run["status"] = "complete"
    remaining = max(0.0, dt)
    run["waiting"] = False

    for _ in range(10000):                 # hard stop against any logic slip
        if run["status"] != "running":
            break
        s = steps[run["step"]]

        # temperature gate: timer doesn't run until the beer reaches the target
        if s["type"] == "hold" and s.get("wait_for_temp") and not run.get("temp_reached"):
            tol = s.get("tolerance_f", 1.0)
            if temp is not None and abs(temp - s["temp_f"]) <= tol:
                run["temp_reached"] = True
            else:
                run["waiting"] = True
                break

        dur = step_duration_s(s, run["step_start_f"])
        if dur is not None and run["step_elapsed_s"] >= dur - EPS:
            _next_step(run, steps)
            continue
        if remaining <= EPS:
            break
        if dur is None:                    # holding forever
            run["step_elapsed_s"] += remaining
            break
        take = min(dur - run["step_elapsed_s"], remaining)
        run["step_elapsed_s"] += take
        remaining -= take


def current_setpoint(run: dict, profile: dict) -> float:
    steps = profile["steps"]
    if run["status"] == "complete" or run["step"] >= len(steps):
        return final_setpoint(steps[-1])
    return step_setpoint(steps[run["step"]], run["step_start_f"], run["step_elapsed_s"])


def describe(run: dict, profile: dict, paused: bool) -> dict:
    """Status block for the UI."""
    steps = profile["steps"]
    i = min(run["step"], len(steps) - 1)
    s = steps[i]
    complete = run["status"] == "complete"

    dur = step_duration_s(s, run["step_start_f"])
    if complete:
        step_rem, total = 0.0, 0.0
    else:
        step_rem = None if dur is None else max(0.0, dur - run["step_elapsed_s"])
        total = step_rem
        from_f = final_setpoint(s)
        for later in steps[i + 1:]:
            d = step_duration_s(later, from_f)
            if d is None or total is None:
                total = None
                break
            total += d
            from_f = final_setpoint(later)

    if complete:
        status = "complete"
    elif paused:
        status = "paused"
    elif run.get("waiting"):
        status = "waiting_for_temp"
    else:
        status = "running"

    return {
        "status": status,
        "name": profile.get("name"),
        "step": i + 1,
        "step_count": len(steps),
        "step_label": step_label(s),
        "step_elapsed_s": round(run["step_elapsed_s"], 1),
        "step_remaining_s": None if step_rem is None else round(step_rem, 1),
        "total_remaining_s": None if total is None else round(total, 1),
        "setpoint_f": round(current_setpoint(run, profile), 3),
    }
