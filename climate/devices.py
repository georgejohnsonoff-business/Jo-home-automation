"""
devices.py — thin device wrappers over the Tuya Cloud client.

Each wrapper knows its device id + datapoint codes (from config) and nothing
else. In simulation mode (no .env) the cloud client no-ops and logs, so these
all work for development without hardware.
"""
from __future__ import annotations
import logging

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


class Heater(ToggleIR):
    def __init__(self, cloud: TuyaCloud, ir_hub_id: str, cfg: dict):
        # Heater's config historically carries code_on/code_off, both equal
        # (it's a toggle) — accept either key name.
        code = cfg.get("code_on") or cfg.get("code_power", "")
        super().__init__(cloud, ir_hub_id, cfg["remote_id"], code, assumed_initial=False)


class Projector(ToggleIR):
    def __init__(self, cloud: TuyaCloud, ir_hub_id: str, cfg: dict):
        super().__init__(cloud, ir_hub_id, cfg["remote_id"], cfg.get("code_power", ""),
                        assumed_initial=False)


class LED:
    """LED strip has TRUE distinct on/off IR codes (not a toggle) — always
    safe to resend either command regardless of assumed state. Still tracks
    `_on` so the rest of the app has one consistent .set(bool) interface."""
    def __init__(self, cloud: TuyaCloud, ir_hub_id: str, remote_id: str,
                code_on: str, code_off: str):
        self.cloud = cloud
        self.ir = ir_hub_id
        self.remote = remote_id
        self.code_on = code_on
        self.code_off = code_off
        self._on = False

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

    def force_state(self, on: bool):
        self._on = on
