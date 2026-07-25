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

    def sync_from_cloud(self):
        """
        Reconcile our tracked speed with the REAL device state. Unlike the
        IR-only devices (heater/LED/projector), the fan has genuine
        bidirectional Tuya status — we don't have to trust our own
        write-only cache forever. Confirmed live: a stale cache (believed
        speed 1) silently stopped sending commands for over an hour while
        the real fan sat at speed 3, since set_speed() no-ops when the
        requested value already matches what we (wrongly) think is current.
        Call this once per tick, before deciding whether to send anything.
        """
        status = self.cloud.status(self.cfg["id"])
        if not status:
            return   # cloud unreachable this tick — keep the existing belief, don't guess
        if not status.get(self.cfg["code_power"], True):
            self._speed = 0
            return
        val = status.get(self.cfg["code_speed"])
        if val in self.cfg["speed_values"]:
            real_speed = self.cfg["speed_values"].index(val) + 1
            if real_speed != self._speed:
                log.warning("Fan cache was stale: believed %s, cloud reports %s — correcting.",
                           self._speed, real_speed)
                self._speed = real_speed


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
    MAX_STEP_PER_CALL = 1   # one press per call, no burst at all; larger deltas
                            # close over successive ticks instead of a rapid-fire
                            # run (confirmed the projector's receiver on the
                            # same IR hub misreads a tight burst of repeated
                            # pulses as its own "OK" button — re-learning the
                            # code and shortening the burst to 3 with a longer
                            # gap didn't stop it, so the exposure itself is the
                            # risk, not the pattern. A single, isolated pulse is
                            # the minimum footprint software can produce.)
    TEMP_DEADBAND = 2       # ignore dial corrections smaller than this — most
                            # single-degree "corrections" are just sensor noise,
                            # not worth an extra IR pulse and the risk that
                            # comes with it

    def __init__(self, cloud: TuyaCloud, ir_hub_id: str, cfg: dict, pulse_delay: float = 1.1):
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

    def set(self, on: bool, temp: int | None = None, fan: str | None = None, force_step: bool = False):
        """
        `force_step=True` bypasses the deadband (but NOT the per-call step
        cap) — use for an explicit manual +/- press, where the deadband
        would otherwise silently eat a deliberate single-degree click. The
        deadband stays on for the automatic per-tick dial correction, where
        it's suppressing sensor-noise jitter, not user intent.
        """
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
            if force_step or abs(delta) >= self.TEMP_DEADBAND:
                step = max(-self.MAX_STEP_PER_CALL, min(self.MAX_STEP_PER_CALL, delta))
                if step > 0:
                    self._press("increase", step)
                elif step < 0:
                    self._press("decrease", -step)
                self._temp += step   # any remainder closes on later ticks, not this call
            # else: within the deadband — not worth an IR pulse for a 1° nudge

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
        self.colors: dict[str, str] = {}      # e.g. {"blue": "<code>", "red": ...}
        self.modes: dict[str, str] = {}       # e.g. {"smooth": "<code>", ...}
        self.brightness: dict[str, str] = {}  # {"up": "<code>", "down": "<code>"}
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

    def press_brightness(self, direction: str):
        """direction: 'up' or 'down' — one press per call, same reasoning as
        the heater's step cap: minimal IR footprint per user action."""
        code = self.brightness.get(direction.lower())
        if not code:
            log.warning("LED brightness '%s' not learned yet — skipping.", direction)
            return
        self.cloud.ir_send_learned(self.ir, self.remote, code)

    def force_state(self, on: bool):
        self._on = on
