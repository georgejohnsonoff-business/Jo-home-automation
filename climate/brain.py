"""
brain.py — the decision engine. Pure functions, no hardware, no I/O.

This is the exact logic from the live simulator, ported to Python and
extended with hysteresis (different on/off thresholds so nothing flaps).
Everything here is testable in isolation — see test_brain.py.
"""
from __future__ import annotations
import math
from dataclasses import dataclass


# ----------------------------------------------------------------------------
# Psychrometrics — real formulas, not approximations
# ----------------------------------------------------------------------------
def dew_point(t_c: float, rh: float) -> float:
    """Magnus-Tetens dew point in °C. rh is 0-100."""
    a, b = 17.27, 237.7
    gamma = (a * t_c) / (b + t_c) + math.log(max(rh, 1e-3) / 100.0)
    return (b * gamma) / (a - gamma)


def heat_index(t_c: float, rh: float) -> float:
    """NOAA Rothfusz 'feels like' in °C. Below ~27°C it collapses to t_c."""
    t_f = t_c * 9 / 5 + 32
    if t_f < 80:
        return t_c
    hi = (-42.379 + 2.04901523 * t_f + 10.14333127 * rh
          - 0.22475541 * t_f * rh - 6.83783e-3 * t_f * t_f
          - 5.481717e-2 * rh * rh + 1.22874e-3 * t_f * t_f * rh
          + 8.5282e-4 * t_f * rh * rh - 1.99e-6 * t_f * t_f * rh * rh)
    if rh < 13 and 80 <= t_f <= 112:
        hi -= ((13 - rh) / 4) * math.sqrt((17 - abs(t_f - 95)) / 17)
    elif rh > 85 and 80 <= t_f <= 87:
        hi += ((rh - 85) / 10) * ((87 - t_f) / 5)
    return (hi - 32) * 5 / 9


def fan_tier(hi: float) -> int:
    """Fan speed 1-5 scaled to feels-like temperature."""
    if hi < 24: return 1
    if hi < 26: return 2
    if hi < 28: return 3
    if hi < 30: return 4
    return 5


# ----------------------------------------------------------------------------
# Decision
# ----------------------------------------------------------------------------
@dataclass
class Decision:
    mode: str
    cooler: bool
    fan: int          # 0 = off, 1-5 speed
    heater: bool
    reason: str
    # derived, for the dashboard
    dew_point: float = 0.0
    spread: float = 0.0
    heat_index: float = 0.0


def decide(t_c: float, rh: float, prev: Decision | None, cfg: "Thresholds") -> Decision:
    """
    Resolve current readings to exactly one mode.

    `prev` supplies hysteresis: once the cooler/heater is ON it stays on until
    it crosses a *lower* off-threshold, so it doesn't chatter around a setpoint.
    """
    td = dew_point(t_c, rh)
    spread = t_c - td
    hi = heat_index(t_c, rh)

    was_cooling = bool(prev and prev.cooler)
    was_heating = bool(prev and prev.heater)

    # hysteresis bands
    cool_on = cfg.cooler_off_hi if was_cooling else cfg.cooler_on_hi
    heat_edge = cfg.heater_off_t if was_heating else cfg.heater_on_t

    def out(mode, cooler, fan, heater, reason):
        return Decision(mode, cooler, fan, heater, reason, td, spread, hi)

    # --- cold side --------------------------------------------------------
    if t_c < heat_edge:
        return out("COLD", False, 0, True,
                   f"T {t_c:.1f}° below {cfg.heater_on_t}° — heater on via IR until {cfg.heater_off_t}°.")

    # --- warm side --------------------------------------------------------
    if hi >= cool_on:
        muggy = rh > cfg.rh_muggy or spread < cfg.spread_muggy
        if rh > cfg.rh_hardlock or muggy:
            return out("MUGGY", False, 5, False,
                       f"Air saturated (spread {spread:.1f}°, RH {rh:.0f}%). Cooler would add moisture → OFF, fan max.")
        if hi > cfg.hot_hi and spread >= cfg.spread_excellent:
            return out("HOT · DRY", True, 5, False,
                       f"Peak cooler weather — feels {hi:.1f}°, {spread:.1f}° headroom. Full send.")
        if spread >= cfg.spread_good:
            return out("WARM · DRY", True, fan_tier(hi), False,
                       f"Dry enough (spread {spread:.1f}°). Cooler on, fan at feels-like {hi:.1f}°.")
        return out("WARM · MARGINAL", True, fan_tier(hi), False,
                   f"Spread {spread:.1f}° borderline — cooler on, leaning on the fan.")

    # --- comfort / cool ---------------------------------------------------
    if hi >= cfg.comfort_lo:
        return out("COMFORT", False, 1, False,
                   f"Feels {hi:.1f}° — inside the {cfg.comfort_lo:.0f}–{cfg.cooler_on_hi:.0f}° box. Circulating at speed 1.")
    return out("COOL", False, 0, False,
               f"Pleasant at {t_c:.1f}°. All actuators idle.")


@dataclass
class Thresholds:
    """All the numbers, in one place — mirror these in config.yaml."""
    cooler_on_hi: float = 27.0       # feels-like to switch cooler ON
    cooler_off_hi: float = 25.0      # ... and OFF (hysteresis gap)
    hot_hi: float = 30.0             # HOT·DRY boundary
    comfort_lo: float = 24.0
    heater_on_t: float = 20.0        # actual temp to switch heater ON
    heater_off_t: float = 23.0       # ... and OFF
    spread_excellent: float = 9.0    # T-Td: cooler excellent
    spread_good: float = 7.0         # cooler still worthwhile
    spread_muggy: float = 5.0        # below this cooler is counter-productive
    rh_muggy: float = 70.0
    rh_hardlock: float = 75.0        # never run cooler above this RH

    @classmethod
    def from_dict(cls, d: dict) -> "Thresholds":
        return cls(**{k: v for k, v in (d or {}).items() if k in cls.__annotations__})
