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


class Heater:
    """Learned IR remote fired through the Smart IR hub."""
    def __init__(self, cloud: TuyaCloud, ir_hub_id: str, cfg: dict):
        self.cloud = cloud
        self.ir = ir_hub_id
        self.remote = cfg["remote_id"]
        self.code_on = cfg.get("code_on", "")
        self.code_off = cfg.get("code_off", "")
        # Heater IR is a single power TOGGLE. Assume it starts OFF so the first
        # decide()=off never fires a spurious toggle that would switch it ON.
        self._on = False

    def set(self, on: bool):
        if on == self._on:
            return
        code = self.code_on if on else self.code_off
        if not code:
            log.warning("Heater IR code for %s not set in config — skipping.",
                        "on" if on else "off")
            self._on = on
            return
        self.cloud.ir_send_learned(self.ir, self.remote, code)
        self._on = on
