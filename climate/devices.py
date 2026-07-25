"""
devices.py — thin device wrappers over the Tuya Cloud client.

Each wrapper knows its device id + datapoint codes (from config) and nothing
else. In simulation mode (no .env) the cloud client no-ops and logs, so these
all work for development without hardware.
"""
from __future__ import annotations
import logging
import time

from .cloud import TuyaCloud

log = logging.getLogger("devices")


class Sensor:
    def __init__(self, cloud: TuyaCloud, cfg: dict):
        self.cloud = cloud
        self.cfg = cfg

    def read(self) -> tuple[float, float] | None:
        """(temp_c, humidity_pct) or None if the read failed."""
        if not self.cloud.ready:
            return None                         # loop supplies a simulated value
        s = self.cloud.status(self.cfg["id"])
        if self.cfg["code_temp"] not in s or self.cfg["code_humidity"] not in s:
            log.error("Sensor status missing expected codes: %s", s)
            return None
        temp = s[self.cfg["code_temp"]] / self.cfg.get("temp_divisor", 1)
        hum = s[self.cfg["code_humidity"]] / self.cfg.get("humidity_divisor", 1)
        return (float(temp), float(hum))


class Fan:
    """switch_fan (bool) + fan_speed_enum ('1'..'5')."""
    def __init__(self, cloud: TuyaCloud, cfg: dict):
        self.cloud = cloud
        self.cfg = cfg
        self._speed = -1

    def set_speed(self, speed: int):
        if speed == self._speed:
            return
        self._speed = speed
        if speed <= 0:
            self.cloud.commands(self.cfg["id"], [{"code": self.cfg["code_power"], "value": False}])
            return
        value = self.cfg["speed_values"][min(speed, len(self.cfg["speed_values"])) - 1]
        self.cloud.commands(self.cfg["id"], [
            {"code": self.cfg["code_power"], "value": True},
            {"code": self.cfg["code_speed"], "value": value},
        ])

    def force_resend(self):
        """Drop the cached speed so the next set_speed() always re-issues the
        command — used after a detected power outage, since the physical fan
        may have reset to a different speed than we last cached."""
        self._speed = -1


class ToggleIR:
    """
    A single-button IR toggle (heater power, projector power). These remotes
    have exactly ONE code that flips whatever state the device is currently
    in — unlike the LED strip, which has distinct on/off codes. That makes
    them desync-prone: if our tracked `_on` drifts from physical reality
    (e.g. a power outage reset the device to a known default), blindly
    calling set() would fire the toggle and land on the WRONG state.

    force_state() lets the caller assert "we know it's actually X now"
    (e.g. after an outage, these always come back off) without sending a
    command — then a normal set() call fires the toggle only if actually needed.
    """
    def __init__(self, cloud: TuyaCloud, ir_hub_id: str, remote_id: str, code: str,
                assumed_initial: bool = False):
        self.cloud = cloud
        self.ir = ir_hub_id
        self.remote = remote_id
        self.code = code
        self._on = assumed_initial

    def set(self, on: bool):
        if on == self._on:
            return
        if not self.code:
            log.warning("Toggle IR code not set for remote %s — skipping.", self.remote)
            self._on = on
            return
        self.cloud.ir_send_learned(self.ir, self.remote, self.code)
        self._on = on

    def force_state(self, on: bool):
        """Assert the tracked state WITHOUT sending a command — use when we
        know the physical device reset to a known default (e.g. power loss)."""
        self._on = on


class HandyHeater:
    """
    Kanupriya handy heater — NOT a simple toggle. Confirmed remote behavior:
      power — single toggle pulse. Powering ON resets the unit's OWN state to
              temp=25C, fan=HIGH, timer=0(off) — a hardware default we must
              resync our tracking to, not something we choose.
      temp  — a real thermostat dial, 15-32C, one pulse per degree via
              increase/decrease. We treat it purely as an INTENSITY KNOB
              driven every tick by OUR WiFi sensor (brain.heater_dial()) —
              the heater's own thermostat/sensor is never trusted, since it
              sits right next to its own heat output and would under-read
              the room. Reaching a target means pressing the delta in steps.
      fan   — strict 2-state cycle HIGH<->LOW. Any single press flips it.
      timer — 13-state cycle 0(off)..12h, one-directional (no decrease code).
              Not driven by automation (our own loop already decides on/off
              every 30s) — wired in for a possible future manual control.
    """
    TEMP_MIN, TEMP_MAX = 15, 32
    RESET_TEMP = 25
    RESET_FAN = "HIGH"

    def __init__(self, cloud: TuyaCloud, ir_hub_id: str, cfg: dict, pulse_delay: float = 0.35):
        self.cloud = cloud
        self.ir = ir_hub_id
        self.remote = cfg["remote_id"]
        self.codes = {
            "power": cfg.get("code_power") or cfg.get("code_on", ""),
            "increase": cfg.get("code_increase", ""),
            "decrease": cfg.get("code_decrease", ""),
            "fan": cfg.get("code_fan", ""),
            "timer": cfg.get("code_timer", ""),
        }
        self.pulse_delay = pulse_delay
        self._on = False
        self._temp = self.RESET_TEMP
        self._fan = self.RESET_FAN
        self._timer = 0

    def _press(self, key: str, times: int = 1):
        code = self.codes.get(key)
        if not code:
            log.warning("Heater code '%s' not set — skipping", key)
            return
        for i in range(times):
            self.cloud.ir_send_learned(self.ir, self.remote, code)
            if i < times - 1:
                time.sleep(self.pulse_delay)

    def set(self, on: bool, temp: int | None = None, fan: str | None = None):
        if on and not self._on:
            self._press("power")
            self._on = True
            # hardware resets itself to these on power-on — resync, don't guess
            self._temp = self.RESET_TEMP
            self._fan = self.RESET_FAN
            self._timer = 0
        elif not on and self._on:
            self._press("power")
            self._on = False
            return

        if not self._on:
            return

        if temp is not None:
            target = max(self.TEMP_MIN, min(self.TEMP_MAX, round(temp)))
            delta = target - self._temp
            if delta > 0:
                self._press("increase", delta)
            elif delta < 0:
                self._press("decrease", -delta)
            self._temp = target

        if fan is not None and fan != self._fan:
            self._press("fan")   # strict 2-state cycle — one press always flips it
            self._fan = fan

    def set_timer(self, hours: int):
        if not self._on:
            return
        target = max(0, min(12, hours))
        presses = (target - self._timer) % 13   # one-directional cycle, mod handles wrap
        if presses:
            self._press("timer", presses)
        self._timer = target

    def force_state(self, on: bool = False, temp: int | None = None,
                    fan: str | None = None, timer: int | None = None):
        """Assert tracked state WITHOUT sending commands — use when we know
        the physical unit already reset (e.g. after a power outage, it comes
        back OFF, per confirmed behavior)."""
        self._on = on
        self._temp = temp if temp is not None else self.RESET_TEMP
        self._fan = fan if fan is not None else self.RESET_FAN
        self._timer = timer if timer is not None else 0


class Projector(ToggleIR):
    def __init__(self, cloud: TuyaCloud, ir_hub_id: str, cfg: dict):
        super().__init__(cloud, ir_hub_id, cfg["remote_id"], cfg.get("code_power", ""),
                        assumed_initial=False)


class LED:
    """LED strip — a 24-key NEC remote; on/off, colors, and modes are all
    TRUE distinct IR codes (not toggles) — always safe to resend regardless
    of assumed state. Only a subset of the 24 buttons are learned so far
    (see config.yaml comment); colors/modes dicts hold whatever IS learned,
    keyed by name — missing ones just log a warning instead of erroring."""
    def __init__(self, cloud: TuyaCloud, ir_hub_id: str, remote_id: str,
                code_on: str, code_off: str):
        self.cloud = cloud
        self.ir = ir_hub_id
        self.remote = remote_id
        self.code_on = code_on
        self.code_off = code_off
        self.colors: dict[str, str] = {}     # e.g. {"blue": "<code>", "red": ...}
        self.modes: dict[str, str] = {}       # e.g. {"smooth": "<code>", ...}
        self._on = False
        self._color: str | None = None

    def set(self, on: bool, force: bool = False):
        """force=True always resends — needed after power restore, since the
        LED strip's own hardware auto-boots into a flashing demo mode that
        looks 'on' even if we last wanted it off."""
        if on == self._on and not force:
            return
        code = self.code_on if on else self.code_off
        if code:
            self.cloud.ir_send_learned(self.ir, self.remote, code)
        self._on = on

    def set_color(self, name: str):
        code = self.colors.get(name.lower())
        if not code:
            log.warning("LED color '%s' not learned yet — skipping.", name)
            return
        self.cloud.ir_send_learned(self.ir, self.remote, code)
        self._color = name.lower()

    def set_mode(self, name: str):
        code = self.modes.get(name.lower())
        if not code:
            log.warning("LED mode '%s' not learned yet — skipping.", name)
            return
        self.cloud.ir_send_learned(self.ir, self.remote, code)

    def force_state(self, on: bool):
        self._on = on
