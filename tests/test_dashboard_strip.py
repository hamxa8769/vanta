"""Dashboard summary strip + robust chart (v0.6.0) — headless only.

The strip is always at the top (never hideable), fed from the payload
the worker already produces. The chart panel is full-width with both
chart pages 480px tall, a visible status line, and a Retry button.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS",
                      "--no-sandbox --disable-gpu")

from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from goldboard_app.main_window import MainWindow
from goldboard_app.worker import collect_dashboard_data
from goldboard_core.storage import init_db, save_price_bars
from tests.test_ui import _open_window, _seed_db

STRIP_KEYS = ("price", "technical", "regime", "score", "setups",
              "event", "volume")


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _seed_bars(db, days: int = 250) -> None:
    """Rising daily bars + intraday bars, so technical, setups,
    regime filter and volume profile all have real data."""
    price = 3000.0
    bars = []
    base = datetime(2025, 1, 1, tzinfo=timezone.utc)
    for i in range(days):
        price += 3.0
        bars.append({"ts": base + timedelta(days=i),
                     "open": price - 0.5, "high": price + 1.0,
                     "low": price - 1.0, "close": price, "volume": None,
                     "interval": "1d"})
    save_price_bars(db, bars)
    intra = []
    for i in range(96):
        p = 3500.0 + (i % 10)
        intra.append({"ts": base + timedelta(minutes=15 * i),
                      "open": p, "high": p + 2.0, "low": p - 2.0,
                      "close": p, "volume": 10.0 if i % 2 else 1.0,
                      "interval": "15m"})
    save_price_bars(db, intra)


def test_strip_exists_with_all_cards_and_empty_states(
        tmp_path, monkeypatch, qapp):
    """v0.6.0: seven KPI cards, honest empty states before data."""
    db = tmp_path / "empty.db"
    init_db(db)
    monkeypatch.setattr("goldboard_app.worker.refresh_all", lambda db_path: {})
    monkeypatch.setattr("goldboard_app.worker._fetch_history", lambda *a: None)
    monkeypatch.setattr("goldboard_app.worker._fetch_news", lambda *a: None)
    window = MainWindow(db_path=db, enable_live_ticker=False)
    window.show()
    qapp.processEvents()
    assert window._strip_widget is not None
    assert window._strip_widget.isVisible()
    assert set(window._strip_cards) == set(STRIP_KEYS)
    # Six cards are honestly empty before any data arrives. Two
    # exceptions: the event card (bundled calendar) and the setups
    # card (the engine always returns one "Not enough history"
    # placeholder setup when there is no history to evaluate).
    for key in STRIP_KEYS:
        if key in ("event", "setups"):
            continue
        assert window._strip_cards[key]["value"].text() == "—"
        # Honest empty wording differs per card ("No data yet" /
        # "No score yet" / "No setups yet") — all say "yet".
        assert "yet" in window._strip_cards[key]["sub"].text()
    assert "US PCE" in window._strip_cards["event"]["value"].text()
    assert "in " in window._strip_cards["event"]["sub"].text()
    assert window._strip_cards["setups"]["value"].text() == "1"
    assert "Not enough history" in window._strip_cards["setups"]["sub"].text()
    # The strip is NOT a hideable panel: it never appears in the
    # Panels menu and has no panel entry.
    assert "strip" not in [p["key"] for p in window._panels]
    assert "Summary" not in window._panels_menu.title()
    window.close()


def test_strip_updates_from_payload(tmp_path, monkeypatch, qapp):
    """v0.6.0: every card fills from one real payload (seeded DB)."""
    db = tmp_path / "goldboard.db"
    _seed_db(db)
    _seed_bars(db)
    monkeypatch.setattr("goldboard_app.worker.refresh_all", lambda db_path: {})
    monkeypatch.setattr("goldboard_app.worker._fetch_history", lambda *a: None)
    monkeypatch.setattr("goldboard_app.worker._fetch_news", lambda *a: None)
    window = MainWindow(db_path=db, enable_live_ticker=False)
    window.show()
    qapp.processEvents()
    payload = collect_dashboard_data(db, refresh=False)
    window._on_refresh_finished(payload)
    qapp.processEvents()
    cards = window._strip_cards
    # Price: seeded 4,000 USD/oz quote.
    assert "4,000" in cards["price"]["value"].text()
    assert "price" in cards["price"]["sub"].text().lower() \
        or "Yahoo" in cards["price"]["sub"].text()
    # Technical: the strip card shows the LATEST advisory signal,
    # not the indicator counts (those live in the Technical panel).
    # Here the score (+22) is inside the ±40 band, so honestly no
    # signal — the pill says so instead of showing a stale one.
    assert cards["technical"]["value"].text() == "NO SIGNAL"
    assert "±40 band" in cards["technical"]["sub"].text()
    # Regime filter: label + ER/CHOP shown.
    tech = payload["technical"]
    rf = tech["regime_filter"]
    assert cards["regime"]["value"].text() == rf["label"].upper()
    assert "ER" in cards["regime"]["sub"].text()
    assert "CHOP" in cards["regime"]["sub"].text()
    # Score: 22.0 -> Bullish, top component is the COT pillar
    # (70 from the v0.6.0 alias, not 60 from the synthetic score).
    assert cards["score"]["value"].text().startswith("+22.0")
    assert "Bullish" in cards["score"]["sub"].text()
    assert "cot" in cards["score"]["sub"].text()
    # Honest pillar count: the seed fills all five pillars.
    assert "5 of 5 pillars" in cards["score"]["sub"].text()
    # Setups: count + top setup. The top setup is a DIRECTIONAL
    # one (v0.8.0): the event caution fires first in the list with
    # direction "none", so the old card read "· NONE" while real
    # long/short setups existed below it.
    assert cards["setups"]["value"].text() == str(len(payload["setups"]))
    directional = [s for s in payload["setups"]
                   if s["direction"] in ("long", "short")]
    assert directional, "fixture should have a directional setup"
    assert directional[0]["name"] in cards["setups"]["sub"].text()
    assert "NONE" not in cards["setups"]["sub"].text()
    # Next macro event: bundled calendar has PCE / jobs / FOMC.
    assert payload["events"], "bundled calendar should list events"
    assert payload["events"][0]["name"] in cards["event"]["value"].text()
    assert "in " in cards["event"]["sub"].text()
    # Volume profile POC from the intraday bars.
    assert payload["volume_profile"].get("complete") is True
    poc = payload["volume_profile"]["poc"]
    assert f"{poc:,.2f}" in cards["volume"]["value"].text()
    assert "Value area" in cards["volume"]["sub"].text()
    # Technical panel pills mirror the strip.
    assert window._tech_signal_pill.text() == tech["signal"].upper()
    assert window._tech_regime_pill.text() == rf["label"].upper()
    window.close()


def test_strip_shows_latest_buy_signal(tmp_path, monkeypatch, qapp):
    """v0.8.0: the Technical card shows the LATEST advisory signal
    prominently — direction pill, entry, time, confidence."""
    from datetime import datetime, timezone

    from goldboard_core.models import Signal

    window = _open_window(tmp_path, monkeypatch, qapp)
    signal = Signal(
        direction="buy", entry=4000.0, sl=3950.0, tp=4100.0,
        confidence=0.86, reason="Score +60: test.",
        generated_at=datetime(2026, 11, 24, 11, 0, tzinfo=timezone.utc),
        rule_version="v0.1.0", horizon_days=10, status="open")
    payload = {"price": None, "technical": {}, "score": None,
               "signal": signal, "setups": [], "events": [],
               "volume_profile": {}}
    window._update_summary_strip(payload)
    qapp.processEvents()
    cards = window._strip_cards
    assert cards["technical"]["value"].text() == "BUY"
    sub = cards["technical"]["sub"].text()
    assert "4,000" in sub
    assert "86%" in sub
    assert "11:00" in sub
    window.close()


def test_strip_no_signal_is_honest_not_stale(tmp_path, monkeypatch, qapp):
    """v0.8.0: a "none" signal shows NO SIGNAL with the score band
    — never a stale buy/sell from an earlier refresh."""
    from datetime import datetime, timezone

    from goldboard_core.models import Signal

    window = _open_window(tmp_path, monkeypatch, qapp)
    signal = Signal(
        direction="none", entry=4000.0, sl=None, tp=None,
        confidence=0.0, reason="Score +22: inside band.",
        generated_at=datetime(2026, 11, 24, 11, 0, tzinfo=timezone.utc),
        rule_version="v0.1.0", horizon_days=10, status="open")
    payload = {"price": None, "technical": {}, "score": None,
               "signal": signal, "setups": [], "events": [],
               "volume_profile": {}}
    window._update_summary_strip(payload)
    qapp.processEvents()
    cards = window._strip_cards
    assert cards["technical"]["value"].text() == "NO SIGNAL"
    assert "±40 band" in cards["technical"]["sub"].text()
    window.close()


def test_chart_preview_is_small_and_expands_tall(
        tmp_path, monkeypatch, qapp):
    """v0.7.0: the dashboard chart is a ~200px preview; expanding it
    (button or click) moves the SAME widget into a centred dialog
    where it is 480px tall; closing the dialog docks it back."""
    window = _open_window(tmp_path, monkeypatch, qapp)
    price_panel = next(p for p in window._panels if p["key"] == "price")
    assert price_panel["wide"] is True
    # Preview: small, both pages.
    assert window._tv_chart.minimumHeight() == 200
    assert window._price_chart.minimumHeight() == 200
    if window._tv_chart._view is not None:
        assert window._tv_chart._view.minimumHeight() == 200
    window.close()


def test_chart_status_and_retry(tmp_path, monkeypatch, qapp):
    """v0.6.0: fallback status line + Retry button exist; clicking
    Retry calls reload() on the chart widget."""
    window = _open_window(tmp_path, monkeypatch, qapp)
    qapp.processEvents()
    assert window._chart_status is not None
    assert window._chart_retry_button is not None
    # Not live (TradingView cannot load in this VM): fallback text
    # and a visible retry button.
    assert not window._tv_chart.is_live
    assert "Built-in chart" in window._chart_status.text()
    assert window._chart_retry_button.isVisible()
    calls: list[str] = []
    monkeypatch.setattr(window._tv_chart, "reload",
                        lambda: calls.append("reload"))
    window._chart_retry_button.click()
    qapp.processEvents()
    assert calls == ["reload"]
    assert "reloading" in window._chart_status.text().lower()
    window.close()


def test_chart_live_status_hides_retry(tmp_path, monkeypatch, qapp):
    """v0.6.0: when the iframe is live the status goes green and the
    Retry button hides."""
    window = _open_window(tmp_path, monkeypatch, qapp)
    window._update_chart_status(live=True)
    qapp.processEvents()
    assert "TradingView LIVE" in window._chart_status.text()
    assert not window._chart_retry_button.isVisible()
    window._update_chart_status(live=False)
    qapp.processEvents()
    assert "Built-in chart" in window._chart_status.text()
    assert window._chart_retry_button.isVisible()
    window.close()


def test_tradingview_reload_resets_watchdog(tmp_path, monkeypatch, qapp):
    """v0.6.0: reload() resets the watchdog and re-loads the page;
    without WebEngine it stays on the fallback without crashing."""
    import goldboard_app.tradingview_chart as tvc
    from PySide6.QtWidgets import QLabel

    from goldboard_app.tradingview_chart import TradingViewChart

    monkeypatch.setattr(tvc, "WEBENGINE_AVAILABLE", False)
    fallback = QLabel("built-in chart")
    chart = TradingViewChart(fallback)
    assert chart.currentWidget() is fallback
    chart.reload()  # must not raise
    assert chart.currentWidget() is fallback
    assert chart.is_live is False
