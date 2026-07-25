"""
loop.py — the Controller. sensor -> brain -> actuators, plus the stateful
safety layer (hysteresis via brain, minimum dwell, sensor-stale failsafe,
night fan cap).

Scope note: this app owns the FAN and HEATER (Tuya Cloud). The COOLER is a
Qubo plug with no API — it's run by the corrected Google Home script. The brain
still computes a cooler recommendation; we display it but don't actuate it.
"""
from __future__ import annotations
import logging
import math
import time
from collections import deque
from dataclasses import asdict
from datetime import datetime, time as dtime

from .brain import Decision, Thresholds, decide, dew_point, heat_index
from .cloud import TuyaCloud
from .devices import Sensor, Fan, Heater
from .rules import RuleStore, run_actions, safe_eval

log = logging.getLogger("loop")

# maps (device, command) -> the learned IR key_name to fire (matched at boot)
_SCENE_KEYS = {
    ("led", "on"): "led strip power on", ("led", "off"): "led strip power off",
    ("projector", "power"): "power", ("projector", "on"): "power",
    ("projector", "off"): "power",
}


def _hhmm(s: str) -> dtime:
    h, m = s.split(":")
    return dtime(int(h), int(m))


def _in_window(now: dtime, start: dtime, end: dtime) -> bool:
    return start <= now <= end if start <= end else (now >= start or now <= end)


class Controller:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.thr = Thresholds.from_dict(cfg.get("thresholds"))
        self.cloud = TuyaCloud()

        d = cfg["devices"]
        self.sensor = Sensor(self.cloud, d["sensor"])
        self.fan = Fan(self.cloud, d["fan"])
        self.heater = Heater(self.cloud, d["ir_hub"]["id"], d["heater"])

        self.decision: Decision | None = None
        self.mode_since = time.monotonic()
        self.last_good_read = 0.0
        self.reading: tuple[float, float] | None = None
        self.alerts: list[str] = []
        self.fired_rules: list[str] = []
        self.history = deque(maxlen=240)

        self.store = RuleStore()
        self._ir_codes: dict[str, dict[str, str]] = {}   # device -> {command: code}
        self._load_ir_scenes()

    # -- IR scenes (LED / projector) — fetched fresh so re-learns are picked up
    def _load_ir_scenes(self):
        if not self.cloud.ready:
            return
        ir = self.cfg["devices"]["ir_hub"]["id"]
        for dev in ("led", "projector"):
            remote = self.cfg["devices"][dev]["remote_id"]
            by_name = {k.get("key_name", "").strip().lower(): k.get("code")
                       for k in self.cloud.ir_learned_keys(ir, remote)}
            self._ir_codes[dev] = {}
            for (d, cmd), keyname in _SCENE_KEYS.items():
                if d == dev and keyname in by_name:
                    self._ir_codes[dev][cmd] = by_name[keyname]

    def ir_scene(self, device: str, command: str):
        code = self._ir_codes.get(device, {}).get(command)
        ir = self.cfg["devices"]["ir_hub"]["id"]
        remote = self.cfg["devices"][device]["remote_id"]
        if code:
            self.cloud.ir_send_learned(ir, remote, code)
        else:
            log.info("[sim/no-code] IR %s %s", device, command)

    # -- readings -----------------------------------------------------------
    def _read(self) -> tuple[float, float] | None:
        r = self.sensor.read()
        if r is None and not self.cloud.ready:
            r = self._simulated_reading()
        if r is not None:
            self.reading = r
            self.last_good_read = time.monotonic()
        return r

    def _simulated_reading(self) -> tuple[float, float]:
        h = datetime.now().hour + datetime.now().minute / 60
        temp = 29 + 5 * math.sin((h - 9) / 24 * 2 * math.pi)
        rh = 55 - 20 * math.sin((h - 9) / 24 * 2 * math.pi)
        return round(temp, 1), round(max(20, min(90, rh)), 0)

    # -- one cycle ----------------------------------------------------------
    def tick(self):
        self.alerts = []
        now = time.monotonic()
        r = self._read()

        if r is None or (now - self.last_good_read) > self.cfg["sensor_stale_seconds"]:
            self.alerts.append("Sensor stale — failsafe: fan + heater OFF.")
            self.heater.set(False)
            self.fan.set_speed(0)
            return

        t_c, rh = r
        target = decide(t_c, rh, self.decision, self.thr)

        # minimum dwell — hold mode unless nothing is active yet
        if (self.decision and target.mode != self.decision.mode
                and (now - self.mode_since) < self.cfg["min_dwell_seconds"]):
            target = self.decision
        elif not self.decision or target.mode != self.decision.mode:
            self.mode_since = now

        # night fan cap
        n = self.cfg.get("night")
        if n and _in_window(datetime.now().time(), _hhmm(n["start"]), _hhmm(n["end"])):
            target.fan = min(target.fan, n["fan_cap"])

        # actuate what THIS app owns: fan + heater (cooler is Google Home's)
        self.fan.set_speed(target.fan)
        self.heater.set(target.heater)
        self.decision = target

        # user's conversational rules run AFTER the brain and override per-device
        ctx = {"temp": t_c, "rh": rh, "dew_point": target.dew_point,
               "spread": target.spread, "heat_index": target.heat_index,
               "hour": datetime.now().hour, "minute": datetime.now().minute}
        self.fired_rules = []
        acts = {"fan": self.fan, "heater": self.heater, "ir_scene": self.ir_scene}
        for rule in self.store.rules:
            if rule.enabled and rule.target == "app" and not rule.manual:
                try:
                    if safe_eval(rule.when, ctx):
                        run_actions(rule.actions, acts, ctx)
                        self.fired_rules.append(rule.name)
                except Exception as e:
                    log.error("rule '%s' eval failed: %s", rule.name, e)

        self.history.append({"t": time.time(), "temp": t_c, "rh": rh,
                             "mode": target.mode, "hi": round(target.heat_index, 1)})
        log.info("%.1f°C %.0f%% | %s | fan=%s heater=%s | cooler(GH)=%s",
                 t_c, rh, target.mode, target.fan, target.heater, target.cooler)

    def run_rule(self, rid: str) -> bool:
        """Fire a manual scene's actions once, now."""
        rule = self.store.get(rid)
        if not rule or rule.target != "app":
            return False
        r = self.reading or (26, 60)
        ctx = {"temp": r[0], "rh": r[1], "dew_point": dew_point(*r),
               "spread": r[0] - dew_point(*r), "heat_index": heat_index(*r),
               "hour": datetime.now().hour, "minute": datetime.now().minute}
        run_actions(rule.actions, {"fan": self.fan, "heater": self.heater,
                                   "ir_scene": self.ir_scene}, ctx)
        return True

    def snapshot(self) -> dict:
        return {
            "reading": {"temp": self.reading[0], "rh": self.reading[1]} if self.reading else None,
            "decision": asdict(self.decision) if self.decision else None,
            "alerts": self.alerts,
            "live": self.cloud.ready,
            "cooler_note": "managed by Google Home",
            "fired_rules": self.fired_rules,
            "rules": [r.to_dict() for r in self.store.rules],
            "history": list(self.history),
        }

    def run_forever(self):
        log.info("Controller starting — poll %ss (cloud live=%s).",
                 self.cfg["poll_seconds"], self.cloud.ready)
        while True:
            try:
                self.tick()
            except Exception as e:
                log.exception("tick failed: %s", e)
            time.sleep(self.cfg["poll_seconds"])


def load_config(path: str = "climate/config.yaml") -> dict:
    import yaml
    with open(path) as f:
        return yaml.safe_load(f)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    Controller(load_config()).run_forever()
