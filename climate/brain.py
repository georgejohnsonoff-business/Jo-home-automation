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


def fan_tier(t_c: float, floor: float = 27.2, edge: float = 27.5, cap: int = 5) -> int:
    """
    Fan speed 0-5, continuously graduated from `floor` (0, off) through
    `edge` (~3) and beyond (4-5) — one smooth ramp across the whole
    off-to-full-blast range, not a flat idle speed followed by a jump.
    Step size is the floor->edge gap divided into 3, then continued at the
    same size above edge. With the defaults (27.2->27.5), that's 0.1°/tier:
    <27.2 off, 27.2-27.3->1, 27.3-27.4->2, 27.4-27.5->3, 27.5-27.6->4, >27.6->5.
    Relative to floor/edge (not fixed absolute numbers) so retargeting via
    the thermostat override keeps the same shape.
    """
    gap = max(edge - floor, 0.1)
    step = round(gap / 3, 6)
    delta = round(t_c - floor, 6)   # kill float dust (e.g. 27.4-27.2 != exactly 0.2) before comparing
    if delta < 0: return 0
    if delta < step: return min(1, cap)
    if delta < 2 * step: return min(2, cap)
    if delta <= 3 * step: return min(3, cap)          # inclusive at `edge` itself — 27.5 must land on 3, not 4
    if delta <= 4 * step: return min(4, cap)
    return min(5, cap)


def heater_dial(t_c: float, thr: "Thresholds") -> int:
    """
    Heater thermostat-dial VALUE (15-32) to push, driven purely by OUR sensor
    — never the heater's own internal thermostat (it sits next to its own
    heat output and would under-read the room). Full intensity immediately
    once below the trigger — no easing in, matches the "instant, full
    response outside the dead zone" design for the whole system.
    """
    return 32 if t_c < thr.heater_on_t else 20


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
    cooler_priority: bool = False   # True when conditions are hot enough that
                                     # the cooler is doing the real work


def decide(t_c: float, rh: float, prev: Decision | None, cfg: "Thresholds",
           hi_trend: float = 0.0) -> Decision:
    """
    Resolve current readings to exactly one mode.

    `prev` supplies hysteresis: once the cooler/heater is ON it stays on until
    it crosses a *lower* off-threshold, so it doesn't chatter around a setpoint.

    `hi_trend` is the recent warming rate (°C/min of feels-like, positive =
    heating up) over roughly the last 10 minutes. It's used ONLY to decide how
    early to hand control over to the cooler — the reported temp/spread/heat
    index are always the real, unmodified measurements, never adjusted by the
    prediction. A 10-minute lookahead is applied: if the current trend holds,
    `hi_effective` is where feels-like will likely be in 10 minutes. This lets
    the system front-run a fast afternoon heat-up instead of waiting until the
    fan is already maxed out at the cap to start relying on the cooler.
    """
    td = dew_point(t_c, rh)
    spread = t_c - td
    hi = heat_index(t_c, rh)
    hi_effective = hi + max(0.0, hi_trend) * 10.0

    was_cooling = bool(prev and prev.cooler)
    was_heating = bool(prev and prev.heater)

    # hysteresis bands
    cool_on = cfg.cooler_off_hi if was_cooling else cfg.cooler_on_hi
    heat_edge = cfg.heater_off_t if was_heating else cfg.heater_on_t

    def out(mode, cooler, fan, heater, reason, cooler_priority=False):
        return Decision(mode, cooler, fan, heater, reason, td, spread, hi, cooler_priority)

    # --- cold side --------------------------------------------------------
    if t_c < heat_edge:
        return out("COLD", False, 0, True,
                   f"T {t_c:.1f}° below {cfg.heater_on_t}° — heater on via IR until {cfg.heater_off_t}°.")

    # --- warm side ---------------------------------------------------------
    # Gate and fan graduation both run on RAW temperature, not feels-like —
    # matches the cooler's own Google Home script and the numbers as given
    # (27.5/26.5 are raw-temp values). heat_index/spread still classify HOW
    # to respond (dry vs muggy vs peak) once we're in this branch.
    if t_c >= cool_on:
        muggy = rh > cfg.rh_muggy or spread < cfg.spread_muggy
        if rh > cfg.rh_hardlock or muggy:
            # Cooler is physically counter-productive in saturated air (it'd
            # just add moisture) — fan runs full regardless, moving air is
            # still worth doing even when it's humid.
            return out("MUGGY", False, 5, False,
                       f"Air saturated (spread {spread:.1f}°, RH {rh:.0f}%). Cooler would add moisture → OFF. "
                       f"Fan runs full — moving air still helps even when humid.")
        if hi > cfg.hot_hi and spread >= cfg.spread_excellent:
            return out("HOT · DRY", True, fan_tier(t_c, cfg.comfort_lo, cfg.cooler_on_hi, cfg.fan_cap), False,
                       f"Peak cooler weather — {t_c:.1f}° ({spread:.1f}° headroom).",
                       cooler_priority=True)
        if spread >= cfg.spread_good:
            priority = hi_effective > cfg.hot_hi   # front-run a fast rise
            reason = f"Above the {cfg.cooler_on_hi:.1f}° edge (spread {spread:.1f}°) — fan continues its gradual ramp."
            if priority and hi_trend > 0.05:
                reason += f" Warming fast ({hi_trend:.2f}°/min)."
            return out("WARM · DRY", True, fan_tier(t_c, cfg.comfort_lo, cfg.cooler_on_hi, cfg.fan_cap), False, reason, cooler_priority=priority)
        return out("WARM · MARGINAL", True, fan_tier(t_c, cfg.comfort_lo, cfg.cooler_on_hi, cfg.fan_cap), False,
                   f"Spread {spread:.1f}° borderline — fan continues its gradual ramp regardless.")

    # --- comfort / cool ---------------------------------------------------
    # Below cool_on but at/above the fan floor — same continuous ramp as the
    # warm branch, just below the cooler's own gate.
    fan = fan_tier(t_c, cfg.comfort_lo, cfg.cooler_on_hi, cfg.fan_cap)
    if fan > 0:
        return out("COMFORT", False, fan, False,
                   f"{t_c:.1f}° — below the {cfg.cooler_on_hi:.1f}° edge, fan ramping gently (speed {fan}).")
    # Never fully idle from temperature alone — speed 2 is the baseline
    # circulation floor. True 0 only happens via the cooldown window
    # (explicit rest period) or manual override, not from "it's cool enough".
    return out("COOL", False, 2, False,
               f"Below {cfg.comfort_lo:.1f}° — baseline circulation at speed 2.")


@dataclass
class Thresholds:
    """All the numbers, in one place — mirror these in config.yaml."""
    cooler_on_hi: float = 27.5       # raw-temp "raw heat" trigger — matches the live Google Home script exactly
    cooler_off_hi: float = 26.5      # ... and OFF (Google Home's own hysteresis gap)
    muggy_early_temp: float = 26.8   # "feels hotter than it is" early trigger — see muggy_early_rh
    muggy_early_rh: float = 65.0     # ... humidity threshold for that same early trigger
    hot_hi: float = 29.0             # HOT·DRY boundary
    comfort_lo: float = 27.2         # fan floor — off below this, gradual ramp from here through cooler_on_hi and beyond
    heater_on_t: float = 26.5        # bang-bang, no gap — instant full heat below this (independent of the fan floor)
    heater_off_t: float = 26.5       # ... same value: no hysteresis, matches "don't care about switching"
    spread_excellent: float = 9.0    # T-Td: cooler excellent
    spread_good: float = 7.0         # cooler still worthwhile
    spread_muggy: float = 5.0        # below this cooler is counter-productive
    rh_muggy: float = 70.0
    rh_hardlock: float = 75.0        # never run cooler above this RH
    fan_cap: int = 5                 # no cap — full 1-5 range always available

    @classmethod
    def from_dict(cls, d: dict) -> "Thresholds":
        return cls(**{k: v for k, v in (d or {}).items() if k in cls.__annotations__})
