"""Tests for goldboard_core.indicators (v0.3.0) — pure math on
synthetic series with hand-computable values."""

from goldboard_core.indicators import adx, atr, bollinger, ema, macd, rsi, sma


def test_sma_partial_and_full():
    assert sma([1, 2, 3, 4, 5], 3) == [None, None, 2.0, 3.0, 4.0]


def test_ema_constant_series():
    assert ema([100.0] * 10, 3)[-1] == 100.0


def test_rsi_rising_is_100_falling_is_0():
    rising = [float(100 + i) for i in range(20)]
    assert rsi(rising, 14)[-1] == 100.0
    falling = [float(200 - i) for i in range(20)]
    assert rsi(falling, 14)[-1] == 0.0


def test_rsi_flat_is_neutral_50():
    flat = [100.0] * 20
    value = rsi(flat, 14)[-1]
    assert value is not None and abs(value - 50.0) < 1.0


def test_macd_constant_is_zero():
    line, sig, hist = macd([50.0] * 60)
    assert line[-1] == 0.0
    assert sig[-1] == 0.0
    assert hist[-1] == 0.0


def test_macd_rising_is_positive():
    closes = [100.0 + i * 2 for i in range(60)]
    line, sig, _ = macd(closes)
    assert line[-1] is not None and line[-1] > 0
    assert sig[-1] is not None


def test_atr_flat_range_is_range():
    bars = [{"open": 100.0, "high": 105.0, "low": 95.0, "close": 100.0}
            for _ in range(20)]
    values = atr(bars, 14)
    assert values[-1] == 10.0


def test_bollinger_constant_has_zero_width():
    closes = [100.0] * 30
    mid, upper, lower = bollinger(closes, 20, 2.0)
    assert mid[-1] == 100.0
    assert upper[-1] == 100.0
    assert lower[-1] == 100.0


def test_adx_strong_trend_high_flat_low():
    trend = []
    price = 100.0
    for _ in range(60):
        price += 2.0
        trend.append({"open": price - 1, "high": price + 1,
                      "low": price - 2, "close": price})
    adx_v, _, _ = adx(trend, 14)
    assert adx_v[-1] is not None and adx_v[-1] > 25
    flat = [{"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0}
            for _ in range(60)]
    adx_flat, _, _ = adx(flat, 14)
    assert adx_flat[-1] is not None and adx_flat[-1] < 20
