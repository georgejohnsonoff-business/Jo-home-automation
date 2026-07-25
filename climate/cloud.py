"""
cloud.py — Tuya Cloud API client (the I/O layer for this setup).

The temp/humidity sensor is battery/cloud-reporting, so we talk to the Tuya
Cloud API rather than local tinytuya. One TuyaCloud instance is shared by all
device wrappers in devices.py.

Credentials come from a local .env (never committed, never printed):
    TUYA_ACCESS_ID, TUYA_ACCESS_SECRET, TUYA_API_REGION=in

Discovered for George's project (India DC, openapi.tuyain.com):
  sensor  d7802e582e33449a8bx6ao : va_temperature (/10 = C), va_humidity (%)
  fan     d73d1055a9e5f5af92yods : switch_fan (bool), fan_speed_enum ("1".."5")
  heater  d7c92742587535876dvviu : learned IR remote (no DPs) via Smart IR
  ir hub  d7b8d9fc6b8198fe09yb8f
"""
from __future__ import annotations
import logging
import os

log = logging.getLogger("cloud")

try:
    from tuya_connector import TuyaOpenAPI
except ImportError:
    TuyaOpenAPI = None

ENDPOINTS = {
    "in": "https://openapi.tuyain.com",
    "eu": "https://openapi.tuyaeu.com",
    "us": "https://openapi.tuyaus.com",
    "cn": "https://openapi.tuyacn.com",
}


class TuyaCloud:
    def __init__(self):
        self.access_id = os.getenv("TUYA_ACCESS_ID", "")
        self.access_secret = os.getenv("TUYA_ACCESS_SECRET", "")
        region = os.getenv("TUYA_API_REGION", "in").lower()
        self.endpoint = os.getenv("TUYA_API_ENDPOINT") or ENDPOINTS.get(region, ENDPOINTS["in"])
        self.ready = bool(self.access_id and self.access_secret and TuyaOpenAPI)
        self._api = None
        if self.ready:
            self._api = TuyaOpenAPI(self.endpoint, self.access_id, self.access_secret)
            self._api.connect()
            log.info("Tuya Cloud connected (%s).", self.endpoint)
        else:
            log.warning("Tuya Cloud not configured (.env missing) — simulation mode.")

    # -- reads --------------------------------------------------------------
    def status(self, device_id: str) -> dict:
        """{code: value} for a device, or {} on failure."""
        if not self.ready:
            return {}
        try:
            r = self._api.get(f"/v1.0/iot-03/devices/{device_id}/status")
            if not r.get("success"):
                log.error("status(%s): %s", device_id, r.get("msg"))
                return {}
            return {i["code"]: i["value"] for i in r["result"]}
        except Exception as e:
            log.error("status(%s) error: %s", device_id, e)
            return {}

    # -- writes -------------------------------------------------------------
    def commands(self, device_id: str, cmds: list[dict]) -> bool:
        """Send one or more {code, value} commands in a single call."""
        if not self.ready:
            log.info("[sim] %s <- %s", device_id, cmds)
            return True
        try:
            r = self._api.post(f"/v1.0/iot-03/devices/{device_id}/commands",
                               {"commands": cmds})
            if not r.get("success"):
                log.error("commands(%s) failed: %s", device_id, r.get("msg"))
            return bool(r.get("success"))
        except Exception as e:
            log.error("commands error: %s", e)
            return False

    # -- infrared (learned remotes: heater / projector) ---------------------
    def ir_learned_keys(self, ir_id: str, remote_id: str) -> list:
        """List the learned key codes for a DIY remote. Run once to discover
        which key powers the heater, then put it in config."""
        if not self.ready:
            return []
        r = self._api.get(f"/v2.0/infrareds/{ir_id}/remotes/{remote_id}/learning-codes")
        return r.get("result", []) if r.get("success") else []

    def ir_send_learned(self, ir_id: str, remote_id: str, code: str) -> bool:
        """Fire a learned IR code (the raw 'code' string from ir_learned_keys)."""
        if not self.ready:
            log.info("[sim] IR %s/%s send", ir_id, remote_id)
            return True
        try:
            r = self._api.post(
                f"/v2.0/infrareds/{ir_id}/remotes/{remote_id}/learning-codes",
                {"code": code},
            )
            if not r.get("success"):
                log.error("IR send failed: %s", r.get("msg"))
            return bool(r.get("success"))
        except Exception as e:
            log.error("IR send error: %s", e)
            return False
