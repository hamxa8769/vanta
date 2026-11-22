"""Regime filter (v0.5.0) — ER, CHOP, ATR percentile, 4-state rules,
and the range-fade suppression in the setups engine.

The spec's thresholds are starting points and are NOT optimized:
these tests pin the specified behaviour, not tuned values.
"""

from goldboard_core.indicators import (atr, atr_percentile,
                                       choppiness, efficiency_ratio)
from goldboard_core.setups import generate_setups
from goldboard_core.technical import technical_summary


def _bars(prices):
    bars = []
    for i, price in enumerate(prices):
        bars.append({"open": price, "high": price + 1, "low": price - 1,
                     "close": price, "volume": 10.0,
                     "start_ms": i * 86_400_000})
    return bars


# ---------------------------------------------------------------- ER

def test_efficiency_ratio_straight_march_is_near_one():
    closes = [100.0 + i for i in range(30)]  # +1 every bar, no wander
    er = efficiency_ratio(closes, 20)
    assert er[-1] is not None
    assert abs(er[-1] - 1.0) < 0.01


def test_efficiency_ratio_round_trip_is_near_zero():
    # Flat, then up 10 and back down 10 inside the last window:
    # net change 0 over the window, path 20.
    closes = [100.0] * 10 + [100.0 + i for i in range(1, 11)] \
        + [110.0 - i for i in range(1, 11)]
    er = efficiency_ratio(closes, 20)
    assert er[-1] is not None
    assert er[-1] < 0.1


def test_efficiency_ratio_needs_a_full_window():
    assert efficiency_ratio([100.0] * 10, 20)[-1] is None


# -------------------------------------------------------------- CHOP

def test_choppiness_flat_range_is_high():
    # Price bounces inside a tight band: CHOP should read choppy.
    prices = [100.0, 102.0, 98.0, 101.0, 99.0] * 8
    chop = choppiness(_bars(prices), 14)
    assert chop[-1] is not None
    assert chop[-1] > 61.8


def test_choppiness_straight_trend_is_low():
    prices = [100.0 + i * 2 for i in range(40)]
    chop = choppiness(_bars(prices), 14)
    assert chop[-1] is not None
    assert chop[-1] < 38.2


# ------------------------------------------------------- ATR percentile

def test_atr_percentile_latest_is_the_highest():
    values = [None] * 5 + [1.0] * 100 + [5.0]
    assert atr_percentile(values, 250) == 100.0


def test_atr_percentile_latest_is_the_lowest():
    values = [None] * 5 + [5.0] * 100 + [1.0]
    assert atr_percentile(values, 250) == 0.0


def test_atr_percentile_needs_ten_values():
    assert atr_percentile([1.0] * 9) is None


# --------------------------------------------------- 4-state classification

def _summary(prices):
    return technical_summary(_bars(prices))


def test_trending_when_all_three_agree():
    # Straight march: ER ~1, CHOP low, ADX high.
    prices = [3000.0 + i * 5 for i in range(300)]
    summary = _summary(prices)
    rf = summary["regime_filter"]
    assert rf["label"] == "trending"
    assert rf["er"] > 0.5
    assert rf["chop"] < 38.2
    assert summary["adx"] > 25


def test_choppy_when_price_wanders():
    # Oscillating band for 300 bars: ER low, CHOP high.
    prices = []
    for i in range(300):
        prices.append(3500.0 + (10.0 if i % 2 == 0 else -10.0))
    summary = _summary(prices)
    rf = summary["regime_filter"]
    assert rf["label"] == "choppy"
    assert rf["er"] < 0.3
    assert rf["chop"] > 61.8


def test_too_few_bars_is_unknown():
    summary = _summary([3500.0] * 10)
    assert summary["regime_filter"]["label"] == "unknown"


# ------------------------------------------------------- setups wiring

def _complete_technical(**overrides):
    tech = {"complete": True, "signal": "neutral", "buy": 0,
            "neutral": 5, "sell": 0, "adx": 15.0, "rsi14": 50.0,
            "sma20": 3500.0, "last_close": 3505.0}
    tech.update(overrides)
    return tech


def test_range_fade_suppressed_when_regime_trending():
    bars = _bars([3500.0 + (i % 3) for i in range(60)])
    setups = generate_setups(
        _complete_technical(), score_total=None, cot_pctile=None,
        etf_gld_change=None, fomc_days_away=None, daily_bars=bars,
        regime_filter={"label": "trending"})
    assert not [s for s in setups if s.get("kind") == "range"]


def test_range_fade_allowed_when_regime_choppy():
    bars = _bars([3500.0 + (i % 3) for i in range(60)])
    setups = generate_setups(
        _complete_technical(), score_total=None, cot_pctile=None,
        etf_gld_change=None, fomc_days_away=None, daily_bars=bars,
        regime_filter={"label": "choppy"})
    faded = [s for s in setups if s.get("kind") == "range"]
    assert len(faded) == 1
    assert any("CHOPPY" in r for r in faded[0]["reasons"])


def test_range_fade_allowed_without_regime_filter():
    # Backwards compatible: no regime_filter passed at all.
    bars = _bars([3500.0 + (i % 3) for i in range(60)])
    setups = generate_setups(
        _complete_technical(), score_total=None, cot_pctile=None,
        etf_gld_change=None, fomc_days_away=None, daily_bars=bars)
    assert len([s for s in setups if s.get("kind") == "range"]) == 1
