"""
learn.py — the self-learning layer.

Three jobs:
  1. infer_cooler_state()  — Google Home owns the cooler, so we can't read its
     state. But its trigger IS the corrected script we wrote, using the same
     sensor thresholds we already have — so we replay that logic to guess
     whether the cooler was probably on. Approximate, not authoritative.
  2. record_tick()          — logs each reading and, when the actuator combo
     (fan speed / heater / inferred cooler) has held for a while, computes the
     real-world cooling/drying rate for that combo and updates a rolling
     average in the store. This IS the "learning" — plain stats, no black box.
  3. escalate()              — real-time: if the current combo has held for
     a while and isn't hitting a minimum useful rate, push fan higher and/or
     raise an alert. Runs every tick, useful from day one.
  4. daily_autotune()         — once/day: nudges a small set of thresholds by
     a bounded step based on how much time was spent uncomfortable, using the
     rates learned above. Always logged as an event — visible, not silent.
"""
from __future__ import annotations
import logging
import time

from .brain import Thresholds

log = logging.getLogger("learn")


def infer_cooler_state(temp: float, rh: float, thr: Thresholds) -> bool:
    """Best-effort replay of the Google Home cooler script's own trigger logic."""
    hot_dry = temp > 29 and rh < 55
    muggy_off = rh > 62
    cool_off = temp < 25
    # Without hysteresis-state we can't know for certain; hot_dry firing is the
    # strongest signal. Treat "not muggy and not cool and hot_dry" as likely-on.
    return hot_dry and not muggy_off and not cool_off


def _combo_key(fan: int, heater: bool, cooler: bool) -> str:
    return f"fan{fan}_heater{'T' if heater else 'F'}_cooler{'T' if cooler else 'F'}"


class Learner:
    def __init__(self, store, cfg: dict):
        self.store = store
        self.cfg = cfg.get("learning", {})
        self.enabled = self.cfg.get("enabled", True)
        self._segment_start: dict | None = None   # {t, temp, rh, combo}

    # -- 2. record + learn rates ---------------------------------------------
    def record_tick(self, temp, rh, mode, fan, heater, cooler_inferred):
        self.store.log_reading(temp, rh, None, None, None, mode, fan, heater, cooler_inferred)
        if not self.enabled:
            return
        combo = _combo_key(fan, heater, cooler_inferred)
        now = time.time()
        if self._segment_start is None or self._segment_start["combo"] != combo:
            # combo changed — close out the previous segment if long enough
            if self._segment_start is not None:
                self._close_segment(now, temp, rh)
            self._segment_start = {"t": now, "temp": temp, "rh": rh, "combo": combo}

    def _close_segment(self, now, temp, rh):
        s = self._segment_start
        minutes = (now - s["t"]) / 60.0
        if minutes < 5:      # too short to learn anything meaningful
            return
        temp_slope = (temp - s["temp"]) / minutes    # °C/min, negative = cooling
        rh_slope = (rh - s["rh"]) / minutes
        self.store.update_rate(s["combo"], temp_slope, rh_slope)
        log.info("learned %s: %.3f °C/min, %.3f %%RH/min", s["combo"], temp_slope, rh_slope)

    # -- 3. real-time escalation ----------------------------------------------
    def escalate(self, decision, fan_current: int) -> tuple[int, list[str]]:
        """Returns (possibly-bumped fan speed, alerts)."""
        alerts = []
        if not self.enabled or self._segment_start is None:
            return fan_current, alerts
        minutes = (time.time() - self._segment_start["t"]) / 60.0
        if minutes < self.cfg.get("escalate_after_min", 20):
            return fan_current, alerts
        if decision.mode not in ("WARM · DRY", "WARM · MARGINAL", "HOT · DRY"):
            return fan_current, alerts

        combo = self._segment_start["combo"]
        rate = self.store.get_rate(combo)
        min_rate = self.cfg.get("min_cool_rate_c_per_min", 0.03)
        if rate and rate["samples"] >= 2 and rate["temp_slope"] > -min_rate:
            if fan_current < 5:
                alerts.append(f"Fan-only cooling underperforming ({rate['temp_slope']:.2f}°C/min) "
                             f"after {minutes:.0f} min — escalating fan speed.")
                return min(5, fan_current + 1), alerts
            alerts.append(f"Room not cooling despite fan at max for {minutes:.0f} min — "
                         f"cooler may not be engaging. Check Google Home.")
        return fan_current, alerts

    # -- 4. daily bounded auto-tune -------------------------------------------
    def daily_autotune(self, thr: Thresholds) -> list[str]:
        ac = self.cfg.get("autotune", {})
        if not ac.get("enabled", True):
            return []
        today = time.strftime("%Y-%m-%d")
        if self.store.kv_get("last_autotune_date") == today:
            return []
        self.store.kv_set("last_autotune_date", today)

        readings = self.store.readings_since(24 * 3600)
        if len(readings) < ac.get("min_samples", 20):
            return []

        step = ac.get("step", 0.2)
        bounds = ac.get("bounds", {})
        changes = []

        too_hot = sum(1 for r in readings if r["temp"] and r["temp"] > thr.cooler_on_hi) / len(readings)
        too_cold = sum(1 for r in readings if r["temp"] and r["temp"] < thr.heater_on_t + 1) / len(readings)

        if too_hot > 0.30 and "cooler_on_hi" in bounds:
            lo, hi = bounds["cooler_on_hi"]
            new = max(lo, thr.cooler_on_hi - step)
            if new != thr.cooler_on_hi:
                changes.append(("cooler_on_hi", thr.cooler_on_hi, new,
                               f"room was too hot {too_hot*100:.0f}% of the last 24h"))
        elif too_hot < 0.05 and "cooler_on_hi" in bounds:
            lo, hi = bounds["cooler_on_hi"]
            new = min(hi, thr.cooler_on_hi + step / 2)
            if new != thr.cooler_on_hi:
                changes.append(("cooler_on_hi", thr.cooler_on_hi, new,
                               "comfortable >95% of the time — relaxing slightly"))

        if too_cold > 0.20 and "heater_on_t" in bounds:
            lo, hi = bounds["heater_on_t"]
            new = min(hi, thr.heater_on_t + step)
            if new != thr.heater_on_t:
                changes.append(("heater_on_t", thr.heater_on_t, new,
                               f"room was cold {too_cold*100:.0f}% of the last 24h"))

        for name, old, new, why in changes:
            self.store.set_threshold(name, new)
            setattr(thr, name, new)
            self.store.log_event("autotune", f"{name}: {old:.1f} -> {new:.1f}", {"why": why})

        return [f"{n}: {o:.1f}→{v:.1f} ({w})" for n, o, v, w in changes]
