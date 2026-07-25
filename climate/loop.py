"""
loop.py — the Controller. sensor -> brain -> actuators, plus the stateful
safety layer (hysteresis via brain, minimum dwell, sensor-stale failsafe,
night fan cap), manual dashboard overrides, self-learning, and power-outage
recovery.

Scope note: this app owns the FAN, HEATER, LED, PROJECTOR (Tuya Cloud). The
COOLER is a Qubo plug with no API — it's run by the corrected Google Home
script. The brain still computes a cooler recommendation; we display it but
don't actuate it.
"""
from __future__ import annotations
import json
import logging
import math
import time
from collections import deque
from dataclasses import asdict
from datetime import datetime, time as dtime

from .brain import Decision, Thresholds, decide, dew_point, heat_index, heater_dial
from .cloud import TuyaCloud
from .devices import Sensor, Fan, HandyHeater, Projector, LED
from .learn import Learner, infer_cooler_state
from .rules import RuleStore, run_actions, safe_eval
from .store import Store

log = logging.getLogger("loop")

# maps (device, command) -> the learned IR key_name to fire (matched at boot)
_SCENE_KEYS = {
    ("led", "on"): "led strip power on", ("led", "off"): "led strip power off",
}

DESIRED_KEY = "desired_state"


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
        self.db = Store()
        self.learner = Learner(self.db, cfg)

        # persisted threshold overrides (from earlier auto-tune runs) win over config
        for name, value in self.db.get_thresholds().items():
            if hasattr(self.thr, name):
                setattr(self.thr, name, value)

        d = cfg["devices"]
        self.sensor = Sensor(self.cloud, d["sensor"])
        self.fan = Fan(self.cloud, d["fan"])
        self.heater = HandyHeater(self.cloud, d["ir_hub"]["id"], d["heater"])
        self.projector = Projector(self.cloud, d["ir_hub"]["id"], d["projector"])
        self.led = LED(self.cloud, d["ir_hub"]["id"], d["led"]["remote_id"], "", "")

        self.decision: Decision | None = None
        self.mode_since = time.monotonic()
        self.last_good_read = 0.0
        self.reading: tuple[float, float] | None = None
        self.alerts: list[str] = []
        self.fired_rules: list[str] = []
        self.escalation_notes: list[str] = []
        self.history = deque(maxlen=240)

        self.rule_store = RuleStore()
        self._ir_codes: dict[str, dict[str, str]] = {}   # device -> {command: code}
        self._load_ir_scenes()

        # -- last-intended state, persisted so it survives BOTH a power
        # outage (device forgets) and an app restart (in-memory would forget)
        self.desired: dict = json.loads(self.db.kv_get(DESIRED_KEY, "{}") or "{}")
        self.desired.setdefault("fan", 0)
        self.desired.setdefault("heater", False)
        self.desired.setdefault("led", False)
        self.desired.setdefault("projector", False)
        self.desired.setdefault("led_color", None)

        # Toggle-based devices (heater/projector) default their tracked state
        # to False on construction — that's only correct for a fresh install.
        # On every OTHER startup, assume the physical device is still wherever
        # we last left it (no power/connectivity event happened, just our
        # process restarting) and sync tracked state to match `desired`,
        # rather than assuming False and risking a misfired toggle on the
        # next command (confirmed by a real test: restarting the app while
        # the projector was genuinely on, then sending "on" again, turned it
        # OFF — exactly this desync).
        self.heater.force_state(on=self.desired["heater"])
        self.projector.force_state(self.desired["projector"])
        self.led.force_state(self.desired["led"])

        # -- manual dashboard overrides: {device: {"value":..., "expires": monotonic}}
        self.manual: dict[str, dict] = {}

        # -- power outage watch
        self._outage_ids = cfg.get("outage_watch", {}).get("device_ids", [])
        self._all_online_prev: bool | None = None

    # -- IR scenes (LED) — fetched fresh so re-learns are picked up -----------
    # colour/mode key_names as Tuya learned them (note trailing spaces in some —
    # already handled by .strip() below)
    _LED_COLOR_KEYS = {"blue": "led blue colour", "green": "led green colour", "red": "led red colour"}
    _LED_MODE_KEYS = {"smooth": "led smooth mode", "fade": "led fade mode", "strobe": "led strobe mode"}

    def _load_ir_scenes(self):
        if not self.cloud.ready:
            return
        ir = self.cfg["devices"]["ir_hub"]["id"]
        remote = self.cfg["devices"]["led"]["remote_id"]
        by_name = {k.get("key_name", "").strip().lower(): k.get("code")
                   for k in self.cloud.ir_learned_keys(ir, remote)}
        self.led.code_on = by_name.get(_SCENE_KEYS[("led", "on")], "")
        self.led.code_off = by_name.get(_SCENE_KEYS[("led", "off")], "")
        self.led.colors = {name: by_name[key] for name, key in self._LED_COLOR_KEYS.items() if key in by_name}
        self.led.modes = {name: by_name[key] for name, key in self._LED_MODE_KEYS.items() if key in by_name}

    def _persist_desired(self):
        self.db.kv_set(DESIRED_KEY, json.dumps(self.desired))

    def _heater_dial_now(self) -> int:
        """Intensity to push the heater's dial RIGHT NOW, from OUR sensor —
        never the heater's own thermostat. Falls back to a mild default if
        we have no reading yet."""
        t_c = self.reading[0] if self.reading else self.thr.heater_on_t
        return heater_dial(t_c, self.thr)

    def ir_scene(self, device: str, command: str, value: str = ""):
        """Callback used by rules.py's run_actions() for led/projector actions."""
        if device == "led":
            if command == "color":
                self.led.set_color(value)
                self.desired["led_color"] = value
                return
            if command == "mode":
                self.led.set_mode(value)
                return
            on = command != "off"
            self.led.set(on)
            self.desired["led"] = on
        elif device == "projector":
            if command == "off":
                self.projector.set(False); self.desired["projector"] = False
            elif command == "on":
                self.projector.set(True); self.desired["projector"] = True
            else:  # "power" — explicit single-button press: flip current
                new = not self.projector._on
                self.projector.set(new); self.desired["projector"] = new
        self._persist_desired()

    # -- readings -------------------------------------------------------------
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

    # -- manual overrides -------------------------------------------------------
    def _override_active(self, device: str) -> bool:
        o = self.manual.get(device)
        if not o:
            return False
        if time.monotonic() > o["expires"]:
            del self.manual[device]
            self.db.log_event("manual", f"{device} manual override expired")
            return False
        return True

    def set_manual(self, device: str, on: bool | None = None, speed: int | None = None):
        """Called by the dashboard's manual-control endpoint."""
        minutes = self.cfg.get("manual_override_minutes", 30)
        expires = time.monotonic() + minutes * 60
        if device == "fan":
            self.fan.force_resend()
            self.fan.set_speed(speed or 0)
            self.desired["fan"] = speed or 0
            self.manual["fan"] = {"value": speed or 0, "expires": expires}
        elif device == "heater":
            self.heater.set(bool(on), temp=self._heater_dial_now() if on else None, fan="HIGH")
            self.desired["heater"] = bool(on)
            self.manual["heater"] = {"value": bool(on), "expires": expires}
        elif device == "led":
            self.led.set(bool(on), force=True)
            self.desired["led"] = bool(on)
            self.manual["led"] = {"value": bool(on), "expires": expires}
        elif device == "projector":
            self.projector.set(bool(on))
            self.desired["projector"] = bool(on)
            self.manual["projector"] = {"value": bool(on), "expires": expires}
        else:
            return False
        self._persist_desired()
        self.db.log_event("manual", f"{device} manually set to {speed if device=='fan' else on}",
                          {"minutes": minutes})
        return True

    # -- power outage watch -----------------------------------------------------
    def _check_outage(self):
        if not self._outage_ids or not self.cloud.ready:
            return
        states = [self.cloud.device_online(i) for i in self._outage_ids]
        known = [s for s in states if s is not None]
        if not known:
            return
        all_online = all(known) and len(known) == len(self._outage_ids)

        if self._all_online_prev is None:
            self._all_online_prev = all_online
            return

        if not all_online and self._all_online_prev:
            self.db.log_event("outage", "Power/connectivity loss suspected — watched devices offline.")
            self.alerts.append("Power loss suspected (devices offline).")
        elif all_online and not self._all_online_prev:
            self._recover_from_outage()

        self._all_online_prev = all_online

    def _recover_from_outage(self):
        """Devices reconnected. Heater and Projector are single-toggle remotes
        that always come back OFF after real power loss — reset our tracked
        state to match that known default, then replay the desired state so
        anything that SHOULD be on gets turned back on. LED has real on/off
        codes but auto-boots into an annoying flash-demo mode, so it's always
        force-resent regardless. Fan is a real cloud device — just resync."""
        self.db.log_event("outage", "Power restored — replaying last-known device states.",
                          {"desired": self.desired})
        self.alerts.append("Power restored — resyncing devices to prior state.")

        self.heater.force_state(on=False)
        self.heater.set(self.desired["heater"],
                        temp=self._heater_dial_now() if self.desired["heater"] else None,
                        fan="HIGH")

        self.projector.force_state(False)
        self.projector.set(self.desired["projector"])

        self.led.set(self.desired["led"], force=True)
        # "on" alone doesn't stop the flash-demo boot mode — the LED strip
        # apparently already considers itself "on" during that animation.
        # A specific COLOR command is what actually breaks it out of flashing
        # and holds a steady state — confirmed by a real outage test.
        if self.desired["led"] and self.desired.get("led_color"):
            self.led.set_color(self.desired["led_color"])

        self.fan.force_resend()
        self.fan.set_speed(self.desired["fan"])

    # -- one cycle ----------------------------------------------------------
    def tick(self):
        self.alerts = []
        self.escalation_notes = []
        now = time.monotonic()

        self._check_outage()

        r = self._read()
        if r is None or (now - self.last_good_read) > self.cfg["sensor_stale_seconds"]:
            self.alerts.append("Sensor stale — failsafe: fan + heater OFF.")
            if not self._override_active("heater"):
                self.heater.set(False)
            if not self._override_active("fan"):
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
            self.db.log_event("mode", f"Mode -> {target.mode}", {"reason": target.reason})

        # night fan cap
        n = self.cfg.get("night")
        if n and _in_window(datetime.now().time(), _hhmm(n["start"]), _hhmm(n["end"])):
            target.fan = min(target.fan, n["fan_cap"])

        # self-learning: escalate fan if the current combo is underperforming
        target.fan, notes = self.learner.escalate(target, target.fan)
        if notes:
            self.escalation_notes = notes
            self.alerts.extend(notes)
            for n_ in notes:
                self.db.log_event("escalate", n_)

        # actuate what THIS app owns: fan + heater — unless under manual override
        if not self._override_active("fan"):
            self.fan.set_speed(target.fan)
            self.desired["fan"] = target.fan
        if not self._override_active("heater"):
            self.heater.set(target.heater, temp=heater_dial(t_c, self.thr) if target.heater else None,
                            fan="HIGH")
            self.desired["heater"] = target.heater
        self._persist_desired()
        self.decision = target

        # user's conversational rules run AFTER the brain and override per-device
        ctx = {"temp": t_c, "rh": rh, "dew_point": target.dew_point,
               "spread": target.spread, "heat_index": target.heat_index,
               "hour": datetime.now().hour, "minute": datetime.now().minute,
               "weekday": datetime.now().weekday()}   # 0=Monday ... 6=Sunday
        self.fired_rules = []
        acts = {"fan": self.fan, "heater": self.heater, "ir_scene": self.ir_scene}
        for rule in self.rule_store.rules:
            if rule.enabled and rule.target == "app" and not rule.manual:
                try:
                    if safe_eval(rule.when, ctx):
                        run_actions(rule.actions, acts, ctx)
                        self.fired_rules.append(rule.name)
                        self.db.log_event("rule", f"Rule fired: {rule.name}")
                except Exception as e:
                    log.error("rule '%s' eval failed: %s", rule.name, e)

        # self-learning: log this tick + once-daily bounded threshold tuning
        cooler_inferred = infer_cooler_state(t_c, rh, self.thr)
        self.learner.record_tick(t_c, rh, target.mode, target.fan, target.heater, cooler_inferred)
        tuned = self.learner.daily_autotune(self.thr)
        if tuned:
            self.alerts.extend(f"Auto-tuned {t}" for t in tuned)

        self.history.append({"t": time.time(), "temp": t_c, "rh": rh,
                             "mode": target.mode, "hi": round(target.heat_index, 1)})
        log.info("%.1f°C %.0f%% | %s | fan=%s heater=%s | cooler(GH)=%s",
                 t_c, rh, target.mode, target.fan, target.heater, target.cooler)

    def run_rule(self, rid: str) -> bool:
        """Fire a manual scene's actions once, now."""
        rule = self.rule_store.get(rid)
        if not rule or rule.target != "app":
            return False
        r = self.reading or (26, 60)
        ctx = {"temp": r[0], "rh": r[1], "dew_point": dew_point(*r),
               "spread": r[0] - dew_point(*r), "heat_index": heat_index(*r),
               "hour": datetime.now().hour, "minute": datetime.now().minute,
               "weekday": datetime.now().weekday()}   # 0=Monday ... 6=Sunday
        run_actions(rule.actions, {"fan": self.fan, "heater": self.heater,
                                   "ir_scene": self.ir_scene}, ctx)
        self.db.log_event("rule", f"Manual scene run: {rule.name}")
        return True

    def _manual_remaining_min(self, device: str) -> float | None:
        o = self.manual.get(device)
        if not o:
            return None
        remaining = o["expires"] - time.monotonic()
        return round(remaining / 60, 1) if remaining > 0 else None

    def snapshot(self) -> dict:
        return {
            "reading": {"temp": self.reading[0], "rh": self.reading[1]} if self.reading else None,
            "decision": asdict(self.decision) if self.decision else None,
            "alerts": self.alerts,
            "live": self.cloud.ready,
            "cooler_note": "managed by Google Home",
            "fired_rules": self.fired_rules,
            "rules": [r.to_dict() for r in self.rule_store.rules],
            "history": list(self.history),
            "devices": {
                name: {"value": self.desired[name], "manual_min_left": self._manual_remaining_min(name)}
                for name in ("fan", "heater", "led", "projector")
            },
            "learned_rates": self.db.all_rates(),
        }

    def activity(self, limit=100) -> list[dict]:
        return self.db.recent_events(limit)

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
