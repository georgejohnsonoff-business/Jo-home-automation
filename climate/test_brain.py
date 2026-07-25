"""
test_brain.py — verify the decision engine before any hardware exists.
    pytest climate/test_brain.py -v
"""
from climate.brain import dew_point, heat_index, fan_tier, decide, Thresholds

THR = Thresholds()


def approx(a, b, tol=0.5):
    return abs(a - b) < tol


def test_dew_point_known_value():
    # 30°C / 50% RH -> dew point ~18.4°C
    assert approx(dew_point(30, 50), 18.4)


def test_heat_index_below_threshold_is_actual():
    assert heat_index(24, 60) == 24


def test_heat_index_hot_humid_feels_worse():
    assert heat_index(34, 70) > 34


def test_fan_scales_with_heat():
    assert fan_tier(27.1) == 0        # below the 27.2 floor -> off
    assert fan_tier(27.45) == 3       # 27.4-27.5 -> exactly 3, as specified
    assert fan_tier(34) == 5          # no cap — full range always available


def test_hot_and_dry_runs_cooler_full():
    d = decide(35, 30, None, THR)
    assert d.mode == "HOT · DRY"
    assert d.cooler and d.fan == 5 and not d.heater
    assert d.cooler_priority


def test_muggy_forces_cooler_off():
    d = decide(33, 85, None, THR)      # hot but saturated
    assert d.mode == "MUGGY"
    assert not d.cooler and d.fan == 5


def test_cold_runs_heater():
    d = decide(17, 60, None, THR)
    assert d.mode == "COLD"
    assert d.heater and not d.cooler


def test_comfort_just_circulates():
    # t=27.2/rh=40 -> heat_index ~27.01, inside the 27.0-27.5 dead zone
    d = decide(27.2, 40, None, THR)
    assert d.mode == "COMFORT"
    assert not d.cooler and d.fan == 1 and not d.heater


def test_hysteresis_keeps_cooler_on_in_the_gap():
    on = decide(30, 40, None, THR)                 # cooler ON
    assert on.cooler
    # feels-like now in the 26.5-27.5 gap (but above heater_on_t=27.0, so
    # heater doesn't intercept first): without memory it'd switch off,
    # with prev=on it should stay cooling.
    still = decide(27.2, 45, on, THR)
    assert still.cooler


def test_rh_hardlock_never_cools():
    d = decide(34, 78, None, THR)
    assert not d.cooler


def test_fast_warmup_hands_off_to_cooler_early():
    # spread is good (dry) but not yet past hot_hi — without a trend this
    # would just be a normal WARM · DRY tick with no priority flag.
    baseline = decide(28, 40, None, THR)
    assert baseline.mode == "WARM · DRY" and not baseline.cooler_priority
    # a fast warming trend (1.0 °C/min) means feels-like will likely cross
    # hot_hi within the 10-min lookahead -> cooler should take priority now,
    # ahead of the fan actually needing to hit its cap.
    warming = decide(28, 40, None, THR, hi_trend=1.0)
    assert warming.mode == "WARM · DRY" and warming.cooler_priority


def test_muggy_runs_fan_full_even_though_humid():
    # explicit design choice: humidity caps the COOLER, never the fan —
    # moving air still helps even when it's muggy.
    d = decide(33, 85, None, THR)
    assert d.mode == "MUGGY" and d.fan == 5 and not d.cooler
