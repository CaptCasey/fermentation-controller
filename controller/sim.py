"""Fast simulated-time tests for the control logic (no hardware, no waiting).

    python3 sim.py
"""
import json

from control import Controller
from hardware import SimHardware

with open("config.json") as fh:
    CFG = json.load(fh)

DT = 5.0  # seconds per tick


def run(hours, command, hw, ctrl, t0=0.0, report_every_min=60, on_tick=None):
    """Run the loop in simulated time. Returns (end_time, history)."""
    t = t0
    hist = []
    steps = int(hours * 3600 / DT)
    for i in range(steps):
        outs, status = ctrl.update(t, hw.read_temps_f(), command)
        hw.set_outputs(outs)
        hw.advance(DT)
        hist.append((t, dict(hw.t), dict(outs), status))
        if on_tick:
            on_tick(t, outs, status)
        if i % int(report_every_min * 60 / DT) == 0:
            h = status["fermentors"]
            print(f"t={t/3600:5.1f}h  f1={hw.t['f1']:5.1f} ({h['f1']['state']:<18})"
                  f" f2={hw.t['f2']:5.1f} ({h['f2']['state']:<18})"
                  f" glycol={hw.t['glycol']:5.1f} ({status['glycol']['state']})")
        t += DT
    return t, hist


def check_invariants(hist):
    """Safety rules that must hold on every single tick."""
    cc = CFG["chiller"]
    prev_chiller, last_on, last_off = False, None, hist[0][0]
    for t, temps, outs, status in hist:
        for f in CFG["fermentors"]:
            assert not (outs[f"{f}_heat"] and outs[f"{f}_pump"]), "heat+cool together!"
        if outs["chiller"] and not prev_chiller:
            assert t - last_off >= cc["min_off_s"] - 1e-6, f"short cycle at t={t}"
            last_on = t
        if prev_chiller and not outs["chiller"]:
            last_off = t
        prev_chiller = outs["chiller"]


def scenario_normal():
    print("\n=== Scenario 1: f1 held at 62F, f2 held at 72F, chiller 28F (12h) ===")
    hw = SimHardware(CFG, start_f=68.0, ambient_f=70.0, glycol_f=70.0)
    ctrl = Controller(CFG, 0.0)
    cmd = {"fermentors": {"f1": {"mode": "auto", "setpoint_f": 62.0},
                          "f2": {"mode": "auto", "setpoint_f": 72.0}},
           "chiller": {"enabled": True, "setpoint_f": 28.0}}
    _, hist = run(12, cmd, hw, ctrl)
    check_invariants(hist)
    # Over the last 4h, f1 and f2 should be near setpoint
    late = [h for h in hist if h[0] > 8 * 3600]
    f1 = [h[1]["f1"] for h in late]
    f2 = [h[1]["f2"] for h in late]
    print(f"last 4h f1 range {min(f1):.2f}-{max(f1):.2f}, f2 range {min(f2):.2f}-{max(f2):.2f}")
    assert 61.0 <= min(f1) and max(f1) <= 63.5, "f1 not holding setpoint"
    assert 71.0 <= min(f2) and max(f2) <= 73.0, "f2 not holding setpoint"
    print("PASS")


def scenario_sensor_fail():
    print("\n=== Scenario 2: f1 sensor dies at 2h -> f1 outputs must go off ===")
    hw = SimHardware(CFG, start_f=68.0, ambient_f=70.0, glycol_f=30.0)
    ctrl = Controller(CFG, 0.0)
    cmd = {"fermentors": {"f1": {"mode": "auto", "setpoint_f": 60.0},
                          "f2": {"mode": "off"}},
           "chiller": {"enabled": True, "setpoint_f": 28.0}}
    t, hist = run(2, cmd, hw, ctrl)
    hw.broken.add("f1")
    _, hist2 = run(1, cmd, hw, ctrl, t0=t)
    faulted = [h for h in hist2 if h[0] > t + CFG["sensor"]["timeout_s"] + 10]
    assert faulted and all(not h[2]["f1_pump"] and not h[2]["f1_heat"] for h in faulted), \
        "f1 outputs still on after sensor fault"
    assert any("f1:sensor_fault" in h[3]["alarms"] for h in faulted), "no alarm raised"
    print("PASS (outputs off, alarm raised)")


def scenario_freeze_cutoff():
    print("\n=== Scenario 3: glycol drops to freeze cutoff -> chiller forced off ===")
    hw = SimHardware(CFG, start_f=68.0, ambient_f=70.0, glycol_f=23.0)
    ctrl = Controller(CFG, -10000.0)  # long since booted
    cmd = {"fermentors": {}, "chiller": {"enabled": True, "setpoint_f": 28.0}}
    _, hist = run(0.2, cmd, hw, ctrl, t0=0.0, report_every_min=5)
    assert all(not h[2]["chiller"] for h in hist if h[1]["glycol"] <= CFG["chiller"]["freeze_cutoff_f"])
    assert any("chiller:glycol_freeze_cutoff" in h[3]["alarms"] for h in hist)
    print("PASS")


def scenario_boot_delay():
    print("\n=== Scenario 4: compressor waits min_off_s after boot ===")
    hw = SimHardware(CFG, start_f=68.0, ambient_f=75.0, glycol_f=75.0)
    ctrl = Controller(CFG, 0.0)
    cmd = {"fermentors": {}, "chiller": {"enabled": True, "setpoint_f": 28.0}}
    _, hist = run(0.5, cmd, hw, ctrl, report_every_min=10)
    first_on = next(h[0] for h in hist if h[2]["chiller"])
    print(f"chiller first started at {first_on:.0f}s (min_off_s={CFG['chiller']['min_off_s']})")
    assert first_on >= CFG["chiller"]["min_off_s"]
    print("PASS")


if __name__ == "__main__":
    scenario_normal()
    scenario_sensor_fail()
    scenario_freeze_cutoff()
    scenario_boot_delay()
    print("\nAll scenarios passed.")
