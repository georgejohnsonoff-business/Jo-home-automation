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
from dataclasses import asdict, replace
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
        self.cooler_inferred = False   # our best-effort guess at the GH-owned cooler's state

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
        self._led_recover_until: float | None = None   # monotonic deadline
        self._led_recover_next: float | None = None    # monotonic next resend

        # -- thermostat override: {"target": float, "expires": monotonic}
        self.thermostat: dict | None = None

    # -- IR scenes (LED) — fetched fresh so re-learns are picked up -----------
    # colour/mode key_names as Tuya learned them (note trailing spaces in some —
    # already handled by .strip() below)
    _LED_COLOR_KEYS = {"blue": "led blue colour", "green": "led green colour", "red": "led red colour"}
    _LED_MODE_KEYS = {"smooth": "led smooth mode", "fade": "led fade mode", "strobe": "led strobe mode"}
    _LED_BRIGHTNESS_KEYS = {"up": "led strip brightness incr", "down": "led brightness decrease"}

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
        self.led.brightness = {name: by_name[key] for name, key in self._LED_BRIGHTNESS_KEYS.items() if key in by_name}

    def _persist_desired(self):
        self.db.kv_set(DESIRED_KEY, json.dumps(self.desired))

    def _heater_dial_now(self) -> int:
        """Intensity to push the heater's dial RIGHT NOW, from OUR sensor —
        never the heater's own thermostat. Falls back to a mild default if
        we have no reading yet. Respects an active thermostat override."""
        thr = self._active_thresholds()
        t_c = self.reading[0] if self.reading else thr.heater_on_t
        return heater_dial(t_c, thr)

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

    def set_manual(self, device: str, on: bool | None = None, speed: int | None = None,
                   delta: int | None = None, color: str | None = None):
        """
        Called by the dashboard's manual-control endpoint.
        `speed`   — fan: absolute slider value, clamped to the fan cap.
        `delta`   — heater: +1/-1 dial step; led: +1/-1 brightness step
                    (bypasses the heater's automatic-correction deadband —
                    a deliberate manual click should always do something).
        `color`   — led: color name button (red/green/blue).
        """
        minutes = self.cfg.get("manual_override_minutes", 30)
        expires = time.monotonic() + minutes * 60
        if device == "fan":
            speed = max(0, min(self.thr.fan_cap, speed if speed is not None else 0))
            self.fan.force_resend()
            self.fan.set_speed(speed)
            self.desired["fan"] = speed
            self.manual["fan"] = {"value": speed, "expires": expires}
        elif device == "heater":
            if delta is not None:
                base = self.heater._temp if self.heater._on else self._heater_dial_now()
                self.heater.set(True, temp=base + delta, fan="HIGH", force_step=True)
                self.desired["heater"] = True
                self.manual["heater"] = {"value": True, "expires": expires}
            else:
                self.heater.set(bool(on), temp=self._heater_dial_now() if on else None, fan="HIGH")
                self.desired["heater"] = bool(on)
                self.manual["heater"] = {"value": bool(on), "expires": expires}
        elif device == "led":
            if color is not None:
                self.led.set_color(color)
                self.desired["led_color"] = color
                self.manual["led"] = {"value": self.desired.get("led", True), "expires": expires}
            elif delta is not None:
                self.led.press_brightness("up" if delta > 0 else "down")
                self.manual["led"] = {"value": self.desired.get("led", True), "expires": expires}
            elif on is not None:
                self.led.set(bool(on), force=True)
                self.desired["led"] = bool(on)
                self.manual["led"] = {"value": bool(on), "expires": expires}
            else:
                return False
        elif device == "projector":
            self.projector.set(bool(on))
            self.desired["projector"] = bool(on)
            self.manual["projector"] = {"value": bool(on), "expires": expires}
        else:
            return False
        self._persist_desired()
        self.db.log_event("manual", f"{device} manually adjusted",
                          {"minutes": minutes, "on": on, "speed": speed, "delta": delta, "color": color})
        return True

    def confirm_state(self, device: str, on: bool) -> bool:
        """
        User-reported ground truth. Corrects our TRACKED belief to match
        reality WITHOUT sending any command — use this when you know the
        actual device state (you looked at it) and it might differ from what
        the app assumes (a toggle-only IR remote has no feedback channel, so
        this can drift after cross-talk, a missed pulse, or a restart).
        Does not touch the manual-override window — this is a correction,
        not a new command.
        """
        if device == "fan":
            self.fan.force_resend()
            if not on:
                self.fan.set_speed(0)
                self.desired["fan"] = 0
        elif device == "heater":
            self.heater.force_state(on=on, temp=self.heater._temp, fan=self.heater._fan)
            self.desired["heater"] = on
        elif device == "led":
            self.led.force_state(on)
            self.desired["led"] = on
        elif device == "projector":
            self.projector.force_state(on)
            self.desired["projector"] = on
        else:
            return False
        self._persist_desired()
        self.db.log_event("confirm", f"{device} confirmed {'ON' if on else 'OFF'} by user (belief corrected, no command sent)")
        return True

    # -- thermostat override ------------------------------------------------
    def set_thermostat(self, target: float, hours: float = 6.0):
        """
        Set a single target temperature; fan + heater automation works toward
        it instead of the normal comfort-band thresholds, for `hours` (then
        automatically reverts). Recentered band: heater kicks in 1° below
        target, cooler 1° above — both settle back exactly at target.
        """
        expires = time.monotonic() + hours * 3600
        self.thermostat = {"target": target, "expires": expires}
        self.db.log_event("thermostat", f"Thermostat set to {target}° for {hours:.1f}h")

    def clear_thermostat(self):
        self.thermostat = None
        self.db.log_event("thermostat", "Thermostat override cancelled — back to normal automation")

    def _active_thresholds(self) -> Thresholds:
        """The Thresholds this tick should actually use — thermostat-recentered
        if a still-active override is set, otherwise the base config."""
        if self.thermostat is None:
            return self.thr
        if time.monotonic() >= self.thermostat["expires"]:
            self.db.log_event("thermostat", "Thermostat override expired — back to normal automation")
            self.thermostat = None
            return self.thr
        # Same shape as the permanent defaults (comfort_lo=27.2, cooler_on_hi
        # =27.5, cooler_off_hi=26.5, heater on/off=26.5) — `target` plays the
        # role of cooler_on_hi. Fan continuously ramps from 0.3° below target
        # (off) through target (~3) and beyond; heater snaps full 1° below
        # target, independent of the fan floor — no easing on either edge.
        target = self.thermostat["target"]
        return replace(self.thr,
                      comfort_lo=target - 0.3,
                      cooler_on_hi=target,
                      cooler_off_hi=target - 1.0,
                      heater_on_t=target - 1.0,
                      heater_off_t=target - 1.0)

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
        # A specific COLOR command is what actually breaks it out of
        # flashing, but a SINGLE resend isn't reliable enough on its own
        # (confirmed: it still flashed again later) — so this now repeats
        # the colour command every 4 min for 30 min to make it actually
        # stick, instead of trusting one IR pulse to land.
        if self.desired["led"] and self.desired.get("led_color"):
            self.led.set_color(self.desired["led_color"])
            now = time.monotonic()
            self._led_recover_until = now + 30 * 60
            self._led_recover_next = now + 4 * 60

        self.fan.force_resend()
        self.fan.set_speed(self.desired["fan"])

    def _led_recover_tick(self):
        """Keep re-sending the LED colour every 4 min for 30 min after an
        outage recovery — see the comment in _recover_from_outage()."""
        if self._led_recover_until is None:
            return
        now = time.monotonic()
        if now >= self._led_recover_until:
            self._led_recover_until = None
            self._led_recover_next = None
            return
        if now >= self._led_recover_next:
            self.led.set_color(self.desired["led_color"])
            self._led_recover_next = now + 4 * 60
            self.db.log_event("outage", "Re-sent LED colour to hold it out of flash-demo mode.")

    # -- one cycle ----------------------------------------------------------
    def tick(self):
        self.alerts = []
        self.escalation_notes = []
        now = time.monotonic()

        self._check_outage()
        self._led_recover_tick()
        if self.cloud.ready:
            self.fan.sync_from_cloud()   # catch any real-world drift before deciding whether to command it

        r = self._read()
        if r is None or (now - self.last_good_read) > self.cfg["sensor_stale_seconds"]:
            self.alerts.append("Sensor stale — failsafe: fan + heater OFF.")
            if not self._override_active("heater"):
                self.heater.set(False)
            if not self._override_active("fan"):
                self.fan.set_speed(0)
            return

        t_c, rh = r

        # recent warming rate (°C/min of feels-like, last ~10 min) — lets
        # decide() hand off to the cooler early on a fast heat-up instead of
        # waiting until the fan is already at its cap.
        hi_now = heat_index(t_c, rh)
        now_ts = time.time()
        window = [h for h in self.history if now_ts - h["t"] <= 600]
        if window:
            span_min = max((now_ts - window[0]["t"]) / 60.0, 1.0)
            hi_trend = (hi_now - window[0]["hi"]) / span_min
        else:
            hi_trend = 0.0

        active_thr = self._active_thresholds()   # thermostat-recentered if an override is active
        target = decide(t_c, rh, self.decision, active_thr, hi_trend=hi_trend)

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

        # cooldown window (e.g. Mon-Thu 11:00-17:30, away at office) — rest
        # every gadget regardless of what the room's climate would otherwise
        # ask for. Forces the DECISION here (not a post-hoc rule action)
        # specifically so fan/heater never get commanded on and then
        # immediately back off again within the same tick.
        cd = self.cfg.get("cooldown")
        if cd and datetime.now().weekday() in cd.get("weekdays", []) and \
                _in_window(datetime.now().time(), _hhmm(cd["start"]), _hhmm(cd["end"])):
            target.fan = 0
            target.heater = False
            # Heater is a single-press toggle with no feedback channel — set()
            # only presses if it believes it's currently ON, so this can never
            # accidentally send an ON press; it's a no-op once truly off,
            # matching the outage-recovery logic's own tracked-state model.
            if not self._override_active("led") and self.desired.get("led"):
                # LED has a genuine dedicated OFF code (not a toggle), so this
                # is a real, distinct command, not a risky guess — and set()
                # without force only actually sends it once, on the on->off
                # transition, not every tick for the whole window.
                self.led.set(False)
                self.desired["led"] = False

        # self-learning: escalate fan if the current combo is underperforming.
        # Skipped while a thermostat override is active — escalation compares
        # against learned rates from the OPEN-ENDED comfort mode, which can
        # easily look "too slow" against a deliberately relaxed thermostat
        # target and shove the fan right back up, overriding the user's
        # explicit choice.
        if self.thermostat is None:
            target.fan, notes = self.learner.escalate(target, target.fan, fan_cap=self.thr.fan_cap)
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
            self.heater.set(target.heater, temp=heater_dial(t_c, active_thr) if target.heater else None,
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
        self.cooler_inferred = infer_cooler_state(t_c, rh, self.cooler_inferred, self.thr)
        self.learner.record_tick(t_c, rh, target.mode, target.fan, target.heater, self.cooler_inferred)
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
            "cooler_inferred": self.cooler_inferred,
            "fan_cap": self.thr.fan_cap,
            "thermostat": ({"target": self.thermostat["target"],
                          "minutes_left": max(0, (self.thermostat["expires"] - time.monotonic()) / 60)}
                         if self.thermostat else None),
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
