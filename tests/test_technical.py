"""Tests for goldboard_core.technical + setups (v0.3.0) —
the free local technical summary and the setups rule engine."""

from goldboard_core.setups import generate_setups
from goldboard_core.technical import technical_summary


def _bars(n: int, start: float = 100.0, step: float = 0.0) -> list[dict]:
    """Synthetic daily bars: close drifts by ``step`` per day."""
    out = []
    price = start
    for _ in range(n):
        price += step
        out.append({"ts": None, "open": price - 0.5, "high": price + 1.0,
                    "low": price - 1.0, "close": price, "volume": None,
                    "interval": "1d"})
    return out


def test_summary_needs_bars():
    summary = technical_summary([])
    assert summary["complete"] is False
    assert summary["signal"] == "neutral"


def test_summary_trending_up_is_buy():
    bars = _bars(250, start=1000.0, step=3.0)
    summary = technical_summary(bars)
    assert summary["complete"] is True
    assert summary["signal"] in ("buy", "strong buy")
    assert summary["rsi14"] is not None and summary["rsi14"] > 50
    assert summary["adx"] is not None and summary["adx"] > 25
    assert "trending" in summary["regime"]
    assert summary["sma50"] is not None
    assert summary["sma200"] is not None
    assert summary["sma50"] > summary["sma200"]


def test_summary_flat_is_ranging_neutral():
    bars = _bars(250, start=1000.0, step=0.0)
    # Add a little noise so the summary is not degenerate.
    for i, bar in enumerate(bars):
        bar["close"] = 1000.0 + (1.0 if i % 2 else -1.0)
        bar["high"] = bar["close"] + 1.0
        bar["low"] = bar["close"] - 1.0
    summary = technical_summary(bars)
    assert summary["complete"] is True
    assert summary["adx"] is not None and summary["adx"] < 20
    assert "ranging" in summary["regime"]


def test_summary_counts_add_up():
    bars = _bars(250, start=1000.0, step=3.0)
    summary = technical_summary(bars)
    assert (summary["buy"] + summary["neutral"] + summary["sell"]
            == len(summary["signals"]))


def test_setups_waiting_without_history():
    setups = generate_setups(technical_summary([]), None, None, None, None)
    assert len(setups) == 1
    assert setups[0]["kind"] == "waiting"


def test_setups_trend_continuation_when_score_agrees():
    bars = _bars(250, start=1000.0, step=3.0)
    summary = technical_summary(bars)
    setups = generate_setups(summary, 55.0, 88.0, 2.5, None, bars)
    kinds = [s["kind"] for s in setups]
    assert "trend" in kinds
    trend = [s for s in setups if s["kind"] == "trend"][0]
    assert trend["direction"] == "long"
    assert trend["confidence"] == "high"


def test_setups_conflict_when_score_disagrees():
    bars = _bars(250, start=1000.0, step=3.0)
    summary = technical_summary(bars)
    setups = generate_setups(summary, -60.0, 10.0, -3.0, None, bars)
    assert len(setups) == 1
    assert setups[0]["kind"] == "conflict"
    assert setups[0]["direction"] == "none"


def test_setups_fomc_caution_fires():
    bars = _bars(250, start=1000.0, step=3.0)
    summary = technical_summary(bars)
    setups = generate_setups(summary, 55.0, 88.0, 2.5, 1, bars)
    kinds = [s["kind"] for s in setups]
    assert "caution" in kinds


def test_setups_range_fade_when_ranging():
    bars = _bars(250, start=1000.0, step=0.0)
    for i, bar in enumerate(bars):
        bar["close"] = 1000.0 + (0.5 if i % 2 else -0.5)
        bar["high"] = bar["close"] + 1.0
        bar["low"] = bar["close"] - 1.0
    summary = technical_summary(bars)
    assert "ranging" in summary["regime"]
    setups = generate_setups(summary, 0.0, 50.0, 0.0, None, bars)
    kinds = [s["kind"] for s in setups]
    assert "range" in kinds


def test_setups_oversold_bounce():
    # Sharp fall then flat: RSI should drop below 30.
    bars = _bars(60, start=2000.0, step=0.0)
    for i, bar in enumerate(bars):
        bar["close"] = 2000.0 - i * 15.0
        bar["high"] = bar["close"] + 2.0
        bar["low"] = bar["close"] - 2.0
    summary = technical_summary(bars)
    assert summary["rsi14"] is not None and summary["rsi14"] < 30
    setups = generate_setups(summary, 0.0, 50.0, 0.0, None, bars)
    kinds = [s["kind"] for s in setups]
    assert "bounce" in kinds
