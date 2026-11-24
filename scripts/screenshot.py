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
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# Chromium runs as root in this VM: without --no-sandbox its zygote
# process aborts and the TradingView page never loads.
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS",
                      "--no-sandbox --disable-gpu")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from PySide6.QtWidgets import QApplication  # noqa: E402

import goldboard_app.worker as worker_mod  # noqa: E402
from goldboard_app.main_window import MainWindow  # noqa: E402
from goldboard_core.models import SourceSnapshot  # noqa: E402
from goldboard_core.storage import init_db, save_snapshot  # noqa: E402

FIXTURE = ROOT / "data" / "fixtures" / "q3_2026_snapshot.json"
OUT_DARK = ROOT / "docs" / "screenshot-dashboard.png"
OUT_LIGHT = ROOT / "docs" / "screenshot-dashboard-light.png"

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
    # Price bars + Vanta signals (v0.8.0): the markers screenshot
    # needs real bars and real stored signals to plot. Rising daily
    # bars (250d from 2025-01-01) + intraday bars, and two signals
    # whose outcomes the backtest engine can already tell: a buy
    # on day 150 (TP +30 at 2R hits on day 10) and a sell on day
    # 220 (SL +30 hits on day 10). Demo data — screenshot only.
    from goldboard_core.models import Signal
    from goldboard_core.storage import save_price_bars, save_signal

    base = datetime(2025, 1, 1, tzinfo=timezone.utc)
    price = 3000.0
    bars = []
    for i in range(250):
        price += 3.0
        bars.append({"ts": base + timedelta(days=i),
                     "open": price - 0.5, "high": price + 1.0,
                     "low": price - 1.0, "close": price, "volume": None,
                     "interval": "1d"})
    save_price_bars(db_path, bars)
    intra = []
    for i in range(96):
        p = 3500.0 + (i % 10)
        intra.append({"ts": base + timedelta(minutes=15 * i),
                      "open": p, "high": p + 2.0, "low": p - 2.0,
                      "close": p, "volume": 10.0 if i % 2 else 1.0,
                      "interval": "15m"})
    save_price_bars(db_path, intra)
    buy_bar = bars[150]
    save_signal(db_path, Signal(
        direction="buy", entry=buy_bar["close"],
        sl=buy_bar["close"] - 15, tp=buy_bar["close"] + 30,
        confidence=0.86, reason="Demo signal for the markers screenshot.",
        generated_at=buy_bar["ts"] + timedelta(hours=12),
        rule_version="v0.1.0", horizon_days=10, status="open"))
    sell_bar = bars[220]
    save_signal(db_path, Signal(
        direction="sell", entry=sell_bar["close"],
        sl=sell_bar["close"] + 30, tp=sell_bar["close"] - 60,
        confidence=0.82, reason="Demo signal for the markers screenshot.",
        generated_at=sell_bar["ts"] + timedelta(hours=12),
        rule_version="v0.1.0", horizon_days=10, status="open"))


def main() -> int:
    """Usage: python scripts/screenshot.py [--theme dark|light]
    [--size WxH] [--view=tradingview|signals] [--range=2Y]
    [--expand-tab=<key>]. Default size is 1400x950; --size 1366x768
    is the user's screen (v0.7.1 layout check)."""
    args = sys.argv[1:]
    theme = "light" if "light" in args else "dark"
    out = OUT_LIGHT if theme == "light" else OUT_DARK
    width, height = 1400, 950
    view = "tradingview"
    chart_range = None
    expand_tab = None
    for arg in args:
        if arg.startswith("--size="):
            try:
                width, height = (int(part) for part in
                                 arg.split("=", 1)[1].split("x", 1))
            except ValueError:
                print("ERROR: --size must be WxH, e.g. --size=1366x768",
                      file=sys.stderr)
                return 2
        elif arg.startswith("--view="):
            view = arg.split("=", 1)[1]
            if view not in ("tradingview", "signals"):
                print("ERROR: --view must be tradingview|signals",
                      file=sys.stderr)
                return 2
        elif arg.startswith("--range="):
            chart_range = arg.split("=", 1)[1]
        elif arg.startswith("--expand-tab="):
            expand_tab = arg.split("=", 1)[1]
    suffix = "-light" if theme == "light" else ""
    if expand_tab:
        out = out.with_name(
            f"screenshot-tab-{expand_tab}-{width}x{height}{suffix}.png")
    elif view == "signals":
        out = out.with_name(
            f"screenshot-dashboard-{width}x{height}-signals{suffix}.png")
    elif (width, height) != (1400, 950):
        out = out.with_name(
            f"screenshot-dashboard-{width}x{height}{suffix}.png")
    # No network during the screenshot: the background refresh is stubbed
    # and the GitHub update check is disabled entirely.
    worker_mod.refresh_all = lambda db_path: {}
    MainWindow.check_for_updates = lambda self: None  # noqa: ARG005
    from PySide6.QtCore import QSettings

    from goldboard_core.branding import set_theme

    settings = QSettings("Vanta", "GoldBoard")
    previous_theme = settings.value("theme", "dark", type=str) or "dark"
    settings.setValue("theme", theme)
    set_theme(theme)
    with tempfile.TemporaryDirectory(prefix="vanta-shot-") as tmp:
        db_path = Path(tmp) / "demo.db"
        seed_demo_db(db_path)
        app = QApplication(sys.argv)
        window = MainWindow(db_path=db_path)
        window.resize(width, height)
        window.show()
        app.processEvents()
        # Let the window paint (header clock, gauge, chart, lists).
        from PySide6.QtCore import QEventLoop, QTimer
        loop = QEventLoop()
        QTimer.singleShot(2500, loop.quit)
        loop.exec()
        app.processEvents()
        # Switch to the requested chart view / range / expanded tab
        # (v0.8.0) after the startup refresh has applied its payload.
        target = window
        if chart_range:
            window._set_chart_range(chart_range)
        if view == "signals":
            window._set_chart_view("signals")
        if expand_tab:
            window._expand_tab(expand_tab)
            app.processEvents()
            if window._tab_dialog is not None:
                target = window._tab_dialog
                # The dialog is a separate top-level window: give
                # it a real paint pass before grabbing, or the grab
                # comes back black (its content has not laid out).
                loop = QEventLoop()
                QTimer.singleShot(1000, loop.quit)
                loop.exec()
                app.processEvents()
        app.processEvents()
        pixmap = target.grab()
        out.parent.mkdir(parents=True, exist_ok=True)
        if pixmap.isNull() or not pixmap.save(str(out), "PNG"):
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
    settings.setValue("theme", previous_theme)
    set_theme(previous_theme)
    print(str(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
