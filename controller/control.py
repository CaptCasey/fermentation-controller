"""Pure control logic for the fermentation controller.

No GPIO, no files, no real clock. Everything the controller needs comes in
through update(), which makes it easy to test with simulated time/sensors.

All temperatures are degrees Fahrenheit. `now` is any monotonic seconds value.
"""
from dataclasses import dataclass
from typing import Optional

NEVER = float("-inf")


@dataclass
class FermState:
    cooling: bool = False
    heating: bool = False
    last_heat_off: float = NEVER
    last_cool_off: float = NEVER
    oob_since: Optional[float] = None  # when temp first left the alarm band


class Controller:
    def __init__(self, cfg: dict, now: float):
        self.cfg = cfg
        self.ferm = {name: FermState() for name in cfg["fermentors"]}
        self.chiller_on = False
        self.chiller_last_on = NEVER
        # Treat boot as "just turned off" so the compressor always waits
        # min_off_s after a power-up or restart before it first starts.
        self.chiller_last_off = now
        self.last_good = {}  # sensor name -> (value, time)

    # ------------------------------------------------------------ sensors
    def _sensor(self, name: str, raw: Optional[float], now: float) -> Optional[float]:
        """Return a validated reading, or None if the sensor is faulty/stale."""
        sc = self.cfg["sensor"]
        if raw is not None and sc["valid_min_f"] <= raw <= sc["valid_max_f"]:
            self.last_good[name] = (raw, now)
        good = self.last_good.get(name)
        if good and now - good[1] <= sc["timeout_s"]:
            return good[0]
        return None

    # ------------------------------------------------------------ fermentor
    def _fermentor(self, name, now, temp, glycol, cmd):
        fc = self.cfg["fermentor"]
        st = self.ferm[name]
        alarms = []
        mode = cmd.get("mode", "off")
        sp = cmd.get("setpoint_f")

        if temp is None:
            alarms.append("sensor_fault")

        if mode != "auto" or sp is None or temp is None:
            self._set_cool(st, False, now)
            self._set_heat(st, False, now)
            st.oob_since = None
            state = "fault" if (mode == "auto" and temp is None) else "off"
            return state, False, False, sp, alarms

        sp = min(max(float(sp), fc["safe_min_f"]), fc["safe_max_f"])
        db, lock = fc["deadband_f"], fc["switch_lockout_s"]

        # 1) stop conditions first (so heat and cool can never both be on)
        if st.cooling and (temp <= sp or temp <= fc["safe_min_f"]):
            self._set_cool(st, False, now)
        if st.heating and (temp >= sp or temp >= fc["safe_max_f"]):
            self._set_heat(st, False, now)

        # 2) start conditions, only when idle, with a heat<->cool lockout
        if not st.cooling and not st.heating:
            if temp > sp + db and now - st.last_heat_off >= lock:
                self._set_cool(st, True, now)
            elif temp < sp - db and now - st.last_cool_off >= lock:
                self._set_heat(st, True, now)

        # 3) pump only runs if the glycol is actually colder than the beer
        pump = False
        if st.cooling:
            if glycol is None:
                state = "cooling_no_glycol_sensor"
            elif glycol <= temp - fc["min_glycol_delta_f"]:
                pump = True
                state = "cooling"
            else:
                state = "waiting_for_glycol"
        elif st.heating:
            state = "heating"
        else:
            state = "idle"

        # 4) soft alarm if we've been outside the band for a long time
        if abs(temp - sp) > fc["alarm_band_f"]:
            if st.oob_since is None:
                st.oob_since = now
            elif now - st.oob_since >= fc["alarm_delay_s"]:
                alarms.append("temp_high" if temp > sp else "temp_low")
        else:
            st.oob_since = None

        return state, pump, st.heating, sp, alarms

    @staticmethod
    def _set_cool(st, on, now):
        if st.cooling and not on:
            st.last_cool_off = now
        st.cooling = on

    @staticmethod
    def _set_heat(st, on, now):
        if st.heating and not on:
            st.last_heat_off = now
        st.heating = on

    # ------------------------------------------------------------ chiller
    def _chiller(self, now, glycol, cmd):
        cc = self.cfg["chiller"]
        alarms = []
        enabled = bool(cmd.get("enabled", False))
        db = cc["deadband_f"]
        sp = float(cmd.get("setpoint_f", cc["default_setpoint_f"]))
        sp = max(sp, cc["freeze_cutoff_f"] + 2 * db)

        forced_off = False
        state = "idle"
        if not enabled:
            want, forced_off, state = False, True, "disabled"
        elif glycol is None:
            want, forced_off, state = False, True, "fault"
            alarms.append("glycol_sensor_fault")
        elif glycol <= cc["freeze_cutoff_f"]:
            want, forced_off, state = False, True, "freeze_cutoff"
            alarms.append("glycol_freeze_cutoff")
        elif self.chiller_on:
            want = glycol > sp - db      # run until we reach sp - db
        else:
            want = glycol >= sp + db     # start once we reach sp + db

        # compressor protection
        if want and not self.chiller_on:
            if now - self.chiller_last_off < cc["min_off_s"]:
                want, state = False, "waiting_min_off"
        elif not want and self.chiller_on and not forced_off:
            if now - self.chiller_last_on < cc["min_on_s"]:
                want = True  # hold on until min_on_s has elapsed

        if want and not self.chiller_on:
            self.chiller_last_on = now
        elif not want and self.chiller_on:
            self.chiller_last_off = now
        self.chiller_on = want

        if want:
            state = "running"
        return state, want, sp, alarms

    # ------------------------------------------------------------ main entry
    def update(self, now: float, raw_temps: dict, command: dict):
        """One control tick. Returns (outputs, status)."""
        temps = {n: self._sensor(n, raw_temps.get(n), now) for n in raw_temps}
        glycol = temps.get("glycol")
        outputs, ferm_status, all_alarms = {}, {}, []

        for name in self.cfg["fermentors"]:
            cmd = command.get("fermentors", {}).get(name, {})
            state, pump, heat, sp, alarms = self._fermentor(
                name, now, temps.get(name), glycol, cmd)
            outputs[f"{name}_pump"] = pump
            outputs[f"{name}_heat"] = heat
            ferm_status[name] = {
                "label": self.cfg["fermentors"][name].get("label", name),
                "temp_f": temps.get(name),
                "setpoint_f": sp,
                "mode": cmd.get("mode", "off"),
                "state": state,
                "alarms": alarms,
            }
            all_alarms += [f"{name}:{a}" for a in alarms]

        ch_state, ch_on, ch_sp, ch_alarms = self._chiller(
            now, glycol, command.get("chiller", {}))
        outputs["chiller"] = ch_on
        all_alarms += [f"chiller:{a}" for a in ch_alarms]

        if temps.get("ambient") is None and "ambient" in raw_temps:
            all_alarms.append("ambient:sensor_fault")

        status = {
            "fermentors": ferm_status,
            "glycol": {"temp_f": glycol, "setpoint_f": ch_sp, "state": ch_state},
            "ambient_f": temps.get("ambient"),
            "outputs": outputs,
            "alarms": all_alarms,
        }
        return outputs, status
