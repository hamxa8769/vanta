"""Tests for price history fetch/parse, storage round-trip, and the
price-only replay rule (v0.2.0). No network: Yahoo JSON is faked."""

from datetime import datetime, timedelta, timezone

import pytest

from goldboard_core.backtest import backtest
from goldboard_core.replay import generate_replay_signals
from goldboard_core.storage import get_price_bars, init_db, save_price_bars
from goldboard_data.price import PriceConnector

NOW = datetime(2026, 11, 21, tzinfo=timezone.utc)

CHART_JSON = {
    "chart": {
        "result": [{
            "timestamp": [1700000000, 1700086400, 1700172800],
            "indicators": {"quote": [{
                "open": [100.0, 101.0, None],
                "high": [102.0, 103.0, 105.0],
                "low": [99.0, 100.0, 101.0],
                "close": [101.0, 102.0, None],
                "volume": [1000, 1100, 1200],
            }]},
        }],
        "error": None,
    }
}


def test_parse_history_skips_null_closes():
    bars = PriceConnector().parse_history(CHART_JSON, "1d")
    assert len(bars) == 2  # third bar has a null close -> skipped
    assert bars[0]["close"] == 101.0
    assert bars[0]["open"] == 100.0
    assert bars[0]["high"] == 102.0
    assert bars[0]["low"] == 99.0
    assert bars[0]["volume"] == 1000.0
    assert bars[0]["interval"] == "1d"
    assert bars[0]["ts"] == datetime.fromtimestamp(1700000000, tz=timezone.utc)


def test_parse_history_rejects_garbage():
    with pytest.raises(Exception):
        PriceConnector().parse_history({"chart": {}}, "1d")


def _bars(closes):
    """Daily bars: flat-ish OHLC around each close (high/low +/-1)."""
    out = []
    for i, close in enumerate(closes):
        out.append({
            "ts": NOW - timedelta(days=len(closes) - i),
            "open": close - 0.5, "high": close + 1.0,
            "low": close - 1.0, "close": float(close),
            "volume": 1000.0, "interval": "1d",
        })
    return out


def test_storage_price_bars_round_trip(tmp_path):
    db = str(tmp_path / "test.db")
    init_db(db)
    bars = _bars([100.0, 101.0, 102.0])
    assert save_price_bars(db, bars) == 3
    # Upsert: same bars again must not duplicate (PK ts+interval).
    assert save_price_bars(db, bars) == 3
    stored = get_price_bars(db, "1d")
    assert len(stored) == 3
    assert [b["close"] for b in stored] == [100.0, 101.0, 102.0]
    assert get_price_bars(db, "15m") == []


def test_replay_needs_enough_bars():
    assert generate_replay_signals(_bars([100.0] * 20)) == []


def test_replay_buy_on_sma_cross():
    # 20 flat bars at 100, then a jump to 110: close crosses above SMA20.
    bars = _bars([100.0] * 20 + [110.0, 111.0])
    signals = generate_replay_signals(bars)
    assert len(signals) >= 1
    sig = signals[0]
    assert sig["direction"] == "buy"
    assert sig["entry"] == 110.0
    assert sig["sl"] < 110.0 < sig["tp"]
    # Risk levels: TP is 2R (3.0xATR vs 1.5xATR).
    assert sig["tp"] - 110.0 == pytest.approx(2 * (110.0 - sig["sl"]))


def test_replay_sell_on_sma_cross_down():
    # 19 flat bars at 100, one bar at 101 (above SMA20), then a drop
    # to 90: close crosses below SMA20 -> sell.
    bars = _bars([100.0] * 19 + [101.0, 90.0, 89.0])
    signals = generate_replay_signals(bars)
    assert signals and signals[0]["direction"] == "sell"
    assert signals[0]["entry"] == 90.0


def test_replay_backtest_runs_on_synthetic_history():
    # Flat 100 for 20 bars, jump to 110, then drift to 120: the buy
    # signal should hit its 3xATR TP inside the 10-day horizon.
    bars = _bars([100.0] * 20 + [110.0] + [112.0 + i for i in range(10)])
    signals = generate_replay_signals(bars)
    assert signals
    prices = [(b["ts"], b["close"], b["high"], b["low"]) for b in bars]
    result = backtest(signals, prices)
    assert result.n_trades >= 1
    assert result.trades[0].outcome in ("tp", "sl", "expired")
    assert result.total_r == pytest.approx(
        sum(t.r_multiple for t in result.trades))
