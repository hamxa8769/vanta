"""Desktop UI tests — headless only (QT_QPA_PLATFORM=offscreen).

No display and no network required: the refresh worker is run against
a mocked registry, and the window is built from a small seeded DB.
Skipped with a clear reason when PySide6 is not installed.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# QtWebEngine's Chromium refuses to start as root without this flag.
# The CI runners are not root, but local/VM runs are — setdefault so a
# deliberate outer value still wins. (Windows users never hit this.)
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS",
                      "--no-sandbox --disable-gpu")

from datetime import datetime, timezone

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication

from goldboard_app.main_window import APP_DISPLAY_NAME, MainWindow
from goldboard_app.worker import RefreshWorker, collect_dashboard_data
from goldboard_core.models import SourceSnapshot
from goldboard_core.storage import init_db, save_snapshot

NOW = datetime.now(timezone.utc)


def _seed_db(db_path):
    """Small honest seed: five scored pillars + a price + COT history."""
    init_db(db_path)
    rows = [
        ("cftc", "cot_score", 60.0, ""),
        ("wgc", "central_bank_etf_score", 40.0, ""),
        ("fred", "macro_score", -20.0, ""),
        ("gdelt", "news_score", 10.0, ""),
        ("comex", "microstructure_score", 0.0, ""),
        ("price", "price_xauusd", 4000.0, "USD/oz"),
        ("cftc", "cot_gold_mm_net", 102942.0, "contracts"),
        ("cftc", "cot_gold_mm_net_pct_oi", 35.0, "pct_of_oi"),
        ("etf", "etf_gld_tonnes", 1034.0, "tonnes"),
        ("etf", "etf_gld_daily_change_tonnes", 2.5, "tonnes"),
        ("etf", "etf_iau_tonnes", 500.0, "tonnes"),
        ("wgc", "wgc_cb_net_tonnes", 321.0, "tonnes"),
        ("fred", "fred_dfii10", 1.85, "%"),
        ("fred", "fred_dtwexbgs", 98.5, "index"),
        ("gdelt", "gdelt_gold_tone_latest", 2.1, ""),
    ]
    for source, series_id, value, unit in rows:
        save_snapshot(db_path, SourceSnapshot(
            source=source, series_id=series_id, value=value, unit=unit,
            as_of=NOW, fetched_at=NOW, status="ok",
            source_url=f"https://example.invalid/{source}"))
    # COT history for the chart (oldest -> newest).
    for i, value in enumerate([80000, 90000, 95000, 102942]):
        save_snapshot(db_path, SourceSnapshot(
            source="cftc", series_id="cot_gold_mm_net",
            value=float(value), unit="contracts",
            as_of=NOW, fetched_at=NOW, status="ok",
            source_url="https://example.invalid/cftc"))


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def test_collect_dashboard_data_with_mocked_registry(tmp_path, monkeypatch):
    db = tmp_path / "goldboard.db"
    _seed_db(db)
    monkeypatch.setattr(
        "goldboard_app.worker.refresh_all",
        lambda db_path: {"price": {"status": "ok", "snapshots": 1,
                                   "error": None, "fetched_at": NOW.isoformat()}},
    )
    # History + news fetchers are stubbed: they hit the network in prod.
    monkeypatch.setattr("goldboard_app.worker._fetch_history", lambda *a: None)
    monkeypatch.setattr("goldboard_app.worker._fetch_news", lambda *a: None)
    payload = collect_dashboard_data(db, refresh=True)
    assert payload["errors"] == []
    assert payload["report"]["price"]["status"] == "ok"
    # 0.25*70 + 0.20*40 + 0.25*(-20) + 0.15*10 + 0.15*0 = 22.0 — the
    # COT pillar is 70, not 60: the seed's raw cot_gold_mm_net_pct_oi
    # (35.0) maps through the v0.6.0 alias (clamp(35*2)) and outranks
    # the synthetic cot_score=60.
    assert payload["score"] is not None
    assert payload["score"].total == pytest.approx(22.0)
    assert len(payload["score"].components) == 5
    assert payload["price"] is not None
    assert payload["price"].value == pytest.approx(4000.0)
    # 19.5 < 40, so no directional signal.
    assert payload["signal"] is not None
    assert payload["signal"].direction == "none"
    assert payload["ready_for_muuk"] is False  # G1–G4 fail: no closed trades
    assert len(payload["gates"]) == 9
    assert len(payload["cot_history"]) >= 4
    assert payload["freshness"]["cftc"]["status"] == "ok"


def test_refresh_worker_emits_results_on_mocked_registry(
        tmp_path, monkeypatch, qapp):
    db = tmp_path / "goldboard.db"
    _seed_db(db)
    monkeypatch.setattr(
        "goldboard_app.worker.refresh_all",
        lambda db_path: {"price": {"status": "ok", "snapshots": 1,
                                   "error": None, "fetched_at": NOW.isoformat()}},
    )
    monkeypatch.setattr("goldboard_app.worker._fetch_history", lambda *a: None)
    monkeypatch.setattr("goldboard_app.worker._fetch_news", lambda *a: None)
    received: list[dict] = []
    errors: list[str] = []
    worker = RefreshWorker(str(db))
    worker.finished.connect(received.append)
    worker.failed.connect(errors.append)
    worker.run()  # synchronous run: same code path, no thread scheduling
    assert errors == []
    assert len(received) == 1
    assert received[0]["score"] is not None
    assert received[0]["score"].total == pytest.approx(22.0)


def test_refresh_worker_runs_on_qthread(tmp_path, monkeypatch, qapp):
    db = tmp_path / "goldboard.db"
    _seed_db(db)
    monkeypatch.setattr(
        "goldboard_app.worker.refresh_all", lambda db_path: {})
    monkeypatch.setattr("goldboard_app.worker._fetch_history", lambda *a: None)
    monkeypatch.setattr("goldboard_app.worker._fetch_news", lambda *a: None)
    received: list[dict] = []
    worker = RefreshWorker(str(db))
    worker.finished.connect(received.append)
    loop = QEventLoop()
    worker.finished.connect(loop.quit)
    QTimer.singleShot(10000, loop.quit)  # hard timeout, never hangs the suite
    worker.start()
    loop.exec()
    worker.wait(2000)
    assert len(received) == 1
    assert received[0]["score"] is not None


def test_main_window_constructs_and_shows_vanta(tmp_path, monkeypatch, qapp):
    db = tmp_path / "goldboard.db"
    _seed_db(db)
    # Keep the window's background refresh offline: stub the registry.
    monkeypatch.setattr("goldboard_app.worker.refresh_all", lambda db_path: {})
    monkeypatch.setattr("goldboard_app.worker._fetch_history", lambda *a: None)
    monkeypatch.setattr("goldboard_app.worker._fetch_news", lambda *a: None)
    window = MainWindow(db_path=db, enable_live_ticker=False)
    window.show()
    qapp.processEvents()
    assert APP_DISPLAY_NAME in window.windowTitle()
    assert "Vanta" in window.windowTitle()
    # Gates banner: nothing is ready with an empty shadow history.
    assert "NOT ready for Muuk integration" in window._gates_banner.text()
    # Seeded values are visible, with provenance.
    assert "4,000" in window._price_value.text()
    assert window._score_value.text().startswith("Total: +22.0")
    assert window._components_list.count() >= 5
    assert window._freshness_list.count() == len(
        __import__("goldboard_app.worker", fromlist=["FRESHNESS_SOURCES"]).FRESHNESS_SOURCES)
    assert window._gates_list.count() == 9
    assert window._cot_summary.text() != "No COT data yet."
    assert "Finnhub: key required" in window._news_label.text()
    window.close()


def test_main_window_empty_db_shows_no_data_not_fake_values(
        tmp_path, monkeypatch, qapp):
    db = tmp_path / "empty.db"
    init_db(db)
    monkeypatch.setattr("goldboard_app.worker.refresh_all", lambda db_path: {})
    monkeypatch.setattr("goldboard_app.worker._fetch_history", lambda *a: None)
    monkeypatch.setattr("goldboard_app.worker._fetch_news", lambda *a: None)
    window = MainWindow(db_path=db, enable_live_ticker=False)
    window.show()
    qapp.processEvents()
    assert "No score yet" in window._score_value.text()
    assert "No signal yet" in window._signal_label.text()
    assert "NOT ready for Muuk integration" in window._gates_banner.text()
    assert "No price yet" in window._price_source.text()
    window.close()


def test_main_window_has_live_widgets(tmp_path, monkeypatch, qapp):
    """v0.2.0: live chart, news list, backtest panel, settings button."""
    db = tmp_path / "goldboard.db"
    _seed_db(db)
    monkeypatch.setattr("goldboard_app.worker.refresh_all", lambda db_path: {})
    monkeypatch.setattr("goldboard_app.worker._fetch_history", lambda *a: None)
    monkeypatch.setattr("goldboard_app.worker._fetch_news", lambda *a: None)
    window = MainWindow(db_path=db, enable_live_ticker=False)
    window.show()
    qapp.processEvents()
    assert window._settings_button is not None
    assert window._price_chart is not None
    assert window._news_list is not None
    assert window._backtest_button is not None
    assert window._live_dot is not None
    assert "History stored" in window._backtest_label.text()
    window.close()


def test_settings_form_saves_keys(tmp_path, monkeypatch, qapp):
    """v0.7.0: the settings FORM (was a dialog) round-trips keys +
    intervals to config; the header Settings button switches to the
    System tab instead of opening a dialog."""
    from goldboard_app.main_window import SettingsForm
    from goldboard_core.config import load_config

    monkeypatch.setenv("GOLD_BOARD_CONFIG_DIR", str(tmp_path / "cfg"))
    cfg = load_config()
    form = SettingsForm(cfg)
    form._finnhub_edit.setText("fh-test-key")
    form._fred_edit.setText("fred-test-key")
    form._fedwatch_edit.setText("fw-test-key")
    form._fedwatch_url_edit.setText("https://example.invalid/fw")
    form._price_spin.setValue(5)
    form._full_spin.setValue(30)
    form.apply_to(cfg)
    assert cfg.finnhub_api_key == "fh-test-key"
    assert cfg.fred_api_key == "fred-test-key"
    assert cfg.fedwatch_api_key == "fw-test-key"
    assert cfg.fedwatch_api_url == "https://example.invalid/fw"
    assert cfg.price_refresh_seconds == 5
    assert cfg.full_refresh_minutes == 30
    # Reset restores the form from the config.
    form._finnhub_edit.setText("changed")
    form.load_from(cfg)
    assert form._finnhub_edit.text() == "fh-test-key"
    form.close()


def test_settings_button_switches_to_system_tab(tmp_path, monkeypatch, qapp):
    """v0.7.0: header Settings switches to the System tab, where the
    form lives; the Save button persists it to the config file."""
    from goldboard_core.config import load_config

    monkeypatch.setenv("GOLD_BOARD_CONFIG_DIR", str(tmp_path / "cfg"))
    window = _open_window(tmp_path, monkeypatch, qapp)
    assert window._tabs.currentIndex() != window._tab_index["system"]
    window._settings_button.click()
    qapp.processEvents()
    assert window._tabs.currentIndex() == window._tab_index["system"]
    assert window._settings_form is not None
    # Save persists through the form.
    window._settings_form._finnhub_edit.setText("fh-saved-key")
    window._settings_save_button.click()
    qapp.processEvents()
    assert load_config().finnhub_api_key == "fh-saved-key"
    window.close()


def test_run_history_backtest_on_seeded_bars(tmp_path):
    """v0.2.0: replay backtest runs on stored daily bars, no network."""
    from datetime import timedelta

    from goldboard_core.storage import save_price_bars
    from goldboard_app.worker import run_history_backtest

    db = str(tmp_path / "bt.db")
    init_db(db)
    bars = []
    for i in range(40):
        close = 100.0 if i < 20 else 100.0 + (i - 19) * 2.0
        bars.append({
            "ts": NOW - timedelta(days=40 - i),
            "open": close - 0.5, "high": close + 1.0,
            "low": close - 1.0, "close": close,
            "volume": 1000.0, "interval": "1d",
        })
    save_price_bars(db, bars)
    result = run_history_backtest(db)
    assert result["bars_daily"] == 40
    assert result["live"]["n_trades"] == 0  # no board signals stored
    assert result["replay"]["n_trades"] >= 1
    assert len(result["folds"]) == 3
    assert "Price-only replay" in result["rule"]


def test_live_price_worker_emits_ticks(tmp_path, monkeypatch, qapp):
    """v0.3.0: ticker polls the multi-source aggregator (network faked)."""
    from datetime import datetime, timezone

    from goldboard_app.worker import LivePriceWorker

    monkeypatch.setattr(
        "goldboard_data.price_sources.fetch_best_price",
        lambda: {"value": 3539.0,
                 "as_of": datetime(2026, 11, 21, 20, 10, 38,
                                   tzinfo=timezone.utc),
                 "source": "gold-api.com", "kind": "spot",
                 "tried": [{"source": "gold-api.com", "ok": True,
                            "error": None}],
                 "diverged": False, "quotes": []})
    ticks: list[dict] = []
    worker = LivePriceWorker(interval_seconds=5)
    worker.price_updated.connect(ticks.append)
    loop = QEventLoop()
    worker.price_updated.connect(loop.quit)
    QTimer.singleShot(8000, loop.quit)  # hard timeout, never hangs
    worker.start()
    loop.exec()
    worker.stop()
    worker.wait(3000)
    assert len(ticks) >= 1
    assert ticks[0]["value"] == pytest.approx(3539.0)
    assert ticks[0]["source"] == "gold-api.com"
    assert ticks[0]["kind"] == "spot"


def test_technical_panel_renders_summary_and_setups(
        tmp_path, monkeypatch, qapp):
    """v0.3.0: technical summary panel + setups list render from
    stored bars (no network)."""
    from datetime import timedelta

    from goldboard_core.storage import save_price_bars

    db = tmp_path / "goldboard.db"
    _seed_db(db)
    price = 3000.0
    bars = []
    base = datetime(2025, 1, 1, tzinfo=timezone.utc)
    for i in range(250):
        price += 3.0
        bars.append({"ts": base + timedelta(days=i),
                     "open": price - 0.5, "high": price + 1.0,
                     "low": price - 1.0, "close": price, "volume": None,
                     "interval": "1d"})
    save_price_bars(db, bars)
    payload = collect_dashboard_data(db, refresh=False)
    assert payload["technical"]["complete"] is True
    assert payload["technical"]["signal"] in ("buy", "strong buy")
    assert len(payload["setups"]) >= 1
    window = MainWindow(db_path=db, enable_live_ticker=False)
    window.show()
    qapp.processEvents()
    window._on_refresh_finished(payload)
    assert "Aggregate:" in window._technical_label.text()
    assert "RSI(14)" in window._technical_label.text()
    assert window._setups_list.count() >= 1
    window.close()


def test_no_refresh_starts_after_window_close(tmp_path, monkeypatch, qapp):
    """Regression: closing the window must cancel the pending 500ms
    startup refresh — a static singleShot fired after close and left
    an ownerless live refresh running (Windows CI exited 1 on it
    even though all 178 tests had passed)."""
    from PySide6.QtTest import QTest

    calls: list[str] = []
    monkeypatch.setattr(
        "goldboard_app.worker.refresh_all",
        lambda db_path: calls.append(db_path) or {})
    monkeypatch.setattr("goldboard_app.worker._fetch_history", lambda *a: None)
    monkeypatch.setattr("goldboard_app.worker._fetch_news", lambda *a: None)
    db = tmp_path / "goldboard.db"
    _seed_db(db)
    window = MainWindow(db_path=db, enable_live_ticker=False)
    window.show()
    qapp.processEvents()
    window.close()
    qapp.processEvents()
    # Wait past the 500ms startup timer (and the 1s ticker timer).
    QTest.qWait(1500)
    qapp.processEvents()
    assert calls == []


def _open_window(tmp_path, monkeypatch, qapp):
    """Window with the background refresh stubbed out (offline)."""
    monkeypatch.setattr("goldboard_app.worker.refresh_all", lambda db_path: {})
    monkeypatch.setattr("goldboard_app.worker._fetch_history", lambda *a: None)
    monkeypatch.setattr("goldboard_app.worker._fetch_news", lambda *a: None)
    db = tmp_path / "goldboard.db"
    _seed_db(db)
    window = MainWindow(db_path=db, enable_live_ticker=False)
    window.show()
    qapp.processEvents()
    return window


def test_update_button_always_visible(tmp_path, monkeypatch, qapp):
    """v0.4.0: the update button is visible from the start — before
    v0.4.0 it only appeared after a successful check found a newer
    release, so a failed check left the user with no button at all."""
    window = _open_window(tmp_path, monkeypatch, qapp)
    assert window._update_button.isVisible()
    assert window._update_button.text() in (
        "Check for updates", "Checking…", "✓ Up to date")
    # Up-to-date state keeps the button visible.
    window._on_update_checked(None, None)
    assert window._update_button.isVisible()
    assert window._update_button.text() == "✓ Up to date"
    # A newer release turns it into the gold update button.
    window._on_update_checked(
        {"version": "9.9.9", "html_url": "https://example.invalid"}, None)
    assert window._update_button.isVisible()
    assert "9.9.9" in window._update_button.text()
    assert window._pending_update["version"] == "9.9.9"
    window.close()


def test_panels_menu_hides_and_restores(tmp_path, monkeypatch, qapp):
    """v0.4.0: hiding a panel from the Panels menu removes it from
    its tab and remembers the choice in QSettings. (v0.7.0: panels
    live in tabs now, so the tab must be current before isVisible()
    means anything — a panel in a background tab is hidden by Qt.)"""
    window = _open_window(tmp_path, monkeypatch, qapp)
    assert window._panels_button is not None
    assert window._panels_menu is not None
    window._tabs.setCurrentIndex(window._tab_index["macro_news"])
    qapp.processEvents()
    news_box = next(p["box"] for p in window._panels
                    if p["key"] == "news")
    assert news_box.isVisible()
    window._toggle_panel("news", False)
    qapp.processEvents()
    assert not news_box.isVisible()
    assert window.settings.value("panels/news", True, type=bool) is False
    assert all(p["box"] is not news_box
               for p in window._panels
               if window._panel_visible.get(p["key"], True))
    window._toggle_panel("news", True)
    qapp.processEvents()
    assert news_box.isVisible()
    assert window.settings.value("panels/news", False, type=bool) is True
    window.close()


def test_volume_profile_and_events_panels_render(tmp_path, monkeypatch, qapp):
    """v0.4.0: a refresh payload with a volume profile and events
    renders both new panels."""
    from goldboard_core.volume_profile import volume_profile

    window = _open_window(tmp_path, monkeypatch, qapp)
    bars = [{"open": 3500.0 + i, "high": 3502.0 + i, "low": 3498.0 + i,
             "close": 3500.0 + i, "volume": 10.0 if i % 2 else 1.0}
            for i in range(40)]
    payload = {
        "snapshots": {}, "scores": {}, "total_score": None,
        "signal": None, "gates": {}, "cot_history": [],
        "price_bars_daily": bars, "price_bars_intraday": bars,
        "technical": {}, "setups": [],
        "volume_profile": volume_profile(bars),
        "events": [{"name": "US PCE inflation (Oct)", "date": "2026-11-25",
                    "days_away": 3,
                    "impact": "Hot PCE -> gold pressured."}],
        "errors": [],
    }
    window._on_refresh_finished(payload)
    qapp.processEvents()
    assert window._volume_widget._profile.get("complete") is True
    assert "POC" in window._volume_label.text()
    assert "US PCE" in window._events_list.item(0).text()
    assert "PCE" in window._events_label.text()
    window.close()


def test_tradingview_chart_falls_back_without_webengine(monkeypatch, qapp):
    """v0.4.0: without PySide6-WebEngine the chart widget shows the
    built-in chart instead of a blank panel."""
    import goldboard_app.tradingview_chart as tvc
    from PySide6.QtWidgets import QLabel

    from goldboard_app.tradingview_chart import TradingViewChart

    monkeypatch.setattr(tvc, "WEBENGINE_AVAILABLE", False)
    fallback = QLabel("built-in chart")
    chart = TradingViewChart(fallback)
    assert chart.currentWidget() is fallback
    assert chart.is_live is False
    chart.show_fallback()
    assert chart.currentWidget() is fallback
