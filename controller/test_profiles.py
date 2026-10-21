"""Tests for the profile engine. No pytest needed:

    python3 test_profiles.py
"""
import json
import os
import tempfile

from control import Controller
from hardware import SimHardware
from profiles import validate_profile
from runner import ProfileRunner
from util import write_json_atomic

with open("config.json") as fh:
    CFG = json.load(fh)
with open("profiles.example.json") as fh:
    EXAMPLES = json.load(fh)

H = 3600.0


def make_runner(profiles, wall=None, **profile_cfg):
    d = tempfile.mkdtemp()
    write_json_atomic(os.path.join(d, "profiles.json"), profiles)
    cfg = json.loads(json.dumps(CFG))
    cfg["profile"].update(profile_cfg)
    clock = wall if wall is not None else [1_000_000.0]
    return ProfileRunner(cfg, d, wall=lambda: clock[0]), d, cfg, clock


def cmd(pid="p", run_id="r1", **extra):
    c = {"mode": "profile", "profile": pid, "run_id": run_id}
    c.update(extra)
    return {"fermentors": {"f1": c}}


def run_for(runner, command, hours, temp=62.0, t0=0.0, tick=60.0):
    """Advance `hours` of simulated time; returns (end_time, last_status)."""
    t, st = t0, None
    for _ in range(int(hours * H / tick) + 1):
        _, st = runner.resolve(t, {"f1": temp}, command)
        t += tick
    return t, st


def sp_at(runner, command, hours, **kw):
    _, st = run_for(runner, command, hours, **kw)
    return st["runs"]["f1"]["setpoint_f"]


# ---------------------------------------------------------------- tests
def test_example_profile():
    print("example profile: 60F 12h, +1F/h to 72, hold 72 for 14d, cool to 35")
    # fresh runner per checkpoint keeps each check independent and simple
    checks = [
        (11.5,            60.0),
        (12.5,            60.0),   # ramp started, first increment not yet due
        (13.2,            61.0),
        (17.2,            65.0),
        (23.5,            71.0),
        (24.3,            72.0),   # ramp finished -> 72 hold
        (24 + 13 * 24,    72.0),   # still in the 2 week hold
        (24 + 14 * 24 + 0.5, 35.0),  # cooled
    ]
    for hours, expect in checks:
        r, *_ = make_runner(EXAMPLES)
        got = sp_at(r, cmd("ale-standard"), hours, temp=60.0, tick=300.0)
        assert abs(got - expect) < 1e-6, f"at {hours} h expected {expect}, got {got}"
        print(f"  t={hours:7.1f} h  setpoint {got:g}  ok")


def test_forever_hold_never_completes():
    r, *_ = make_runner(EXAMPLES)
    _, st = run_for(r, cmd("ale-standard"), 24 + 14 * 24 + 100, tick=600.0)
    info = st["runs"]["f1"]
    assert info["status"] == "running" and info["total_remaining_s"] is None
    print("hold-forever step keeps running (no completion): ok")


def test_linear_ramp_and_completion():
    prof = {"p": {"steps": [{"type": "ramp", "to_f": 66, "rate_f_per_h": 2},
                            {"type": "hold", "temp_f": 66, "duration_h": 1}]}}
    r, *_ = make_runner(prof)
    # starts at beer temp 60 -> 3 h ramp
    assert abs(sp_at(r, cmd(), 1.5, temp=60.0) - 63.0) < 0.1
    r, *_ = make_runner(prof)
    _, st = run_for(r, cmd(), 4.5, temp=60.0)
    info = st["runs"]["f1"]
    assert info["status"] == "complete" and info["setpoint_f"] == 66.0
    print("linear ramp from actual temp, then complete & hold last setpoint: ok")


def test_downward_staircase():
    prof = {"p": {"steps": [{"type": "hold", "temp_f": 70, "duration_h": 1},
                            {"type": "ramp", "to_f": 66, "step_f": 1.5, "every_h": 2},
                            {"type": "hold", "temp_f": 66, "duration_h": 1}]}}
    r, *_ = make_runner(prof)
    # 4F in 1.5F steps = 3 steps (70, 68.5, 67, 66)
    assert abs(sp_at(r, cmd(), 1 + 2.1, temp=70.0, tick=300.0) - 68.5) < 1e-6
    r, *_ = make_runner(prof)
    assert abs(sp_at(r, cmd(), 1 + 4.1, temp=70.0, tick=300.0) - 67.0) < 1e-6
    r, *_ = make_runner(prof)
    assert abs(sp_at(r, cmd(), 1 + 6.5, temp=70.0, tick=300.0) - 66.0) < 1e-6
    print("downward staircase with a partial last step lands exactly on target: ok")


def test_pause_freezes_clock():
    prof = {"p": {"steps": [{"type": "hold", "temp_f": 62, "duration_h": 10}]}}
    r, *_ = make_runner(prof)
    t, st = run_for(r, cmd(), 2)
    e1 = st["runs"]["f1"]["step_elapsed_s"]
    t, st = run_for(r, cmd(paused=True), 5, t0=t)
    assert st["runs"]["f1"]["status"] == "paused"
    assert st["runs"]["f1"]["step_elapsed_s"] == e1, "clock moved while paused"
    t, st = run_for(r, cmd(paused=False), 1, t0=t)
    assert st["runs"]["f1"]["step_elapsed_s"] > e1 + 3000
    print("pause freezes the timer, resume continues: ok")


def test_wait_for_temp():
    prof = {"p": {"steps": [{"type": "hold", "temp_f": 72, "duration_h": 1,
                             "wait_for_temp": True, "tolerance_f": 1.0}]}}
    r, *_ = make_runner(prof)
    t, st = run_for(r, cmd(), 3, temp=65.0)          # far from 72
    info = st["runs"]["f1"]
    assert info["status"] == "waiting_for_temp" and info["step_elapsed_s"] == 0
    t, st = run_for(r, cmd(), 0.5, temp=71.4, t0=t)  # within 1F -> timer starts
    assert st["runs"]["f1"]["step_elapsed_s"] > 0
    t, st = run_for(r, cmd(), 0.7, temp=70.0, t0=t)  # drifts off, timer keeps going
    assert st["runs"]["f1"]["status"] == "complete"
    print("wait_for_temp: timer starts only once beer reaches target: ok")


def test_new_run_id_restarts_and_leaving_profile_cancels():
    prof = {"p": {"steps": [{"type": "hold", "temp_f": 62, "duration_h": 10}]}}
    r, *_ = make_runner(prof)
    t, st = run_for(r, cmd(run_id="a"), 2)
    assert st["runs"]["f1"]["step_elapsed_s"] > 7000
    t, st = run_for(r, cmd(run_id="a"), 1, t0=t)      # same id: keeps going
    assert st["runs"]["f1"]["step_elapsed_s"] > 10000
    t, st = run_for(r, cmd(run_id="b"), 0.1, t0=t)    # new id: restarts
    assert st["runs"]["f1"]["step_elapsed_s"] < 600
    eff, st = r.resolve(t, {"f1": 62.0}, {"fermentors": {"f1": {"mode": "auto", "setpoint_f": 65}}})
    assert eff["fermentors"]["f1"]["setpoint_f"] == 65 and "f1" not in st["runs"]
    assert "f1" not in r.runs
    print("new run_id restarts; leaving profile mode cancels the run: ok")


def test_start_step():
    r, *_ = make_runner(EXAMPLES)
    _, st = run_for(r, cmd("ale-standard", start_step=2), 1, temp=72.0)
    info = st["runs"]["f1"]
    assert info["step"] == 3 and info["setpoint_f"] == 72.0
    print("start_step begins partway through a profile: ok")


def test_persistence_and_downtime():
    prof = EXAMPLES
    wall = [1_000_000.0]
    r, d, cfg, _ = make_runner(prof, wall=wall)
    t, st = run_for(r, cmd("ale-standard"), 5, temp=60.0)
    before = st["runs"]["f1"]["step_elapsed_s"]
    r.flush()
    runs_path = os.path.join(d, "runs.json")
    with open(runs_path) as fh:
        snapshot = fh.read()          # what is on disk when the power goes out

    def power_cycle():                # every restart scenario starts from that disk state
        with open(runs_path, "w") as fh:
            fh.write(snapshot)

    # restart after a 30 minute outage, downtime NOT counted (default)
    wall2 = [wall[0] + 1800]
    cfg2 = json.loads(json.dumps(cfg))
    r2 = ProfileRunner(cfg2, d, wall=lambda: wall2[0])
    _, st2 = r2.resolve(0.0, {"f1": 60.0}, cmd("ale-standard"))
    after = st2["runs"]["f1"]["step_elapsed_s"]
    assert abs(after - before) < 1.0, (before, after)
    assert st2["resumed_after_gap_s"] and abs(st2["resumed_after_gap_s"] - 1800) < 2
    print(f"restart resumes at same point ({after/H:.2f} h), outage reported: ok")

    # same restart, downtime counted
    power_cycle()
    cfg3 = json.loads(json.dumps(cfg))
    cfg3["profile"]["count_downtime"] = True
    r3 = ProfileRunner(cfg3, d, wall=lambda: wall2[0])
    _, st3 = r3.resolve(0.0, {"f1": 60.0}, cmd("ale-standard"))
    got = st3["runs"]["f1"]["step_elapsed_s"]
    assert abs(got - (before + 1800)) < 1.0, (before, got)
    print("count_downtime=true adds the outage to the timer: ok")

    # a clock that jumped backwards must never move the timer backwards
    power_cycle()
    wall4 = [wall[0] - 10 * H]
    r4 = ProfileRunner(cfg3, d, wall=lambda: wall4[0])
    _, st4 = r4.resolve(0.0, {"f1": 60.0}, cmd("ale-standard"))
    assert st4["runs"]["f1"]["step_elapsed_s"] >= before - 1
    print("backwards clock jump is ignored: ok")


def test_errors_and_hold_last_setpoint():
    prof = {"p": {"steps": [{"type": "hold", "temp_f": 62, "duration_h": 10}]}}
    r, d, _, _ = make_runner(prof)
    eff, st = r.resolve(0.0, {"f1": 62.0}, cmd("missing"))
    assert eff["fermentors"]["f1"]["mode"] == "off" and st["runs"]["f1"]["status"] == "error"
    # a running profile whose definition vanishes keeps holding its last setpoint
    t, _ = run_for(r, cmd("p"), 1, t0=60.0)
    os.remove(os.path.join(d, "profiles.json"))
    eff, st = r.resolve(t, {"f1": 62.0}, cmd("p"))
    assert eff["fermentors"]["f1"] == {"mode": "auto", "setpoint_f": 62}
    assert st["runs"]["f1"]["status"] == "error"
    eff, st = r.resolve(t + 60, {"f1": 62.0}, {"fermentors": {"f1": {"mode": "profile", "profile": "p"}}})
    assert st["runs"]["f1"]["status"] == "error" and "run_id" in st["runs"]["f1"]["error"]
    print("missing profile / missing run_id: safe fallback + clear error: ok")


def test_validation():
    good = EXAMPLES["ale-standard"]
    assert validate_profile(good) == []
    bad_cases = {
        "no steps": {"steps": []},
        "bad type": {"steps": [{"type": "soak"}]},
        "temp too high": {"steps": [{"type": "hold", "temp_f": 150, "duration_h": 1}]},
        "temp too low": {"steps": [{"type": "hold", "temp_f": 10, "duration_h": 1}]},
        "zero duration": {"steps": [{"type": "hold", "temp_f": 60, "duration_h": 0}]},
        "forever not last": {"steps": [{"type": "hold", "temp_f": 60, "duration_h": None},
                                       {"type": "hold", "temp_f": 62, "duration_h": 1}]},
        "ramp no rate": {"steps": [{"type": "ramp", "to_f": 66}]},
        "ramp both": {"steps": [{"type": "ramp", "to_f": 66, "rate_f_per_h": 1,
                                 "step_f": 1, "every_h": 1}]},
        "ramp half stair": {"steps": [{"type": "ramp", "to_f": 66, "step_f": 1}]},
        "text temp": {"steps": [{"type": "hold", "temp_f": "sixty", "duration_h": 1}]},
        "not an object": "nope",
    }
    for label, prof in bad_cases.items():
        assert validate_profile(prof), f"'{label}' should be rejected"
    print(f"validation accepts the example and rejects {len(bad_cases)} bad profiles: ok")


def test_closed_loop_with_controller():
    """Profile -> runner -> controller -> simulated tanks, 9 simulated hours."""
    print("closed loop: hold 62F 3h, +1F/h up to 66F, hold 66F 2h (simulated tanks)")
    prof = {"p": {"steps": [{"type": "hold", "temp_f": 62, "duration_h": 3},
                            {"type": "ramp", "to_f": 66, "step_f": 1, "every_h": 1},
                            {"type": "hold", "temp_f": 66, "duration_h": 2}]}}
    r, d, cfg, _ = make_runner(prof)
    hw = SimHardware(cfg, start_f=68.0, ambient_f=70.0, glycol_f=30.0)
    ctrl = Controller(cfg, 0.0)
    command = {"fermentors": {"f1": {"mode": "profile", "profile": "p", "run_id": "x"},
                              "f2": {"mode": "off"}},
               "chiller": {"enabled": True, "setpoint_f": 28.0}}
    dt, t = 5.0, 0.0
    samples = {}
    for i in range(int(9.2 * H / dt)):
        eff, pst = r.resolve(t, hw.read_temps_f(), command)
        outs, status = ctrl.update(t, hw.read_temps_f(), eff)
        assert not (outs["f1_heat"] and outs["f1_pump"])
        hw.set_outputs(outs)
        hw.advance(dt)
        h = t / H
        for mark in (2.9, 6.9, 8.9):
            if abs(h - mark) < dt / H / 2:
                samples[mark] = (hw.t["f1"], pst["runs"]["f1"]["setpoint_f"],
                                 pst["runs"]["f1"]["status"])
        t += dt
    for mark, (temp, sp, st) in sorted(samples.items()):
        print(f"  t={mark:.1f} h  setpoint {sp:g}  beer {temp:.2f}  ({st})")
    assert abs(samples[2.9][0] - 62.0) < 1.0
    assert abs(samples[6.9][0] - 66.0) < 1.0
    assert abs(samples[8.9][0] - 66.0) < 1.0
    assert samples[8.9][2] in ("running", "complete")
    print("beer tracks the profile: ok")


if __name__ == "__main__":
    test_validation()
    test_example_profile()
    test_forever_hold_never_completes()
    test_linear_ramp_and_completion()
    test_downward_staircase()
    test_pause_freezes_clock()
    test_wait_for_temp()
    test_new_run_id_restarts_and_leaving_profile_cancels()
    test_start_step()
    test_persistence_and_downtime()
    test_errors_and_hold_last_setpoint()
    test_closed_loop_with_controller()
    print("\nAll profile tests passed.")
