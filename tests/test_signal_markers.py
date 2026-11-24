"""Vanta signal markers on the built-in chart (v0.8.0) — headless only.

The TradingView embed is a cross-origin iframe with NO marker /
drawing API (verified against the embed script: its only messages
are iframe-resize and open-in-popup, and its settings list has no
marker parameter). So Vanta's signals are plotted on the built-in
chart, where every pixel is ours: a buy/sell marker at the
signal's bar + entry price, dashed entry / SL (red) / TP (green)
lines, and the outcome for closed signals (TP hit / SL hit /
expired, with the R multiple) from the existing backtest engine.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS",
                      "--no-sandbox --disable-gpu")

from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from goldboard_app.main_window import MainWindow, SignalChart
from goldboard_app.worker import collect_dashboard_data
from goldboard_core.backtest import backtest
from goldboard_core.models import Signal
from goldboard_core.storage import init_db, save_signal
from tests.test_dashboard_strip import _seed_bars
from tests.test_ui import _open_window, _seed_db


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _bars(days: int = 30, start: float = 3000.0, step: float = 3.0):
    """Rising daily bars from 2025-01-01 (deterministic)."""
    base = datetime(2025, 1, 1, tzinfo=timezone.utc)
    bars = []
    price = start
    for i in range(days):
        price += step
        bars.append({"ts": base + timedelta(days=i),
                     "open": price - 0.5, "high": price + 1.0,
                     "low": price - 1.0, "close": price, "volume": None,
                     "interval": "1d"})
    return bars


def _signal(direction: str, entry: float, sl: float, tp: float,
            when: datetime) -> Signal:
    return Signal(direction=direction, entry=entry, sl=sl, tp=tp,
                  confidence=0.86, reason="test signal",
                  generated_at=when, rule_version="v0.1.0",
                  horizon_days=10, status="open")


def _chart_with_bars(qapp, bars):
    chart = SignalChart()
    chart.resize(800, 400)
    chart.set_bars(bars)
    chart.show()
    qapp.processEvents()
    return chart


def test_bar_index_for_time_maps_to_bar_in_effect(qapp):
    """v0.8.0: a signal maps to the last bar with ts <= its time
    (the bar in effect when it fired) — never a later bar."""
    bars = _bars()
    chart = _chart_with_bars(qapp, bars)
    assert chart.bar_index_for_time(bars[0]["ts"]) == 0
    assert chart.bar_index_for_time(
        bars[5]["ts"] + timedelta(hours=12)) == 5
    assert chart.bar_index_for_time(
        bars[-1]["ts"] + timedelta(days=30)) == len(bars) - 1
    assert chart.bar_index_for_time(
        bars[0]["ts"] - timedelta(days=1)) is None
    assert chart.bar_index_for_time(None) is None
    chart.close()


def test_markers_sit_at_correct_bar_and_price(qapp):
    """v0.8.0: a marker's pixel x/y equal the chart's own mapping
    for its bar index and entry price — exact, not approximate."""
    bars = _bars()
    chart = _chart_with_bars(qapp, bars)
    buy_at = bars[10]["ts"] + timedelta(hours=12)
    sell_at = bars[20]["ts"] + timedelta(hours=12)
    buy = _signal("buy", bars[10]["close"], bars[10]["close"] - 30,
                  bars[10]["close"] + 15, buy_at)
    sell = _signal("sell", bars[20]["close"], bars[20]["close"] + 30,
                   bars[20]["close"] - 15, sell_at)
    chart.set_signals([(buy, None), (sell, None)])
    markers = chart.markers()
    assert len(markers) == 2
    by_dir = {m["direction"]: m for m in markers}
    geo = chart._geometry()
    for direction, idx, entry in (("buy", 10, buy.entry),
                                  ("sell", 20, sell.entry)):
        marker = by_dir[direction]
        assert marker["bar_index"] == idx
        assert marker["entry"] == entry
        assert marker["outcome"] == "open"
        exp_x, exp_y = chart.xy(idx, entry, geo)
        assert marker["x"] == exp_x
        assert marker["y"] == exp_y
        # SL/TP lines sit at the right prices through the same map.
        exp_sl_y = chart.xy(idx, marker["sl"], geo)[1]
        exp_tp_y = chart.xy(idx, marker["tp"], geo)[1]
        assert exp_sl_y != exp_y and exp_tp_y != exp_y
        if direction == "buy":
            assert exp_tp_y < exp_y < exp_sl_y  # TP above, SL below
        else:
            assert exp_sl_y < exp_y < exp_tp_y  # SL above, TP below
    chart.close()


def test_none_direction_signals_are_never_plotted(qapp):
    """v0.8.0: a "none" signal has no levels and no marker."""
    bars = _bars()
    chart = _chart_with_bars(qapp, bars)
    none = Signal(direction="none", entry=bars[5]["close"], sl=None,
                  tp=None, confidence=0.0, reason="inside band",
                  generated_at=bars[5]["ts"] + timedelta(hours=1),
                  rule_version="v0.1.0", horizon_days=10, status="open")
    chart.set_signals([(none, None)])
    assert chart.markers() == []
    assert chart.in_range_count() == 0
    chart.close()


def test_outcomes_tp_sl_open_from_backtest(qapp):
    """v0.8.0: closed-signal outcomes come from the existing
    backtest engine on synthetic bars — TP hit +2R, SL hit −1R,
    and a signal too recent to evaluate stays "open"."""
    bars = _bars(days=30)
    prices = [(b["ts"], b["close"], b["high"], b["low"]) for b in bars]
    # Buy, TP +30 (2R: risk 15) hit on day 10 (bars rise +3/day).
    buy = _signal("buy", bars[5]["close"], bars[5]["close"] - 15,
                  bars[5]["close"] + 30, bars[5]["ts"])
    # Sell, SL +2 hit the next day (bars rise +3/day, high +1).
    sell = _signal("sell", bars[10]["close"], bars[10]["close"] + 2,
                   bars[10]["close"] - 30, bars[10]["ts"])
    # Too recent: generated after the last bar — unevaluable.
    recent = _signal("buy", bars[-1]["close"], bars[-1]["close"] - 15,
                     bars[-1]["close"] + 30,
                     bars[-1]["ts"] + timedelta(days=1))
    result = backtest([buy, sell, recent], prices)
    by_key = {(t.direction, t.entry, t.generated_at): t
              for t in result.trades}
    chart = _chart_with_bars(qapp, bars)
    chart.set_signals([
        (buy, by_key.get((buy.direction, buy.entry, buy.generated_at))),
        (sell, by_key.get((sell.direction, sell.entry,
                           sell.generated_at))),
        (recent, by_key.get((recent.direction, recent.entry,
                             recent.generated_at))),
    ])
    markers = {m["direction"]: m for m in chart.markers()}
    assert markers["buy"]["outcome"] == "tp"
    assert markers["buy"]["r_multiple"] == pytest.approx(2.0)
    assert markers["sell"]["outcome"] == "sl"
    assert markers["sell"]["r_multiple"] == pytest.approx(-1.0)
    # The recent signal has no TradeResult: still open, never faked.
    assert len(markers) == 2  # recent maps past the last bar
    chart.close()


def test_view_switcher_flips_between_tradingview_and_signals(
        tmp_path, monkeypatch, qapp):
    """v0.8.0: the price panel header switches the stacked view
    between the live TradingView chart and the signals chart."""
    window = _open_window(tmp_path, monkeypatch, qapp)
    assert window._chart_view == "tradingview"
    assert window._view_stack.currentIndex() == 0
    assert window._view_buttons["tradingview"].isChecked()
    window._view_buttons["signals"].click()
    qapp.processEvents()
    assert window._chart_view == "signals"
    assert window._view_stack.currentIndex() == 1
    assert window._view_buttons["signals"].isChecked()
    assert "Vanta signals" in window._chart_status.text()
    assert not window._chart_retry_button.isVisible()
    window._view_buttons["tradingview"].click()
    qapp.processEvents()
    assert window._chart_view == "tradingview"
    assert window._view_stack.currentIndex() == 0
    window.close()


def test_signals_chart_uses_same_range_as_price_chart(
        tmp_path, monkeypatch, qapp):
    """v0.8.0: the signals chart shows the same bars as the
    built-in chart for the selected range — and a stored buy
    signal plots on it with its backtest outcome."""
    db = tmp_path / "goldboard.db"
    _seed_db(db)
    _seed_bars(db)
    monkeypatch.setattr("goldboard_app.worker.refresh_all",
                        lambda db_path: {})
    monkeypatch.setattr("goldboard_app.worker._fetch_history",
                        lambda *a: None)
    monkeypatch.setattr("goldboard_app.worker._fetch_news", lambda *a: None)
    # A stored buy signal on day 150 of the rising bars: TP +30
    # (2R: risk 15) hits on day 10, so the backtest says TP +2.0R.
    from goldboard_core.storage import get_price_bars
    daily = get_price_bars(db, "1d", limit=600)
    bar = daily[150]
    save_signal(db, _signal("buy", bar["close"], bar["close"] - 15,
                            bar["close"] + 30,
                            bar["ts"] + timedelta(hours=12)))
    window = MainWindow(db_path=db, enable_live_ticker=False)
    window.show()
    qapp.processEvents()
    payload = collect_dashboard_data(db, refresh=False)
    window._on_refresh_finished(payload)
    qapp.processEvents()
    window._set_chart_range("2Y")
    qapp.processEvents()
    assert len(window._signals_chart._bars) == \
        len(window._price_chart._bars) == 250
    markers = window._signals_chart.markers()
    assert len(markers) == 1
    assert markers[0]["direction"] == "buy"
    assert markers[0]["bar_index"] == 150
    assert markers[0]["outcome"] == "tp"
    assert markers[0]["r_multiple"] == pytest.approx(2.0)
    window.close()


def test_signals_chart_paints_without_crashing(qapp):
    """v0.8.0: the markers chart renders offscreen (markers +
    dashed lines + labels) without error."""
    bars = _bars()
    chart = _chart_with_bars(qapp, bars)
    sig = _signal("buy", bars[10]["close"], bars[10]["close"] - 30,
                  bars[10]["close"] + 15,
                  bars[10]["ts"] + timedelta(hours=12))
    chart.set_signals([(sig, None)])
    pixmap = chart.grab()
    assert not pixmap.isNull()
    chart.close()
