"""Desktop UI tests — headless only (QT_QPA_PLATFORM=offscreen).

No display and no network required: the refresh worker is run against
a mocked registry, and the window is built from a small seeded DB.
Skipped with a clear reason when PySide6 is not installed.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

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
    payload = collect_dashboard_data(db, refresh=True)
    assert payload["errors"] == []
    assert payload["report"]["price"]["status"] == "ok"
    # 0.25*60 + 0.20*40 + 0.25*(-20) + 0.15*10 + 0.15*0 = 19.5
    assert payload["score"] is not None
    assert payload["score"].total == pytest.approx(19.5)
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
    received: list[dict] = []
    errors: list[str] = []
    worker = RefreshWorker(str(db))
    worker.finished.connect(received.append)
    worker.failed.connect(errors.append)
    worker.run()  # synchronous run: same code path, no thread scheduling
    assert errors == []
    assert len(received) == 1
    assert received[0]["score"] is not None
    assert received[0]["score"].total == pytest.approx(19.5)


def test_refresh_worker_runs_on_qthread(tmp_path, monkeypatch, qapp):
    db = tmp_path / "goldboard.db"
    _seed_db(db)
    monkeypatch.setattr(
        "goldboard_app.worker.refresh_all", lambda db_path: {})
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
    window = MainWindow(db_path=db)
    window.show()
    qapp.processEvents()
    assert APP_DISPLAY_NAME in window.windowTitle()
    assert "Vanta" in window.windowTitle()
    # Gates banner: nothing is ready with an empty shadow history.
    assert "NOT ready for Muuk integration" in window._gates_banner.text()
    # Seeded values are visible, with provenance.
    assert "4,000" in window._price_value.text()
    assert window._score_value.text().startswith("Total: +19.5")
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
    window = MainWindow(db_path=db)
    window.show()
    qapp.processEvents()
    assert "No score yet" in window._score_value.text()
    assert "No signal yet" in window._signal_label.text()
    assert "NOT ready for Muuk integration" in window._gates_banner.text()
    assert "No price yet" in window._price_source.text()
    window.close()
