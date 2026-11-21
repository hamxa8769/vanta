"""Headless dashboard screenshot (dev tool only — NOT live data).

Seeds a throwaway temp DB from data/fixtures/q3_2026_snapshot.json
(labelled TEST FIXTURE ONLY) plus a few extra scored snapshots so
every panel has something to show, launches the main window
offscreen, grabs it, and saves docs/screenshot-dashboard.png.

The seed is demo data for the screenshot ONLY — the shipped app never
loads the fixture as live data.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from PySide6.QtWidgets import QApplication  # noqa: E402

import goldboard_app.worker as worker_mod  # noqa: E402
from goldboard_app.main_window import MainWindow  # noqa: E402
from goldboard_core.models import SourceSnapshot  # noqa: E402
from goldboard_core.storage import init_db, save_snapshot  # noqa: E402

FIXTURE = ROOT / "data" / "fixtures" / "q3_2026_snapshot.json"
OUT = ROOT / "docs" / "screenshot-dashboard.png"

NOW = datetime.now(timezone.utc)

# Extra demo-seed rows (fixture only covers 3 series) so every panel
# renders with values. Clearly demo data — screenshot only.
DEMO_EXTRA = [
    ("price", "price_xauusd", 4125.50, "USD/oz"),
    ("cftc", "cot_score", 60.0, ""),
    ("wgc", "central_bank_etf_score", 40.0, ""),
    ("fred", "macro_score", -20.0, ""),
    ("gdelt", "news_score", 10.0, ""),
    ("comex", "microstructure_score", 0.0, ""),
    ("cftc", "cot_gold_mm_net", 102942.0, "contracts"),
    ("cftc", "cot_gold_mm_net_pct_oi", 35.0, "pct_of_oi"),
    ("etf", "etf_gld_tonnes", 1034.0, "tonnes"),
    ("etf", "etf_gld_daily_change_tonnes", 2.5, "tonnes"),
    ("etf", "etf_iau_tonnes", 500.0, "tonnes"),
    ("wgc", "wgc_cb_net_tonnes", 321.0, "tonnes"),
    ("fred", "fred_dfii10", 1.85, "%"),
    ("fred", "fred_dtwexbgs", 98.5, "index"),
    ("fred", "fred_dgs10", 4.10, "%"),
    ("gdelt", "gdelt_gold_tone_latest", 2.1, ""),
    ("gdelt", "gdelt_gold_tone_7d_avg", 1.4, ""),
]


def seed_demo_db(db_path: Path) -> None:
    """Seed the temp DB from the labelled test fixture + demo extras."""
    init_db(db_path)
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert "TEST FIXTURE ONLY" in data["label"]
    for raw in data["snapshots"]:
        save_snapshot(db_path, SourceSnapshot(
            source=raw["source"], series_id=raw["series_id"],
            value=float(raw["value"]), unit=raw["unit"],
            as_of=datetime.fromisoformat(raw["as_of"]),
            fetched_at=NOW, status="ok", source_url=raw["source_url"]))
    for source, series_id, value, unit in DEMO_EXTRA:
        save_snapshot(db_path, SourceSnapshot(
            source=source, series_id=series_id, value=value, unit=unit,
            as_of=NOW, fetched_at=NOW, status="ok",
            source_url=f"https://example.invalid/{source}"))
    # COT history points for the chart.
    for value in (80000, 90000, 95000, 102942):
        save_snapshot(db_path, SourceSnapshot(
            source="cftc", series_id="cot_gold_mm_net",
            value=float(value), unit="contracts",
            as_of=NOW, fetched_at=NOW, status="ok",
            source_url="https://example.invalid/cftc"))


def main() -> int:
    # No network during the screenshot: the background refresh is stubbed
    # and the GitHub update check is disabled entirely.
    worker_mod.refresh_all = lambda db_path: {}
    MainWindow.check_for_updates = lambda self: None  # noqa: ARG005
    with tempfile.TemporaryDirectory(prefix="vanta-shot-") as tmp:
        db_path = Path(tmp) / "demo.db"
        seed_demo_db(db_path)
        app = QApplication(sys.argv)
        window = MainWindow(db_path=db_path)
        window.resize(1400, 950)
        window.show()
        app.processEvents()
        # Let the window paint (header clock, gauge, chart, lists).
        from PySide6.QtCore import QEventLoop, QTimer
        loop = QEventLoop()
        QTimer.singleShot(2500, loop.quit)
        loop.exec()
        app.processEvents()
        pixmap = window.grab()
        OUT.parent.mkdir(parents=True, exist_ok=True)
        if pixmap.isNull() or not pixmap.save(str(OUT), "PNG"):
            print("ERROR: screenshot grab failed (null pixmap)", file=sys.stderr)
            return 1
        window.close()
        # Drain: let the background refresh / update-check threads finish
        # (or be waited on by closeEvent) before the interpreter exits,
        # otherwise Qt aborts with "QThread destroyed while running".
        loop2 = QEventLoop()
        QTimer.singleShot(500, loop2.quit)
        loop2.exec()
        app.processEvents()
    print(str(OUT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
