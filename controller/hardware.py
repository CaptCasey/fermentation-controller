"""Hardware layer: the real Raspberry Pi (GPIO + DS18B20) and a simulator.

Both classes expose the same three methods:
    read_temps_f()        -> {"f1": 65.2, "f2": None, "glycol": ..., "ambient": ...}
    set_outputs(outputs)  -> {"f1_pump": True, ...}
    all_off()

Run `python3 hardware.py --list-sensors` on the Pi to find your DS18B20 IDs.
"""
import glob
import os
import sys

W1_PATH = "/sys/bus/w1/devices"


def output_pins(cfg: dict) -> dict:
    pins = {}
    for name, f in cfg["fermentors"].items():
        pins[f"{name}_pump"] = f["pump_pin"]
        pins[f"{name}_heat"] = f["heat_pin"]
    pins["chiller"] = cfg["chiller_pin"]
    return pins


def c_to_f(c: float) -> float:
    return c * 9.0 / 5.0 + 32.0


# ---------------------------------------------------------------- real Pi
class RealHardware:
    def __init__(self, cfg: dict):
        from gpiozero import OutputDevice  # preinstalled on Raspberry Pi OS

        self.sensor_ids = cfg["sensors"]
        active_high = cfg.get("ssr_active_high", True)
        self.devices = {
            name: OutputDevice(pin, active_high=active_high, initial_value=False)
            for name, pin in output_pins(cfg).items()
        }

    def read_temps_f(self) -> dict:
        return {name: self._read_one(sid) for name, sid in self.sensor_ids.items()}

    @staticmethod
    def _read_one(sensor_id: str):
        """Read one DS18B20. Returns degrees F, or None on any problem."""
        try:
            with open(f"{W1_PATH}/{sensor_id}/w1_slave") as fh:
                lines = fh.read().splitlines()
            if len(lines) < 2 or not lines[0].strip().endswith("YES"):
                return None  # CRC failure
            raw = lines[1].split("t=")[1]
            # Note: a DS18B20 that just reset reports exactly 85 C (185 F);
            # that is outside the controller's valid range so it's rejected there.
            return c_to_f(int(raw) / 1000.0)
        except (OSError, IndexError, ValueError):
            return None

    def set_outputs(self, outputs: dict):
        for name, dev in self.devices.items():
            if outputs.get(name, False):
                dev.on()
            else:
                dev.off()

    def all_off(self):
        for dev in self.devices.values():
            dev.off()


# ---------------------------------------------------------------- simulator
class SimHardware:
    """Crude thermal model so the control loop can be tested on any computer.

    Roughly: ~7 gal of wort per fermentor, 150 W heat blanket, ~600 W chiller,
    small reservoir of glycol, some heat leak to the room.
    """
    C_FERM = 105000.0   # J/K per fermentor
    C_GLY = 68000.0     # J/K glycol reservoir
    UA_LEAK = 3.0       # W/K fermentor <-> room
    UA_COIL = 40.0      # W/K fermentor <-> glycol (when pump is on)
    UA_GLY_LEAK = 5.0   # W/K reservoir <-> room
    HEAT_W = 150.0
    CHILL_W = 600.0

    def __init__(self, cfg: dict, start_f=68.0, ambient_f=70.0, glycol_f=70.0):
        self.names = list(cfg["fermentors"])
        self.t = {n: start_f for n in self.names}
        self.t["glycol"] = glycol_f
        self.t["ambient"] = ambient_f
        self.out = {}
        self.broken = set()  # sensor names to report as failed

    def read_temps_f(self) -> dict:
        return {k: (None if k in self.broken else v) for k, v in self.t.items()}

    def set_outputs(self, outputs: dict):
        self.out = dict(outputs)

    def all_off(self):
        self.out = {}

    def advance(self, dt: float):
        k = 1.8  # degF per K
        amb = self.t["ambient"]
        g = self.t["glycol"]
        q_gly = self.UA_GLY_LEAK * (amb - g) / k  # heat leaking into reservoir (W)
        for n in self.names:
            tf = self.t[n]
            q = self.UA_LEAK * (amb - tf) / k
            if self.out.get(f"{n}_heat"):
                q += self.HEAT_W
            if self.out.get(f"{n}_pump"):
                q_coil = self.UA_COIL * (tf - g) / k  # W flowing beer -> glycol
                q -= q_coil
                q_gly += q_coil
            self.t[n] = tf + q * dt / self.C_FERM * k
        if self.out.get("chiller"):
            q_gly -= self.CHILL_W
        self.t["glycol"] = g + q_gly * dt / self.C_GLY * k


if __name__ == "__main__":
    if "--list-sensors" in sys.argv:
        found = sorted(glob.glob(f"{W1_PATH}/28-*"))
        if not found:
            print("No DS18B20 sensors found. Is 1-Wire enabled and the sensor wired?")
        for p in found:
            sid = os.path.basename(p)
            print(sid, "->", RealHardware._read_one(sid), "F")
    else:
        print(__doc__)
