"""Vanta desktop UI (Phase 4) — PySide6/Qt, black + gold, shadow-only.

Single main screen with eleven panels (live price + chart, score gauge,
COT chart, ETF flow, central-bank buying, macro, live news headlines,
signals, gates, freshness, history backtest), a live price ticker
(Yahoo quote every 10s), a live clock, Refresh + auto-refresh on a
QTimer, refresh in a QThread (never blocks the UI), a Settings page
for API keys, an in-app updater, threshold alerts, and a log drawer.

Honesty rule: every number shows source + timestamp. A failed source
shows FAILED/STALE — never an invented value. No live trading anywhere.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QSettings, Qt, QThread, QTimer, Signal, QUrl
from PySide6.QtGui import (
    QAction,
    QColor,
    QDesktopServices,
    QFont,
    QIcon,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QDockWidget,
    QFormLayout,
    QGroupBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QStatusBar,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from goldboard_core.branding import (
    APP_DISPLAY_NAME,
    LOGO_DIR,
    PALETTE,
    get_app_icon_path,
)
from goldboard_core.config import get_config_path, load_config, save_config
from goldboard_core.gates import GATE_THRESHOLDS
from goldboard_data.fedwatch import load_fomc_calendar, next_meetings

from . import __version__
from .tradingview_chart import TradingViewChart
from .updater import (
    NO_CHANNEL_MESSAGE,
    check_for_update_live,
    download_asset,
    fetch_expected_sha256,
    install_update,
)
from .worker import (
    FRESHNESS_SOURCES,
    BacktestWorker,
    LivePriceWorker,
    RefreshWorker,
    collect_dashboard_data,
    run_history_backtest,
)

# Status colours are functional (ok / stale / failed), not brand colours;
# every brand surface uses PALETTE from branding.py.
STATUS_COLORS = {"ok": "#2E9E5B", "stale": "#C77E2A", "failed": "#C0392B"}

IMF_HOLDINGS_NOTE = "IMF holds 2,814.1t — it does not trade gold (not a flow)."



def _font(size: int, bold: bool = False) -> QFont:
    """Default-family font. Never pass an empty family to QFont: on some
    Linux font sets Qt resolves "" to the first family alphabetically,
    which can be a TeX math font with no digits (clock rendered as
    garbage glyphs)."""
    font = QFont()
    font.setPointSize(size)
    font.setBold(bold)
    return font

def resource_path(relative: str | Path) -> Path:
    """Resolve a repo-relative path, including inside a PyInstaller bundle."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))
    return base / relative


def _fmt_value(value: float | None, unit: str = "", digits: int = 2) -> str:
    if value is None:
        return "—"
    text = f"{value:,.{digits}f}"
    return f"{text} {unit}".strip() if unit else text


def _fmt_time(value: datetime | str | None) -> str:
    if value is None:
        return "—"
    if isinstance(value, str):
        return value[:19].replace("T", " ")
    return value.strftime("%Y-%m-%d %H:%M")


def _snapshot_line(snap, label: str = "") -> str:
    """One line for a snapshot: value + source + timestamp, or NO DATA."""
    if snap is None:
        return f"{label}: no data yet".strip(": ")
    status = "" if snap.status == "ok" else f" [{snap.status.upper()}]"
    proxy = " (proxy)" if getattr(snap, "is_proxy", False) else ""
    stamp = _fmt_time(snap.fetched_at or snap.as_of)
    prefix = f"{label}: " if label else ""
    return (
        f"{prefix}{_fmt_value(snap.value, snap.unit)}"
        f" · {snap.source}{status}{proxy} · {stamp}"
    )


# ------------------------------------------------------------------ widgets

class ScoreGauge(QWidget):
    """Horizontal -100..+100 gauge with a marker at the total score."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._value: float | None = None
        self.setMinimumHeight(76)

    def set_value(self, value: float | None) -> None:
        self._value = value
        self.update()

    def paintEvent(self, event) -> None:  # noqa: D102 — Qt paint hook
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        width, height = self.width(), self.height()
        bar_y, bar_h = 22, 18
        # Track: bearish (left) → neutral → bullish (right, gold).
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("#3A3A3C"))
        painter.drawRoundedRect(0, bar_y, width // 2, bar_h, 4, 4)
        painter.setBrush(QColor(PALETTE["gold"]))
        painter.drawRoundedRect(width // 2, bar_y, width // 2, bar_h, 4, 4)
        painter.setPen(QColor(PALETTE["muted"]))
        painter.drawText(0, 2, 40, 12, Qt.AlignLeft, "-100")
        painter.drawText(width // 2 - 20, 2, 40, 12, Qt.AlignCenter, "0")
        painter.drawText(width - 40, 2, 40, 12, Qt.AlignRight, "+100")
        if self._value is not None:
            frac = (max(-100.0, min(100.0, self._value)) + 100.0) / 200.0
            x = int(frac * width)
            painter.setPen(QPen(QColor(PALETTE["text"]), 2))
            painter.drawLine(x, bar_y - 6, x, bar_y + bar_h + 6)
            painter.setPen(QColor(PALETTE["text"]))
            painter.setFont(_font(11, bold=True))
            painter.drawText(max(0, min(width - 60, x - 30)), bar_y + bar_h + 4,
                             60, 16, Qt.AlignCenter, f"{self._value:+.0f}")
        else:
            painter.setPen(QColor(PALETTE["muted"]))
            painter.drawText(0, bar_y + bar_h + 8, width, 16,
                             Qt.AlignCenter, "no score yet — no data")


class CotChart(QWidget):
    """Custom-painted managed-money net history (matplotlib-free)."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._points: list[dict] = []
        self.setMinimumHeight(150)

    def set_points(self, points: list[dict]) -> None:
        self._points = points or []
        self.update()

    def paintEvent(self, event) -> None:  # noqa: D102 — Qt paint hook
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        width, height = self.width(), self.height()
        if len(self._points) < 2:
            painter.setPen(QColor(PALETTE["muted"]))
            painter.drawText(0, 0, width, height, Qt.AlignCenter,
                             "No COT history yet — refresh to fetch CFTC data.")
            return
        values = [p["value"] for p in self._points]
        lo, hi = min(values), max(values)
        span = (hi - lo) or 1.0
        pad_l, pad_r, pad_t, pad_b = 8, 8, 30, 22
        plot_w = width - pad_l - pad_r
        plot_h = height - pad_t - pad_b

        def xy(i: int, v: float) -> tuple[float, float]:
            x = pad_l + (plot_w * i / (len(values) - 1))
            y = pad_t + plot_h * (1.0 - (v - lo) / span)
            return x, y

        # Zero line.
        if lo < 0 < hi:
            _, zy = xy(0, 0.0)
            painter.setPen(QPen(QColor(PALETTE["muted"]), 1, Qt.DashLine))
            painter.drawLine(pad_l, int(zy), width - pad_r, int(zy))
        # Gold line through the points.
        path = QPainterPath()
        x0, y0 = xy(0, values[0])
        path.moveTo(x0, y0)
        for i, v in enumerate(values[1:], start=1):
            x, y = xy(i, v)
            path.lineTo(x, y)
        painter.setPen(QPen(QColor(PALETTE["gold"]), 2))
        painter.drawPath(path)
        # Last point.
        lx, ly = xy(len(values) - 1, values[-1])
        painter.setBrush(QColor(PALETTE["gold_light"]))
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(int(lx) - 4, int(ly) - 4, 8, 8)
        # Min / max labels.
        painter.setPen(QColor(PALETTE["muted"]))
        painter.setFont(_font(9))
        painter.drawText(pad_l, 4, 200, 12, Qt.AlignLeft, f"max {hi:,.0f}")
        painter.drawText(pad_l, height - 8, 200, 12, Qt.AlignLeft,
                         f"min {lo:,.0f}  ·  {len(values)} points")


class PriceChart(QWidget):
    """Custom-painted gold price chart from stored OHLC bars (live).

    Bars come from the DB (Yahoo history: daily 2y / intraday 5d) —
    never invented. ``live_tick`` is the latest ticker quote, drawn
    as a dot at the right edge so the chart moves between refreshes.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._bars: list[dict] = []
        self._live_tick: dict | None = None
        self.setMinimumHeight(170)

    def set_bars(self, bars: list[dict], live_tick: dict | None = None) -> None:
        self._bars = bars or []
        self._live_tick = live_tick
        self.update()

    def set_live_tick(self, live_tick: dict | None) -> None:
        self._live_tick = live_tick
        self.update()

    def paintEvent(self, event) -> None:  # noqa: D102 — Qt paint hook
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        width, height = self.width(), self.height()
        bars = self._bars
        if len(bars) < 2 and self._live_tick is None:
            painter.setPen(QColor(PALETTE["muted"]))
            painter.drawText(0, 0, width, height, Qt.AlignCenter,
                             "No price history yet — refresh to fetch it.")
            return
        closes = [float(b["close"]) for b in bars]
        if self._live_tick is not None:
            closes.append(float(self._live_tick["value"]))
        lo, hi = min(closes), max(closes)
        span = (hi - lo) or 1.0
        pad_l, pad_r, pad_t, pad_b = 8, 8, 30, 22
        plot_w = width - pad_l - pad_r
        plot_h = height - pad_t - pad_b
        n = len(closes)

        def xy(i: int, v: float) -> tuple[float, float]:
            x = pad_l + (plot_w * i / max(1, n - 1))
            y = pad_t + plot_h * (1.0 - (v - lo) / span)
            return x, y

        # Gold line through the closes.
        path = QPainterPath()
        x0, y0 = xy(0, closes[0])
        path.moveTo(x0, y0)
        for i, v in enumerate(closes[1:], start=1):
            x, y = xy(i, v)
            path.lineTo(x, y)
        painter.setPen(QPen(QColor(PALETTE["gold"]), 2))
        painter.drawPath(path)
        # Last stored point + live tick dot.
        lx, ly = xy(n - 1, closes[-1])
        painter.setBrush(QColor(PALETTE["gold_light"]))
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(int(lx) - 4, int(ly) - 4, 8, 8)
        painter.setPen(QColor(PALETTE["muted"]))
        painter.setFont(_font(9))
        interval = bars[-1].get("interval", "1d") if bars else "—"
        painter.drawText(pad_l, 4, 300, 12, Qt.AlignLeft,
                         f"high {hi:,.2f}  ·  low {lo:,.2f}")
        painter.drawText(pad_l, height - 8, 300, 12, Qt.AlignLeft,
                         f"{len(bars)} bars ({interval})"
                         + ("  ·  LIVE" if self._live_tick else ""))


class VolumeProfileWidget(QWidget):
    """Horizontal volume profile: volume per price bin (POC in gold,
    value area shaded). Built from stored intraday bars — no tick
    data needed; true bid/ask footprint is NOT shown here (paid CME
    tick data) and the panel says so."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._profile: dict = {}
        self.setMinimumHeight(170)

    def set_profile(self, profile: dict) -> None:
        self._profile = profile or {}
        self.update()

    def paintEvent(self, event) -> None:  # noqa: D102 — Qt paint hook
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        width, height = self.width(), self.height()
        profile = self._profile
        bins = profile.get("bins") or []
        if not profile.get("complete") or not bins:
            painter.setPen(QColor(PALETTE["muted"]))
            painter.drawText(0, 0, width, height, Qt.AlignCenter,
                             profile.get("reason") or
                             "No volume profile yet — refresh to fetch "
                             "intraday history.")
            return
        max_vol = max(b["volume"] for b in bins) or 1.0
        pad_l, pad_r, pad_t, pad_b = 64, 8, 8, 8
        plot_w = width - pad_l - pad_r
        plot_h = height - pad_t - pad_b
        row_h = plot_h / len(bins)
        va_lo = profile.get("value_area_low")
        va_hi = profile.get("value_area_high")
        poc = profile.get("poc")
        for i, b in enumerate(bins):
            # Bins are low→high; draw high price at the top.
            j = len(bins) - 1 - i
            y = pad_t + j * row_h
            bar_w = plot_w * (b["volume"] / max_vol)
            mid = b["mid"]
            in_va = (va_lo is not None and va_hi is not None
                     and va_lo <= mid <= va_hi)
            half_bin = ((bins[1]["mid"] - bins[0]["mid"]) / 2
                        if len(bins) > 1 else 0.0)
            is_poc = poc is not None and abs(mid - poc) < half_bin
            if is_poc:
                painter.setBrush(QColor(PALETTE["gold"]))
            elif in_va:
                painter.setBrush(QColor("#8a6d2b"))
            else:
                painter.setBrush(QColor("#3A3A3C"))
            painter.setPen(Qt.NoPen)
            painter.drawRect(int(pad_l), int(y) + 1,
                             max(1, int(bar_w)), max(1, int(row_h) - 2))
            painter.setPen(QColor(PALETTE["muted"]))
            painter.setFont(_font(9))
            painter.drawText(0, int(y), pad_l - 4, int(row_h),
                             Qt.AlignRight | Qt.AlignVCenter,
                             f"{mid:,.0f}")
        painter.setPen(QColor(PALETTE["muted"]))
        painter.setFont(_font(9))
        painter.drawText(pad_l, height - pad_b - 2, plot_w, 12,
                         Qt.AlignLeft,
                         "gold = POC · shaded = value area (70% of volume)")


# ------------------------------------------------------------- update threads

class UpdateCheckThread(QThread):
    """Runs the GitHub release check off the UI thread."""

    done = Signal(object, object)  # (update_dict | None, error | None)

    def __init__(self, current_version: str, parent=None) -> None:
        super().__init__(parent)
        self.current_version = current_version
        self.timeout = 8.0  # bounded so closing the app never hangs on it

    def run(self) -> None:  # noqa: D102 — QThread entry point
        try:
            update, error = check_for_update_live(
                self.current_version, timeout=self.timeout)
        except Exception as exc:  # pragma: no cover — defensive
            self.done.emit(None, f"update check failed: {exc!r}")
            return
        self.done.emit(update, error)


class DownloadThread(QThread):
    """Downloads + verifies the update zip off the UI thread."""

    done = Signal(object, object)  # (zip_path | None, error | None)
    progress = Signal(int, int)

    def __init__(self, update: dict, dest: Path, parent=None) -> None:
        super().__init__(parent)
        self.update = update
        self.dest = dest

    def run(self) -> None:  # noqa: D102 — QThread entry point
        try:
            expected = None
            if self.update.get("sha256_asset_url"):
                expected = fetch_expected_sha256(self.update["sha256_asset_url"])
            path = download_asset(
                self.update["asset_url"], self.dest,
                expected_sha256=expected,
                progress_cb=lambda done, total: self.progress.emit(done, total),
            )
        except Exception as exc:
            self.done.emit(None, str(exc))
            return
        self.done.emit(str(path), None)


class KeyTestThread(QThread):
    """Tests one API key off the UI thread (10s timeout, no retries)."""

    done = Signal(object, object)  # (ok: bool, message: str)

    def __init__(self, kind: str, key: str, endpoint: str = "", parent=None) -> None:
        super().__init__(parent)
        self.kind = kind
        self.key = key
        self.endpoint = endpoint

    def run(self) -> None:  # noqa: D102 — QThread entry point
        try:
            if self.kind == "finnhub":
                from goldboard_data.finnhub import FINNHUB_BASE
                from goldboard_data.base import ConnectorError
                conn = _QuickCheck()
                raw = conn.get_json(
                    f"{FINNHUB_BASE}/news-sentiment",
                    params={"symbol": "GLD", "token": self.key})
                if not isinstance(raw, dict) or "sentiment" not in raw:
                    raise ConnectorError("unexpected response shape")
                self.done.emit(True, "OK — Finnhub key works (GLD sentiment).")
            elif self.kind == "fred":
                from goldboard_data.fred import FRED_API_URL
                conn = _QuickCheck()
                raw = conn.get_json(
                    FRED_API_URL,
                    params={"series_id": "DFII10", "api_key": self.key,
                            "file_type": "json", "sort_order": "desc",
                            "limit": 1})
                obs = (raw or {}).get("observations") or []
                if not obs:
                    raise ValueError("no observations returned")
                self.done.emit(True, f"OK — FRED key works (DFII10 = {obs[0].get('value')}).")
            elif self.kind == "fedwatch":
                if not self.endpoint:
                    self.done.emit(False, "Set the endpoint URL first (from the CME API page).")
                    return
                conn = _QuickCheck()
                raw = conn.get_json(self.endpoint, params={"api_key": self.key})
                if not isinstance(raw, dict):
                    raise ValueError("endpoint did not return JSON")
                self.done.emit(True, "OK — FedWatch endpoint answered.")
            else:
                self.done.emit(False, f"unknown key kind: {self.kind}")
        except Exception as exc:
            self.done.emit(False, f"FAILED — {exc}")


class _QuickCheck:
    """Minimal one-shot GET helper for key tests (no connector state)."""

    def get_json(self, url: str, params: dict | None = None) -> object:
        import requests

        resp = requests.get(
            url, params=params, timeout=10,
            headers={"User-Agent": "Vanta/0.2 (key test)"})
        resp.raise_for_status()
        return resp.json()


class SettingsDialog(QDialog):
    """Settings page: API keys + live intervals.

    Keys are saved to the config file (never logged); a key that came
    from the environment is shown read-only. Test buttons verify the
    key against the real provider before saving.
    """

    def __init__(self, config, parent=None) -> None:
        super().__init__(parent)
        self.config = config
        self.setWindowTitle("Settings — API keys & live data")
        self.setMinimumWidth(560)
        self._test_thread: KeyTestThread | None = None
        self._test_target: str | None = None

        form = QFormLayout(self)

        self._finnhub_edit = QLineEdit(self)
        self._finnhub_edit.setEchoMode(QLineEdit.Password)
        self._finnhub_edit.setText(config.finnhub_api_key)
        self._finnhub_edit.setPlaceholderText("finnhub.io — free, 60 calls/min")
        self._finnhub_status = QLabel("Free key: finnhub.io → API Keys.", self)
        self._finnhub_status.setWordWrap(True)
        self._finnhub_test = QPushButton("Test", self)
        self._finnhub_test.clicked.connect(
            lambda: self._test_key("finnhub"))
        row = QHBoxLayout()
        row.addWidget(self._finnhub_edit)
        row.addWidget(self._finnhub_test)
        form.addRow("Finnhub key:", row)
        form.addRow("", self._finnhub_status)

        self._fred_edit = QLineEdit(self)
        self._fred_edit.setEchoMode(QLineEdit.Password)
        self._fred_edit.setText(config.fred_api_key)
        self._fred_edit.setPlaceholderText("fred.stlouisfed.org — free")
        self._fred_status = QLabel(
            "Free key: fred.stlouisfed.org → My Account → API Keys. "
            "Without it, macro data uses the slow fredgraph.csv fallback.", self)
        self._fred_status.setWordWrap(True)
        self._fred_test = QPushButton("Test", self)
        self._fred_test.clicked.connect(lambda: self._test_key("fred"))
        row = QHBoxLayout()
        row.addWidget(self._fred_edit)
        row.addWidget(self._fred_test)
        form.addRow("FRED key:", row)
        form.addRow("", self._fred_status)

        self._fedwatch_edit = QLineEdit(self)
        self._fedwatch_edit.setEchoMode(QLineEdit.Password)
        self._fedwatch_edit.setText(config.fedwatch_api_key)
        self._fedwatch_edit.setPlaceholderText("$25/mo CME EOD API — optional")
        self._fedwatch_url_edit = QLineEdit(self)
        self._fedwatch_url_edit.setText(config.fedwatch_api_url)
        self._fedwatch_url_edit.setPlaceholderText(
            "Endpoint URL from the CME FedWatch API page")
        self._fedwatch_status = QLabel(
            "Paid ($25/mo): cmegroup.com → Market Data API → FedWatch API. "
            "Without it the board uses the manual FOMC calendar.", self)
        self._fedwatch_status.setWordWrap(True)
        self._fedwatch_test = QPushButton("Test", self)
        self._fedwatch_test.clicked.connect(lambda: self._test_key("fedwatch"))
        row = QHBoxLayout()
        row.addWidget(self._fedwatch_edit)
        row.addWidget(self._fedwatch_test)
        form.addRow("FedWatch key:", row)
        form.addRow("FedWatch endpoint:", self._fedwatch_url_edit)
        form.addRow("", self._fedwatch_status)

        if config._finnhub_key_from_env:
            self._finnhub_edit.setReadOnly(True)
            self._finnhub_status.setText(
                "Finnhub key comes from the environment "
                "(GOLD_BOARD_FINNHUB_KEY) — change it there.")
        if config._fred_key_from_env:
            self._fred_edit.setReadOnly(True)
            self._fred_status.setText(
                "FRED key comes from the environment "
                "(GOLD_BOARD_FRED_KEY) — change it there.")
        if config._fedwatch_key_from_env:
            self._fedwatch_edit.setReadOnly(True)
            self._fedwatch_url_edit.setReadOnly(True)
            self._fedwatch_status.setText(
                "FedWatch key comes from the environment — change it there.")

        self._price_spin = QSpinBox(self)
        self._price_spin.setRange(5, 60)
        self._price_spin.setValue(config.price_refresh_seconds)
        self._price_spin.setSuffix(" s")
        form.addRow("Live price ticker:", self._price_spin)

        self._full_spin = QSpinBox(self)
        self._full_spin.setRange(5, 1440)
        self._full_spin.setValue(config.full_refresh_minutes)
        self._full_spin.setSuffix(" min")
        form.addRow("Full refresh (all sources):", self._full_spin)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Save | QDialogButtonBox.Cancel, self)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addWidget(buttons)

    def _test_key(self, kind: str) -> None:
        if self._test_thread is not None and self._test_thread.isRunning():
            return
        key = {
            "finnhub": self._finnhub_edit.text().strip(),
            "fred": self._fred_edit.text().strip(),
            "fedwatch": self._fedwatch_edit.text().strip(),
        }[kind]
        endpoint = self._fedwatch_url_edit.text().strip()
        status = {
            "finnhub": self._finnhub_status,
            "fred": self._fred_status,
            "fedwatch": self._fedwatch_status,
        }[kind]
        if not key:
            status.setText("Enter a key first.")
            return
        status.setText("Testing…")
        self._test_target = kind
        self._test_thread = KeyTestThread(kind, key, endpoint, self)
        self._test_thread.done.connect(self._on_test_done)
        self._test_thread.start()

    def _on_test_done(self, ok: bool, message: str) -> None:
        status = {
            "finnhub": self._finnhub_status,
            "fred": self._fred_status,
            "fedwatch": self._fedwatch_status,
        }.get(self._test_target or "")
        if status is not None:
            status.setText(message)

    def apply_to(self, config) -> None:
        """Copy the dialog values into the config (call before save)."""
        if not config._finnhub_key_from_env:
            config.finnhub_api_key = self._finnhub_edit.text().strip()
        if not config._fred_key_from_env:
            config.fred_api_key = self._fred_edit.text().strip()
        if not config._fedwatch_key_from_env:
            config.fedwatch_api_key = self._fedwatch_edit.text().strip()
        if not config._fedwatch_url_from_env:
            config.fedwatch_api_url = self._fedwatch_url_edit.text().strip()
        config.price_refresh_seconds = self._price_spin.value()
        config.full_refresh_minutes = self._full_spin.value()


# --------------------------------------------------------------- main window

class MainWindow(QMainWindow):
    """Vanta main window — one screen, ten panels, black + gold."""

    def __init__(self, db_path: str | Path | None = None, parent=None,
                 enable_live_ticker: bool = True) -> None:
        super().__init__(parent)
        self._enable_live_ticker = enable_live_ticker
        self.config = load_config()
        self.db_path = Path(db_path) if db_path else Path(self.config.db_path)
        self.settings = QSettings("Vanta", "GoldBoard")
        # One-time migration: v0.1.x kept the refresh interval in
        # QSettings; v0.2.0 keeps it in the config file. A config file
        # that predates the field keeps the QSettings value.
        try:
            import json as _json

            raw = _json.loads(get_config_path().read_text(encoding="utf-8")) \
                if get_config_path().is_file() else {}
            if "full_refresh_minutes" not in raw:
                qmin = self.settings.value("auto_refresh_minutes", None, type=int)
                if qmin:
                    self.config.full_refresh_minutes = int(qmin)
        except Exception:
            pass
        self._last_signal_direction: str | None = None
        self._last_total: float | None = None
        self._pending_update: dict | None = None
        self._worker: RefreshWorker | None = None
        self._ticker: LivePriceWorker | None = None
        self._backtest_worker: BacktestWorker | None = None
        self._update_thread: UpdateCheckThread | None = None
        self._download_thread: DownloadThread | None = None
        self._last_tick_value: float | None = None
        self._ticker_ok: bool | None = None
        self._chart_range: str = "5D"
        self._chart_bars_daily: list[dict] = []
        self._chart_bars_intraday: list[dict] = []
        self._live_tick: dict | None = None

        self.setWindowTitle(f"{APP_DISPLAY_NAME} — Gold Institutional Analysis Board")
        icon_path = get_app_icon_path()
        if icon_path is not None:
            self.setWindowIcon(QIcon(str(icon_path)))
        self.resize(1400, 950)
        self.setStyleSheet(self._stylesheet())

        self._build_header()
        self._build_panels()
        self._build_log_drawer()
        self.setStatusBar(QStatusBar(self))
        self.statusBar().showMessage("Ready — shadow mode only, no live trading.")

        # Live clock.
        self._clock_timer = QTimer(self)
        self._clock_timer.timeout.connect(self._tick_clock)
        self._clock_timer.start(1000)
        self._tick_clock()

        # Auto-refresh.
        self._refresh_timer = QTimer(self)
        self._refresh_timer.timeout.connect(self.start_refresh)
        self._apply_refresh_interval()

        # Tray icon (alerts surface here when available).
        self._tray: QSystemTrayIcon | None = None
        if QSystemTrayIcon.isSystemTrayAvailable() and icon_path is not None:
            self._tray = QSystemTrayIcon(QIcon(str(icon_path)), self)
            self._tray.show()

        # First paint from the local DB (fast, no network), then refresh.
        self._apply_payload(collect_dashboard_data(self.db_path, refresh=False))
        self.log("Vanta started — loaded from local DB; starting refresh.")
        # Startup timers are members (not static singleShot): closeEvent
        # stops them, so no refresh/update/ticker can start after the
        # window closes — a static singleShot would fire anyway and leave
        # an ownerless thread running (Windows CI exited 1 on exactly
        # that: orphaned live refresh after the tests had all passed).
        self._closed = False
        self._startup_refresh_timer = QTimer(self)
        self._startup_refresh_timer.setSingleShot(True)
        self._startup_refresh_timer.timeout.connect(self.start_refresh)
        self._startup_refresh_timer.start(500)
        self._startup_update_timer = QTimer(self)
        self._startup_update_timer.setSingleShot(True)
        self._startup_update_timer.timeout.connect(self.check_for_updates)
        self._startup_update_timer.start(2000)
        # Live price ticker (header price ticks every N seconds).
        self._startup_ticker_timer = QTimer(self)
        self._startup_ticker_timer.setSingleShot(True)
        self._startup_ticker_timer.timeout.connect(self._start_ticker)
        self._startup_ticker_timer.start(1000)

    # ------------------------------------------------------------------ style

    def _stylesheet(self) -> str:
        return f"""
        QMainWindow, QWidget {{ background-color: {PALETTE["black"]};
            color: {PALETTE["text"]}; font-size: 13px; }}
        QLabel {{ color: {PALETTE["text"]}; background: transparent; }}
        QLabel.muted {{ color: {PALETTE["muted"]}; }}
        QPushButton {{ background: {PALETTE["gold"]}; color: {PALETTE["black"]};
            border: none; border-radius: 4px; padding: 6px 14px; font-weight: bold; }}
        QPushButton:hover {{ background: {PALETTE["gold_light"]}; }}
        QPushButton:disabled {{ background: #3A3A3C; color: {PALETTE["muted"]}; }}
        QPushButton.ghost {{ background: transparent; color: {PALETTE["gold"]};
            border: 1px solid {PALETTE["gold"]}; }}
        QPushButton.ghost:hover {{ background: #2a2415; }}
        QPushButton.gold {{ background: {PALETTE["gold"]}; color: {PALETTE["black"]};
            border: none; }}
        QMenu {{ background: {PALETTE["surface"]}; color: {PALETTE["text"]};
            border: 1px solid {PALETTE["gold"]}; border-radius: 4px; padding: 4px; }}
        QMenu::item:selected {{ background: #2a2415; }}
        QGroupBox {{ border: 1px solid #3d3420; border-radius: 8px;
            margin-top: 12px; padding: 12px; background: {PALETTE["surface"]}; }}
        QGroupBox::title {{ color: {PALETTE["gold"]}; subcontrol-origin: margin;
            left: 8px; padding: 0 4px; font-weight: bold; }}
        QSpinBox {{ background: {PALETTE["surface"]}; color: {PALETTE["text"]};
            border: 1px solid {PALETTE["gold"]}; border-radius: 4px; padding: 3px; }}
        QLineEdit {{ background: {PALETTE["surface"]}; color: {PALETTE["text"]};
            border: 1px solid {PALETTE["gold"]}; border-radius: 4px; padding: 4px; }}
        QLineEdit:read-only {{ color: {PALETTE["muted"]}; }}
        QListWidget {{ background: {PALETTE["black"]};
            border: 1px solid {PALETTE["gold"]}; border-radius: 4px; }}
        QPlainTextEdit {{ background: {PALETTE["black"]};
            color: {PALETTE["text"]}; border: 1px solid {PALETTE["gold"]}; }}
        QScrollArea {{ border: none; }}
        """

    # ------------------------------------------------------------------ build

    def _build_header(self) -> None:
        header = QWidget(self)
        layout = QHBoxLayout(header)
        logo_path = resource_path("assets/logo/vanta-logo.png")
        if not logo_path.is_file():
            from goldboard_core.branding import get_app_logo_path
            logo_path = get_app_logo_path()
        # Logo in a visible tile: the PNG is a gold ring on black, so
        # on the near-black header it was invisible without a border.
        logo = QLabel(header)
        logo.setFixedSize(48, 48)
        logo.setStyleSheet(
            f"border: 1px solid {PALETTE['gold']}; border-radius: 10px; "
            "background-color: #0a0a0a; padding: 2px;")
        if logo_path is not None and Path(logo_path).is_file():
            pix = QPixmap(str(logo_path)).scaled(
                42, 42, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            logo.setPixmap(pix)
        else:
            logo.setText("◆")
            logo.setStyleSheet(
                f"color: {PALETTE['gold']}; font-size: 28px; "
                f"border: 1px solid {PALETTE['gold']}; border-radius: 10px;")
        logo.setAlignment(Qt.AlignCenter)
        layout.addWidget(logo)
        title = QLabel(f"{APP_DISPLAY_NAME}", header)
        title.setFont(_font(20, bold=True))
        title.setStyleSheet(f"color: {PALETTE['gold']};")
        layout.addWidget(title)
        version = QLabel(f"v{__version__} · shadow mode — no live trading", header)
        version.setProperty("class", "muted")
        layout.addWidget(version)
        layout.addStretch(1)
        self._live_dot = QLabel("● LIVE", header)
        self._live_dot.setFont(_font(11, bold=True))
        self._live_dot.setStyleSheet(f"color: {PALETTE['muted']};")
        layout.addWidget(self._live_dot)
        self._clock_label = QLabel(header)
        self._clock_label.setFont(_font(11))
        layout.addWidget(self._clock_label)
        self._settings_button = QPushButton("Settings", header)
        self._settings_button.setProperty("class", "ghost")
        self._settings_button.clicked.connect(self._open_settings)
        layout.addWidget(self._settings_button)
        # Update button is ALWAYS visible now: before v0.4.0 it only
        # appeared after a successful check found a newer release, so
        # a failed or pre-release check left the user with no button
        # at all. Clicking it with no pending update re-checks.
        self._update_button = QPushButton("Check for updates", header)
        self._update_button.setProperty("class", "ghost")
        self._update_button.clicked.connect(self._on_update_button)
        layout.addWidget(self._update_button)
        self._panels_button = QPushButton("Panels ▾", header)
        self._panels_button.setProperty("class", "ghost")
        self._panels_menu = QMenu(self)
        self._panels_button.setMenu(self._panels_menu)
        layout.addWidget(self._panels_button)
        self._alerts_check = QCheckBox("Signal alerts", header)
        self._alerts_check.setChecked(
            self.settings.value("alerts_enabled", True, type=bool))
        layout.addWidget(self._alerts_check)
        layout.addWidget(QLabel("Auto-refresh (min):", header))
        self._interval_spin = QSpinBox(header)
        self._interval_spin.setRange(5, 1440)
        self._interval_spin.setValue(self.config.full_refresh_minutes)
        self._interval_spin.valueChanged.connect(self._on_interval_changed)
        layout.addWidget(self._interval_spin)
        self._refresh_button = QPushButton("Refresh", header)
        self._refresh_button.clicked.connect(self.start_refresh)
        layout.addWidget(self._refresh_button)
        self._header = header

    def _panel(self, title: str) -> tuple[QGroupBox, QVBoxLayout]:
        box = QGroupBox(title, self)
        layout = QVBoxLayout(box)
        return box, layout

    # Panels the user can hide/show from the header "Panels" menu.
    # The choice is remembered in QSettings, so a hidden panel stays
    # hidden across restarts. Freshness is hidden by default — it is
    # the noisiest panel and the board stays usable without it.
    _PANEL_DEFAULTS = {"freshness": False}

    def _register_panel(self, key: str, box: QGroupBox,
                        wide: bool = False) -> None:
        visible = self.settings.value(
            f"panels/{key}",
            self._PANEL_DEFAULTS.get(key, True), type=bool)
        self._panels.append({"key": key, "box": box, "wide": wide})
        # Track visibility in a dict, not box.isVisible(): during
        # __init__ the window is not shown yet, so isVisible() is
        # False for every panel and the relayout would add nothing.
        self._panel_visible[key] = bool(visible)
        box.setVisible(bool(visible))

    def _relayout_panels(self) -> None:
        """Re-flow the visible panels in order, 3 per row."""
        grid = self._grid
        while grid.count():
            grid.takeAt(0)
        row, col = 0, 0
        for panel in self._panels:
            if not self._panel_visible.get(panel["key"], True):
                continue
            box = panel["box"]
            if panel["wide"]:
                if col:
                    row += 1
                    col = 0
                grid.addWidget(box, row, 0, 1, 3)
                row += 1
            else:
                grid.addWidget(box, row, col)
                col += 1
                if col == 3:
                    col = 0
                    row += 1

    def _build_panels_menu(self) -> None:
        self._panels_menu.clear()
        for panel in self._panels:
            action = QAction(panel["box"].title(), self)
            action.setCheckable(True)
            action.setChecked(self._panel_visible.get(panel["key"], True))
            key = panel["key"]
            action.triggered.connect(
                lambda checked=False, k=key: self._toggle_panel(k, checked))
            self._panels_menu.addAction(action)

    def _toggle_panel(self, key: str, visible: bool) -> None:
        for panel in self._panels:
            if panel["key"] == key:
                self.settings.setValue(f"panels/{key}", bool(visible))
                self._panel_visible[key] = bool(visible)
                panel["box"].setVisible(bool(visible))
                break
        self._relayout_panels()
        self._build_panels_menu()

    def _build_panels(self) -> None:
        self._panels: list[dict] = []
        self._panel_visible: dict[str, bool] = {}
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        content = QWidget(scroll)
        grid = QGridLayout(content)
        self._grid = grid

        # (1) Gold price + TradingView chart.
        box, layout = self._panel("Gold price (XAUUSD) — LIVE · TradingView")
        self._price_value = QLabel("—", content)
        self._price_value.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self._price_value.setFont(_font(22, bold=True))
        self._price_value.setStyleSheet(f"color: {PALETTE['gold_light']};")
        self._price_change = QLabel("—", content)
        self._price_change.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self._price_source = QLabel("No data yet.", content)
        self._price_source.setProperty("class", "muted")
        self._price_source.setWordWrap(True)
        self._price_source.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        # Real TradingView candlestick chart; the built-in chart is the
        # fallback when TradingView cannot load (offline / blocked).
        self._price_chart = PriceChart(content)
        self._tv_chart = TradingViewChart(self._price_chart, content)
        tv_note = QLabel(
            "TradingView chart (live candles) — if it stays blank, the "
            "built-in chart takes over automatically.", content)
        tv_note.setProperty("class", "muted")
        tv_note.setWordWrap(True)
        range_row = QHBoxLayout()
        self._range_buttons: dict[str, QPushButton] = {}
        for label in ("1D", "5D", "1M", "1Y", "2Y"):
            btn = QPushButton(label, content)
            btn.setProperty("class", "ghost")
            btn.setCheckable(True)
            btn.setChecked(label == self._chart_range)
            btn.clicked.connect(
                lambda _checked=False, r=label: self._set_chart_range(r))
            self._range_buttons[label] = btn
            range_row.addWidget(btn)
        range_row.addStretch(1)
        layout.addWidget(self._price_value)
        layout.addWidget(self._price_change)
        layout.addWidget(self._price_source)
        layout.addWidget(self._tv_chart)
        layout.addWidget(tv_note)
        layout.addLayout(range_row)
        layout.addStretch(1)
        self._register_panel("price", box)

        # (2) Total score gauge + components.
        box, layout = self._panel("Total score")
        self._gauge = ScoreGauge(content)
        self._score_value = QLabel("No score yet — no data.", content)
        self._score_value.setFont(_font(14, bold=True))
        self._components_list = QListWidget(content)
        self._components_list.setWordWrap(True)
        self._components_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._components_list.setMinimumHeight(120)
        layout.addWidget(self._gauge)
        layout.addWidget(self._score_value)
        layout.addWidget(self._components_list)
        self._register_panel("score", box)

        # (3) COT chart.
        box, layout = self._panel("COT — managed-money net (contracts)")
        self._cot_chart = CotChart(content)
        self._cot_summary = QLabel("No COT data yet.", content)
        self._cot_summary.setWordWrap(True)
        self._cot_summary.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        layout.addWidget(self._cot_chart)
        layout.addWidget(self._cot_summary)
        self._register_panel("cot", box)

        # (4) ETF flow.
        box, layout = self._panel("ETF flow — GLD / IAU")
        self._etf_label = QLabel("No ETF data yet.", content)
        self._etf_label.setWordWrap(True)
        self._etf_label.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        layout.addWidget(self._etf_label)
        layout.addStretch(1)
        self._register_panel("etf", box)

        # (5) Central-bank buying.
        box, layout = self._panel("Central-bank buying")
        self._cb_label = QLabel("No central-bank data yet.", content)
        self._cb_label.setWordWrap(True)
        note = QLabel(IMF_HOLDINGS_NOTE, content)
        note.setProperty("class", "muted")
        note.setWordWrap(True)
        note.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        layout.addWidget(self._cb_label)
        layout.addWidget(note)
        layout.addStretch(1)
        self._register_panel("central_bank", box)

        # (6) Macro.
        box, layout = self._panel("Macro — rates / USD / Fed")
        self._macro_label = QLabel("No macro data yet.", content)
        self._macro_label.setWordWrap(True)
        self._macro_label.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        layout.addWidget(self._macro_label)
        layout.addStretch(1)
        self._register_panel("macro", box)

        # (7) News / sentiment — live headlines (click to open).
        box, layout = self._panel("Live news & sentiment")
        self._news_label = QLabel("No news data yet.", content)
        self._news_label.setWordWrap(True)
        self._news_label.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self._news_list = QListWidget(content)
        self._news_list.setWordWrap(True)
        self._news_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._news_list.setMinimumHeight(140)
        self._news_list.itemClicked.connect(self._on_news_clicked)
        layout.addWidget(self._news_label)
        layout.addWidget(self._news_list)
        layout.addStretch(1)
        self._register_panel("news", box)

        # (8) Signals.
        box, layout = self._panel("Signals (advisory, shadow only)")
        self._signal_label = QLabel("No signal yet — no data.", content)
        self._signal_label.setWordWrap(True)
        self._signal_label.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self._signal_list = QListWidget(content)
        self._signal_list.setWordWrap(True)
        self._signal_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._signal_list.setMinimumHeight(120)
        layout.addWidget(self._signal_label)
        layout.addWidget(self._signal_list)
        self._register_panel("signals", box)

        # (9) Gates.
        box, layout = self._panel("Validation gates G0–G8")
        self._gates_banner = QLabel("NOT ready for Muuk integration", content)
        self._gates_banner.setWordWrap(True)
        self._gates_banner.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self._gates_banner.setFont(_font(12, bold=True))
        self._gates_list = QListWidget(content)
        self._gates_list.setWordWrap(True)
        self._gates_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._gates_list.setMinimumHeight(120)
        layout.addWidget(self._gates_banner)
        layout.addWidget(self._gates_list)
        self._register_panel("gates", box)

        # (10) Data freshness.
        box, layout = self._panel("Data freshness")
        self._freshness_list = QListWidget(content)
        self._freshness_list.setWordWrap(True)
        self._freshness_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._freshness_list.setMinimumHeight(150)
        layout.addWidget(self._freshness_list)
        self._register_panel("freshness", box, wide=True)

        # (11) History backtest.
        box, layout = self._panel("Backtest on history (real OHLC)")
        self._backtest_label = QLabel(
            "No history yet — refresh to fetch 2y daily + 5d intraday bars.",
            content)
        self._backtest_label.setWordWrap(True)
        self._backtest_label.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        bt_row = QHBoxLayout()
        self._backtest_button = QPushButton("Run backtest", content)
        self._backtest_button.clicked.connect(self._run_backtest)
        bt_row.addWidget(self._backtest_button)
        bt_row.addStretch(1)
        layout.addWidget(self._backtest_label)
        layout.addLayout(bt_row)
        layout.addStretch(1)
        self._register_panel("backtest", box, wide=True)

        # (12) Technical summary + setups — computed locally from
        # stored Yahoo history (free; Finnhub's aggregate endpoint
        # is premium-only). Advisory only, never a trade order.
        box, layout = self._panel("Technical summary & setups (advisory)")
        self._technical_label = QLabel(
            "No history yet — refresh to compute RSI / MACD / SMA / ADX.",
            content)
        self._technical_label.setWordWrap(True)
        self._technical_label.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self._setups_list = QListWidget(content)
        self._setups_list.setWordWrap(True)
        self._setups_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._setups_list.setMinimumHeight(120)
        layout.addWidget(self._technical_label)
        layout.addWidget(self._setups_list)
        layout.addStretch(1)
        self._register_panel("technical", box, wide=True)

        # (13) Volume Profile — where trading actually happened (free,
        # from stored intraday bars). True bid/ask footprint needs paid
        # CME tick data and is NOT claimed here.
        box, layout = self._panel("Volume profile — where trading happened")
        self._volume_widget = VolumeProfileWidget(content)
        self._volume_label = QLabel(
            "No volume profile yet — refresh to fetch intraday history.",
            content)
        self._volume_label.setWordWrap(True)
        self._volume_label.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        foot_note = QLabel(
            "From stored 15-minute bars. True bid/ask footprint needs "
            "paid CME tick data — this is volume-at-price, not footprint.",
            content)
        foot_note.setProperty("class", "muted")
        foot_note.setWordWrap(True)
        layout.addWidget(self._volume_widget)
        layout.addWidget(self._volume_label)
        layout.addWidget(foot_note)
        layout.addStretch(1)
        self._register_panel("volume", box)

        # (14) Macro event calendar — the Live Research Engine diary.
        box, layout = self._panel("Macro events — what is next (PCE / jobs / FOMC)")
        self._events_label = QLabel("No events in the calendar yet.", content)
        self._events_label.setWordWrap(True)
        self._events_label.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self._events_list = QListWidget(content)
        self._events_list.setWordWrap(True)
        self._events_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._events_list.setMinimumHeight(120)
        layout.addWidget(self._events_label)
        layout.addWidget(self._events_list)
        layout.addStretch(1)
        self._register_panel("events", box)

        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(2, 1)
        self._relayout_panels()
        self._build_panels_menu()
        content.setLayout(grid)
        scroll.setWidget(content)

        central = QWidget(self)
        central_layout = QVBoxLayout(central)
        central_layout.addWidget(self._header)
        central_layout.addWidget(scroll)
        central_layout.setContentsMargins(8, 4, 8, 4)
        self.setCentralWidget(central)

    def _build_log_drawer(self) -> None:
        dock = QDockWidget("Logs", self)
        dock.setAllowedAreas(Qt.BottomDockWidgetArea)
        self._log_view = QPlainTextEdit(dock)
        self._log_view.setReadOnly(True)
        self._log_view.setMaximumBlockCount(500)
        dock.setWidget(self._log_view)
        self.addDockWidget(Qt.BottomDockWidgetArea, dock)
        self._log_dock = dock
        # Keep the log drawer slim by default so all ten panels stay
        # visible; the user can drag it taller or close it.
        self.resizeDocks([dock], [130], Qt.Vertical)

    # ------------------------------------------------------------------ clock

    def _tick_clock(self) -> None:
        self._clock_label.setText(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    def log(self, message: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self._log_view.appendPlainText(f"[{stamp}] {message}")

    # ---------------------------------------------------------------- refresh

    def _on_interval_changed(self, minutes: int) -> None:
        self.config.full_refresh_minutes = minutes
        save_config(self.config)
        self._apply_refresh_interval()
        self.log(f"Auto-refresh set to every {minutes} min.")

    def _apply_refresh_interval(self) -> None:
        minutes = self.config.full_refresh_minutes
        self._refresh_timer.setInterval(max(1, minutes) * 60 * 1000)

    # ------------------------------------------------------------- settings

    def _open_settings(self) -> None:
        dialog = SettingsDialog(self.config, self)
        if dialog.exec() != QDialog.Accepted:
            return
        dialog.apply_to(self.config)
        save_config(self.config)
        self.config = load_config()
        self._interval_spin.setValue(self.config.full_refresh_minutes)
        self._apply_refresh_interval()
        self._restart_ticker()
        self.log("Settings saved.")
        self.statusBar().showMessage(
            "Settings saved — keys apply on the next refresh.")
        # Keys changed: refresh now so the new sources show up.
        self._startup_refresh_timer.start(500)

    # ------------------------------------------------------------ live ticker

    def _start_ticker(self) -> None:
        if not self._enable_live_ticker:
            return
        if self._ticker is not None and self._ticker.isRunning():
            return
        self._ticker = LivePriceWorker(self.config.price_refresh_seconds, self)
        self._ticker.price_updated.connect(self._on_live_price)
        self._ticker.failed.connect(self._on_live_price_failed)
        self._ticker.start()

    def _restart_ticker(self) -> None:
        if self._ticker is not None and self._ticker.isRunning():
            self._ticker.stop()
            self._ticker.wait(2000)
        self._ticker = None
        self._start_ticker()

    def _on_live_price(self, tick: dict) -> None:
        if self._ticker_ok is not True:
            self.log("Live ticker connected — price updating every "
                     f"{self.config.price_refresh_seconds}s.")
        self._ticker_ok = True
        self._live_dot.setText("● LIVE")
        self._live_dot.setStyleSheet(f"color: {STATUS_COLORS['ok']};")
        value = float(tick["value"])
        prev = self._last_tick_value
        self._last_tick_value = value
        self._live_tick = tick
        self._price_value.setText(_fmt_value(value, "USD/oz"))
        stamp = _fmt_time(tick.get("fetched_at"))
        source = tick.get("source") or "Yahoo Finance (GC=F)"
        kind = tick.get("kind") or "futures"
        self._price_source.setText(
            f"Source: {source} · {kind} · tick {stamp}"
            + (" · ⚠ sources disagree >1.5%" if tick.get("diverged") else ""))
        if prev is not None and prev > 0:
            change = value - prev
            pct = change / prev * 100.0
            sign = "+" if change >= 0 else ""
            color = STATUS_COLORS["ok"] if change >= 0 else STATUS_COLORS["failed"]
            self._price_change.setText(
                f"{sign}{change:,.2f} ({sign}{pct:.3f}%) since last tick")
            self._price_change.setStyleSheet(f"color: {color};")
        self._price_chart.set_live_tick(tick)

    def _on_live_price_failed(self, message: str) -> None:
        if self._ticker_ok is not False:
            self.log(f"Live ticker failed: {message} — keeping last price.")
        self._ticker_ok = False
        self._live_dot.setText("● LIVE — retrying")
        self._live_dot.setStyleSheet(f"color: {STATUS_COLORS['failed']};")

    # ------------------------------------------------------------- chart

    def _set_chart_range(self, range_label: str) -> None:
        self._chart_range = range_label
        for label, btn in self._range_buttons.items():
            btn.setChecked(label == range_label)
        self._refresh_chart()

    def _refresh_chart(self) -> None:
        if self._chart_range in ("1D", "5D"):
            bars = self._chart_bars_intraday
            if self._chart_range == "1D":
                bars = bars[-96:]
        else:
            count = {"1M": 22, "1Y": 250, "2Y": 500}[self._chart_range]
            bars = self._chart_bars_daily[-count:]
        self._price_chart.set_bars(bars, self._live_tick)

    # -------------------------------------------------------------- news

    def _on_news_clicked(self, item: QListWidgetItem) -> None:
        url = item.data(Qt.UserRole) or ""
        if url:
            QDesktopServices.openUrl(QUrl(url))

    # ----------------------------------------------------------- backtest

    def _run_backtest(self) -> None:
        if self._backtest_worker is not None and self._backtest_worker.isRunning():
            return
        self._backtest_button.setEnabled(False)
        self._backtest_label.setText("Running backtest on stored history…")
        self._backtest_worker = BacktestWorker(str(self.db_path), self)
        self._backtest_worker.finished.connect(self._on_backtest_finished)
        self._backtest_worker.failed.connect(self._on_backtest_failed)
        self._backtest_worker.start()

    @staticmethod
    def _bt_lines(title: str, res: dict) -> list[str]:
        if not res or res.get("n_trades", 0) == 0:
            note = res.get("note", "no trades") if isinstance(res, dict) else "no trades"
            return [f"{title}: no trades ({note})"]
        pf = res.get("profit_factor")
        pf_text = "∞" if pf == float("inf") else f"{pf:.2f}"
        return [
            f"{title}: {res['n_trades']} trades · "
            f"win {res['win_rate']:.0%} · total {res['total_r']:+.2f}R · "
            f"expectancy {res['expectancy_r']:+.2f}R · PF {pf_text} · "
            f"max DD {res['max_drawdown_r']:.2f}R",
        ]

    def _on_backtest_finished(self, result: dict) -> None:
        self._backtest_button.setEnabled(True)
        lines = [f"History: {result.get('bars_daily', 0)} daily bars (2y)."]
        lines += self._bt_lines("Board signals (live)", result.get("live", {}))
        lines += self._bt_lines("Replay (price-only)", result.get("replay", {}))
        folds = result.get("folds", [])
        if folds:
            fold_text = " · ".join(
                f"F{f['fold']}: {f['n_trades']} trades, "
                f"{f['total_r']:+.2f}R" for f in folds)
            lines.append(f"Walk-forward (3 folds): {fold_text}")
        lines.append(result.get("rule", ""))
        self._backtest_label.setText("\n".join(lines))
        self.log("Backtest finished: "
                 f"{result.get('replay', {}).get('n_trades', 0)} replay trades, "
                 f"{result.get('live', {}).get('n_trades', 0)} board-signal trades.")

    def _on_backtest_failed(self, message: str) -> None:
        self._backtest_button.setEnabled(True)
        self._backtest_label.setText(f"Backtest failed: {message}")
        self.log(f"ERROR: {message}")

    def start_refresh(self) -> None:
        if getattr(self, "_closed", False):
            return
        if self._worker is not None and self._worker.isRunning():
            return
        self._refresh_button.setEnabled(False)
        self.statusBar().showMessage("Refreshing…")
        self._worker = RefreshWorker(str(self.db_path), self)
        self._worker.finished.connect(self._on_refresh_finished)
        self._worker.failed.connect(self._on_refresh_failed)
        self._worker.start()

    def _on_refresh_finished(self, payload: dict) -> None:
        self._refresh_button.setEnabled(True)
        self._apply_payload(payload)
        for error in payload.get("errors", []):
            self.log(f"ERROR: {error}")
        self.statusBar().showMessage(
            f"Refreshed at {payload.get('refreshed_at', '')[:19]} "
            f"· shadow mode only, no live trading.")
        self.log("Refresh finished.")

    def _on_refresh_failed(self, message: str) -> None:
        self._refresh_button.setEnabled(True)
        self.statusBar().showMessage(f"Refresh failed: {message}")
        self.log(f"ERROR: {message}")

    # ----------------------------------------------------------------- payload

    def _apply_payload(self, payload: dict) -> None:
        snaps = payload.get("snapshots", {})

        # (1) Price.
        price = payload.get("price")
        if price is not None:
            self._price_value.setText(_fmt_value(price.value, price.unit))
            history = payload.get("price_history", [])
            if len(history) >= 2:
                prev = history[-2]["value"]
                change = price.value - prev
                pct = (change / prev * 100.0) if prev else 0.0
                sign = "+" if change >= 0 else ""
                self._price_change.setText(
                    f"{sign}{change:,.2f} ({sign}{pct:.2f}%) vs previous stored quote")
            else:
                self._price_change.setText("Change: — (only one stored quote)")
            self._price_source.setText(_snapshot_line(price, "Source"))
        else:
            self._price_value.setText("—")
            self._price_change.setText("Change: —")
            self._price_source.setText(
                "No price yet — refresh to fetch the Yahoo quote (proxy).")
        # Live chart from stored OHLC bars.
        self._chart_bars_daily = payload.get("price_bars_daily", []) or []
        self._chart_bars_intraday = payload.get("price_bars_intraday", []) or []
        self._refresh_chart()

        # (2) Score + components.
        score = payload.get("score")
        self._components_list.clear()
        if score is not None:
            self._gauge.set_value(score.total)
            self._score_value.setText(f"Total: {score.total:+.1f} / ±100")
            for comp in score.components:
                item = QListWidgetItem(
                    f"{comp.name}: {comp.score:+.0f} "
                    f"(weight {comp.weight:.0%}) — {comp.reason}")
                self._components_list.addItem(item)
            for reason in score.reasons:
                self._components_list.addItem(QListWidgetItem(f"• {reason}"))
        else:
            self._gauge.set_value(None)
            self._score_value.setText("No score yet — no data.")
            self._components_list.addItem(
                QListWidgetItem("No pillars have data yet."))

        # (3) COT chart.
        cot_history = payload.get("cot_history", [])
        self._cot_chart.set_points(cot_history)
        mm_net = snaps.get("cot_gold_mm_net")
        mm_pct = snaps.get("cot_gold_mm_net_pct_oi")
        pctile = snaps.get("cot_managed_money_net_pctile_52w")
        lines = []
        if mm_net is not None:
            lines.append(_snapshot_line(mm_net, "Managed-money net"))
        if mm_pct is not None:
            lines.append(_snapshot_line(mm_pct, "Net as % of OI"))
        if pctile is not None:
            lines.append(_snapshot_line(pctile, "52-week percentile"))
        elif cot_history:
            values = sorted(p["value"] for p in cot_history)
            latest = cot_history[-1]["value"]
            rank = sum(1 for v in values if v <= latest) / len(values) * 100.0
            lines.append(f"52-week percentile (from stored history): {rank:.0f}%")
        self._cot_summary.setText("\n".join(lines) if lines else "No COT data yet.")

        # (4) ETF flow.
        etf_lines = [
            _snapshot_line(snaps.get("etf_gld_tonnes"), "GLD holdings"),
            _snapshot_line(snaps.get("etf_gld_daily_change_tonnes"), "GLD daily change"),
            _snapshot_line(snaps.get("etf_iau_tonnes"), "IAU holdings"),
            _snapshot_line(snaps.get("etf_iau_daily_change_tonnes"), "IAU daily change"),
        ]
        self._etf_label.setText("\n".join(etf_lines))

        # (5) Central-bank buying.
        cb_lines = [
            _snapshot_line(snaps.get("wgc_cb_net_tonnes"), "Net buying (latest)"),
            _snapshot_line(snaps.get("central_bank_net_tonnes_3m"), "Net buying (3m)"),
        ]
        self._cb_label.setText("\n".join(cb_lines))

        # (6) Macro.
        macro_lines = [
            _snapshot_line(snaps.get("fred_dfii10"), "US 10y real yield (DFII10)"),
            _snapshot_line(snaps.get("fred_dtwexbgs"), "Broad USD index (DTWEXBGS)"),
            _snapshot_line(snaps.get("fred_dgs10"), "US 10y nominal (DGS10)"),
            _snapshot_line(snaps.get("fred_sofr"), "SOFR"),
        ]
        if not self.config.fred_api_key:
            macro_lines.append(
                "FRED: no key set — macro data uses the slow fredgraph.csv "
                "fallback; a free key (GOLD_BOARD_FRED_KEY, "
                "fred.stlouisfed.org) makes it fast.")
        try:
            calendar = load_fomc_calendar()
            target = calendar.get("fed_funds_target", "—")
            macro_lines.append(f"Fed funds target: {target} (manual calendar)")
            upcoming = next_meetings()
            if upcoming:
                nxt = upcoming[0]
                macro_lines.append(
                    f"Next FOMC: {nxt.get('start')} – {nxt.get('end')}")
            else:
                macro_lines.append("Next FOMC: none in calendar")
        except Exception as exc:
            macro_lines.append(f"FOMC calendar unavailable: {exc}")
        self._macro_label.setText("\n".join(macro_lines))

        # (7) News / sentiment — live headlines (click to open).
        news_lines = [
            _snapshot_line(snaps.get("gdelt_gold_tone_latest"), "GDELT gold tone"),
            _snapshot_line(snaps.get("gdelt_gold_tone_7d_avg"), "GDELT tone 7d avg"),
            _snapshot_line(snaps.get("gdelt_gold_mentions_latest"), "GDELT mentions"),
        ]
        if self.config.finnhub_api_key:
            news_lines.append(_snapshot_line(
                snaps.get("finnhub_gld_bullish_pct"), "Finnhub GLD bullish %"))
            news_lines.append(_snapshot_line(
                snaps.get("finnhub_gold_news_count"), "Finnhub gold news count"))
        else:
            news_lines.append(
                "Finnhub: key required — set it in Settings to enable.")
        self._news_label.setText("\n".join(news_lines))
        self._news_list.clear()
        for item in payload.get("news", [])[:30]:
            stamp = _fmt_time(item.get("published_at"))
            text = (f"[{item.get('provider', '')}] {item.get('title', '')} "
                    f"— {item.get('source', '')} · {stamp}")
            list_item = QListWidgetItem(text)
            list_item.setData(Qt.UserRole, item.get("url") or "")
            self._news_list.addItem(list_item)
        if not payload.get("news"):
            self._news_list.addItem(QListWidgetItem(
                "No headlines yet — refresh to fetch GDELT news."))

        # (8) Signals.
        signal = payload.get("signal")
        if signal is not None:
            direction = signal.direction.upper()
            self._signal_label.setText(
                f"Latest: {direction} @ {_fmt_value(signal.entry, 'USD/oz')} · "
                f"SL {_fmt_value(signal.sl)} · TP {_fmt_value(signal.tp)} · "
                f"confidence {signal.confidence:.0%} · "
                f"{_fmt_time(signal.generated_at)}\n{signal.reason}")
        else:
            self._signal_label.setText("No signal yet — no data.")
        self._signal_list.clear()
        for past in payload.get("signals_history", [])[:20]:
            self._signal_list.addItem(QListWidgetItem(
                f"{_fmt_time(past.generated_at)} — {past.direction.upper()} @ "
                f"{_fmt_value(past.entry)} · {past.status} · {past.reason}"))

        # (9) Gates.
        gates = payload.get("gates", [])
        self._gates_list.clear()
        for gate in gates:
            mark = "PASS" if gate.passed else "FAIL"
            threshold = GATE_THRESHOLDS.get(gate.name, gate.threshold or "")
            self._gates_list.addItem(QListWidgetItem(
                f"{gate.name} [{mark}] — {gate.detail} ({threshold})"))
        ready = payload.get("ready_for_muuk", False)
        if ready:
            self._gates_banner.setText("Ready for Muuk integration")
            self._gates_banner.setStyleSheet(f"color: {STATUS_COLORS['ok']};")
        else:
            self._gates_banner.setText("NOT ready for Muuk integration")
            self._gates_banner.setStyleSheet(f"color: {STATUS_COLORS['failed']};")

        # (10) Freshness.
        self._freshness_list.clear()
        freshness = payload.get("freshness", {})
        for source in FRESHNESS_SOURCES:
            entry = freshness.get(source, {})
            status = str(entry.get("status") or "failed").lower()
            color = STATUS_COLORS.get(status, STATUS_COLORS["failed"])
            stamp = _fmt_time(entry.get("fetched_at"))
            error = entry.get("error")
            text = f"{source}: {status.upper()} · last fetch {stamp}"
            if error:
                text += f" · {error}"
            item = QListWidgetItem(text)
            item.setForeground(QColor(color))
            self._freshness_list.addItem(item)

        self._check_alerts(payload)

        # (11) Backtest — history coverage line (results arrive on demand).
        daily = len(self._chart_bars_daily)
        intraday = len(self._chart_bars_intraday)
        self._backtest_label.setText(
            f"History stored: {daily} daily bars (2y) · {intraday} intraday "
            f"bars (5d, 15m). Press “Run backtest” to evaluate the board's "
            f"signals and the price-only replay on the daily bars.")

        # (12) Technical summary & setups — local, free, advisory.
        tech = payload.get("technical") or {}
        if tech and tech.get("bars_daily"):
            lines = [
                f"Aggregate: {str(tech.get('signal', 'neutral')).upper()} — "
                f"{tech.get('buy', 0)} buy / {tech.get('neutral', 0)} neutral "
                f"/ {tech.get('sell', 0)} sell signals "
                f"({tech.get('bars_daily')} daily bars)",
                f"Regime: {tech.get('regime', '—')}",
            ]
            if tech.get("rsi14") is not None:
                lines.append(f"RSI(14): {tech['rsi14']:.1f}")
            if tech.get("macd") is not None and tech.get("macd_signal") is not None:
                lines.append(
                    f"MACD: {tech['macd']:.2f} vs signal "
                    f"{tech['macd_signal']:.2f} "
                    f"(hist {tech.get('macd_hist') or 0:.2f})")
            if tech.get("sma20") is not None:
                lines.append(
                    f"SMA20 {tech['sma20']:,.2f} · SMA50 "
                    f"{(tech.get('sma50') or 0):,.2f} · SMA200 "
                    f"{(tech.get('sma200') or 0):,.2f}")
            if tech.get("atr14") is not None:
                lines.append(f"ATR(14): {tech['atr14']:,.2f} USD/oz")
            # Regime filter (v0.5.0): ER + CHOP + ADX, three methods.
            rf = tech.get("regime_filter") or {}
            if rf.get("label") and rf["label"] != "unknown":
                er_t = f"{rf['er']:.2f}" if rf.get("er") is not None else "—"
                chop_t = (f"{rf['chop']:.1f}"
                          if rf.get("chop") is not None else "—")
                pct_t = (f"{rf['atr_percentile']:.0f}%"
                         if rf.get("atr_percentile") is not None else "—")
                lines.append(
                    f"Regime filter: {str(rf['label']).upper()} — "
                    f"ER {er_t} · CHOP {chop_t} · ATR percentile {pct_t}")
                for reason in rf.get("reasons", []):
                    lines.append(f"   • {reason}")
                lines.append(
                    "Spread rule: not measurable from Yahoo bars (no "
                    "spread in them); news window is covered by the event "
                    "caution setup above.")
            lines.append(
                "Computed locally from stored Yahoo history — the "
                "Finnhub aggregate endpoint is premium-only, this is "
                "the free equivalent. Advisory only, NOT a trade order.")
            self._technical_label.setText("\n".join(lines))
        else:
            self._technical_label.setText(
                "No history yet — refresh to compute RSI / MACD / SMA / ADX.")
        self._setups_list.clear()
        for setup in payload.get("setups", []):
            header = (f"{setup.get('name', '')} — "
                      f"{str(setup.get('direction', 'none')).upper()} · "
                      f"{setup.get('confidence', '')} confidence")
            self._setups_list.addItem(QListWidgetItem(header))
            for reason in setup.get("reasons", []):
                self._setups_list.addItem(QListWidgetItem(f"   • {reason}"))
        if not payload.get("setups"):
            self._setups_list.addItem(QListWidgetItem(
                "No setups yet — refresh to evaluate the rules."))
        # Volume profile + macro event calendar.
        profile = payload.get("volume_profile") or {}
        self._volume_widget.set_profile(profile)
        if profile.get("complete"):
            last = self._live_tick.get("value") if self._live_tick else None
            if last is None and payload.get("price_bars_daily"):
                last = payload["price_bars_daily"][-1].get("close")
            where = "—"
            if last is not None:
                if last > profile["value_area_high"]:
                    where = "price ABOVE value area (accepted higher)"
                elif last < profile["value_area_low"]:
                    where = "price BELOW value area (accepted lower)"
                else:
                    where = "price INSIDE value area (balanced)"
            self._volume_label.setText(
                f"POC (most traded price): "
                f"{profile['poc']:,.2f} USD/oz\n"
                f"Value area (70% of volume): "
                f"{profile['value_area_low']:,.2f} – "
                f"{profile['value_area_high']:,.2f}\n"
                f"{where}")
        else:
            self._volume_label.setText(
                profile.get("reason") or
                "No volume profile yet — refresh to fetch intraday history.")
        events = payload.get("events") or []
        self._events_list.clear()
        if events:
            nxt = events[0]
            self._events_label.setText(
                f"Next: {nxt.get('name', '')} — "
                f"{nxt.get('days_away', '?')} day(s) away "
                f"({nxt.get('date', '')}).")
            for event in events:
                days = event.get("days_away")
                when = (f"in {days} day{'s' if days != 1 else ''}"
                        if days is not None else str(event.get("date", "")))
                self._events_list.addItem(QListWidgetItem(
                    f"{event.get('name', '')} — {when}"))
                if event.get("impact"):
                    self._events_list.addItem(QListWidgetItem(
                        f"   • {event['impact']}"))
        else:
            self._events_label.setText(
                "No events in the calendar yet — the bundled list may be "
                "out of date; check bea.gov / bls.gov / federalreserve.gov.")

    # ------------------------------------------------------------------ alerts

    def _check_alerts(self, payload: dict) -> None:
        if not self._alerts_check.isChecked():
            return
        signal = payload.get("signal")
        score = payload.get("score")
        messages: list[str] = []
        if signal is not None and signal.direction != "none":
            if self._last_signal_direction != signal.direction:
                messages.append(
                    f"ALERT: new {signal.direction.upper()} signal @ "
                    f"{_fmt_value(signal.entry, 'USD/oz')} "
                    f"(confidence {signal.confidence:.0%})")
            self._last_signal_direction = signal.direction
        if score is not None:
            threshold = 40.0
            if (self._last_total is None or abs(self._last_total) < threshold) \
                    and abs(score.total) >= threshold:
                messages.append(
                    f"ALERT: total score crossed ±{threshold:.0f} "
                    f"(now {score.total:+.1f})")
            self._last_total = score.total
        for message in messages:
            self.log(message)
            self.statusBar().showMessage(message)
            if self._tray is not None:
                self._tray.showMessage(
                    APP_DISPLAY_NAME, message,
                    QSystemTrayIcon.Information, 8000)

    # ----------------------------------------------------------------- updater

    def _set_update_button_style(self, style_class: str) -> None:
        """Change the update button's style class (needs a repolish)."""
        self._update_button.setProperty("class", style_class)
        self._update_button.style().unpolish(self._update_button)
        self._update_button.style().polish(self._update_button)

    def check_for_updates(self) -> None:
        if self._update_thread is not None and self._update_thread.isRunning():
            return
        self._update_button.setText("Checking…")
        self._update_button.setEnabled(False)
        self._update_thread = UpdateCheckThread(__version__, self)
        self._update_thread.done.connect(self._on_update_checked)
        self._update_thread.start()

    def _on_update_checked(self, update: dict | None, error: str | None) -> None:
        self._update_button.setEnabled(True)
        self._set_update_button_style("ghost")
        if error is not None:
            if NO_CHANNEL_MESSAGE in error:
                self.log("Updater: no release channel yet "
                         "(GitHub repo hamxa8769/vanta not created).")
                self.statusBar().showMessage(
                    "No release channel yet — repo hamxa8769/vanta not created.")
            else:
                self.log(f"Updater: {error}")
                self.statusBar().showMessage(
                    "Update check failed — click the button to retry.")
            self._update_button.setText("Check for updates")
            return
        if update is None:
            self.log("Updater: no newer release available.")
            self._update_button.setText("✓ Up to date")
            self.statusBar().showMessage(
                f"Vanta v{__version__} is the latest release.")
            return
        self._pending_update = update
        self._update_button.setText(f"⬆ Update available: v{update['version']}")
        self._set_update_button_style("gold")
        self.log(f"Updater: v{update['version']} available "
                 f"({update.get('html_url') or ''}).")
        self.statusBar().showMessage(
            f"Update available: v{update['version']} — click the header button.")

    def _on_update_button(self) -> None:
        update = self._pending_update
        if update is None:
            # No update pending: the button is a manual re-check.
            self.check_for_updates()
            return
        if not update.get("asset_url"):
            QMessageBox.information(
                self, "Update",
                f"v{update['version']} is published but has no Windows "
                f"download attached yet. See {update.get('html_url') or ''}.")
            return
        answer = QMessageBox.question(
            self, "Update available",
            f"Download and install Vanta v{update['version']}?\n"
            "Your config is preserved (it lives outside the install folder).")
        if answer != QMessageBox.Yes:
            return
        self._update_button.setEnabled(False)
        self._update_button.setText("Downloading update…")
        dest = Path.home() / "Downloads" / (update.get("asset_name") or "Vanta-update.zip")
        self._download_thread = DownloadThread(update, dest, self)
        self._download_thread.progress.connect(self._on_download_progress)
        self._download_thread.done.connect(self._on_download_done)
        self._download_thread.start()

    def _on_download_progress(self, done: int, total: int) -> None:
        if total:
            self.statusBar().showMessage(
                f"Downloading update… {done / 1024 / 1024:.1f} / "
                f"{total / 1024 / 1024:.1f} MB")

    def _on_download_done(self, zip_path: str | None, error: str | None) -> None:
        self._update_button.setEnabled(True)
        if error is not None or zip_path is None:
            self.log(f"Updater: download failed — {error}")
            self.statusBar().showMessage(f"Update download failed: {error}")
            self._update_button.setText("Update available")
            return
        answer = QMessageBox.question(
            self, "Update downloaded",
            "The update is downloaded and verified. Install now?\n"
            "Vanta will close and restart on the new version.")
        if answer != QMessageBox.Yes:
            self._update_button.setText("Update available")
            return
        try:
            install_update(zip_path)
        except Exception as exc:
            QMessageBox.warning(self, "Update failed", str(exc))
            self.log(f"Updater: install failed — {exc}")
            return
        self.log("Updater: installing — closing Vanta for the update.")
        # Close (not just quit): closeEvent waits for background threads
        # so no QThread is destroyed while still running.
        self.close()
        QApplication.quit()

    # ------------------------------------------------------------------- close

    def closeEvent(self, event) -> None:  # noqa: D102 — Qt close hook
        self._closed = True
        self._clock_timer.stop()
        self._refresh_timer.stop()
        # Stop the startup timers first: a pending refresh/update/ticker
        # must never start after close (ownerless background thread).
        for timer in (self._startup_refresh_timer,
                      self._startup_update_timer,
                      self._startup_ticker_timer):
            timer.stop()
        if self._ticker is not None and self._ticker.isRunning():
            self._ticker.stop()
            self._ticker.wait(2000)
        # Wait for background threads: destroying a running QThread aborts
        # the process, so bound the waits (update check timeout is 8s).
        # The refresh worker has no cooperative stop flag (its run() is a
        # straight registry -> score -> signal pass), so it gets the
        # longest wait; terminate() is the last resort at shutdown only —
        # its DB writes are single atomic SQLite transactions.
        for thread, wait_ms in (
            (self._worker, 15000),
            (self._backtest_worker, 15000),
            (self._update_thread, 9000),
            (self._download_thread, 3000),
        ):
            if thread is not None and thread.isRunning():
                thread.quit()
                if not thread.wait(wait_ms):
                    # Last resort: a stuck download is idempotent (the
                    # partial file is simply left behind), and a stuck
                    # refresh only loses one score/gate write, so it is
                    # safe to terminate rather than abort the process.
                    thread.terminate()
                    thread.wait(1000)
        super().closeEvent(event)
