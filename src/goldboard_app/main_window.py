"""Vanta desktop UI (Phase 4) — PySide6/Qt, black + gold, shadow-only.

One main screen: a summary strip (the whole analysis at a glance),
a small chart preview (click to expand into a centred window), and
the lower panels organised into tabs (Institutions, Macro & News,
Technical, Signals & Gates, System) — a live price ticker (Yahoo
quote every 10s), a live clock, Refresh + auto-refresh on a QTimer,
refresh in a QThread (never blocks the UI), a Settings form in the
System tab for API keys, an in-app updater, threshold alerts, a log
drawer, and a dark/light theme toggle.

Honesty rule: every number shows source + timestamp. A failed source
shows FAILED/STALE — never an invented value. No live trading anywhere.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

from PySide6.QtCore import (
    QEvent,
    QSettings,
    Qt,
    QThread,
    QTimer,
    Signal,
    QUrl,
)
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
    QDockWidget,
    QFormLayout,
    QFrame,
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
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QStatusBar,
    QSystemTrayIcon,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from goldboard_core.backtest import backtest
from goldboard_core.branding import (
    APP_DISPLAY_NAME,
    LOGO_DIR,
    PALETTE,
    get_app_icon_path,
    set_theme,
)
from goldboard_core.config import get_config_path, load_config, save_config
from goldboard_core.gates import GATE_THRESHOLDS
from goldboard_data.fedwatch import load_fomc_calendar, next_meetings

from . import __version__
from .tradingview_chart import CHART_MIN_HEIGHT, PREVIEW_MIN_HEIGHT, TradingViewChart
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


def _as_aware(value) -> datetime | None:
    """A datetime as an aware UTC datetime (naive -> assume UTC)."""
    if value is None:
        return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


# Sort floor for signals without a timestamp (they sort last).
_MIN_DATETIME = datetime.min.replace(tzinfo=timezone.utc)


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


# Small rounded status chips (v0.6.0): regime + technical signal get
# a coloured pill instead of a plain word, in the summary strip and
# in the Technical Summary panel. One helper, reused in both places.
PILL_COLORS = {
    "green": ("#2E9E5B", "#FFFFFF"),
    "amber": ("#C77E2A", "#FFFFFF"),
    "red": ("#C0392B", "#FFFFFF"),
    "grey": ("#3A3A3C", "#F2EDE3"),
    "gold": ("#C9A227", "#0B0B0C"),
}

REGIME_PILL_KIND = {"trending": "green", "choppy": "amber",
                    "high-risk": "red", "unclear": "grey"}
SIGNAL_PILL_KIND = {"buy": "green", "strong buy": "green",
                    "sell": "red", "strong sell": "red",
                    "neutral": "grey"}


def _status_pill(text: str, kind: str = "grey") -> QLabel:
    """A small rounded chip label; restyle with _set_pill()."""
    label = QLabel(text)
    label.setAlignment(Qt.AlignCenter)
    _set_pill(label, text, kind)
    return label


def _set_pill(label: QLabel, text: str, kind: str = "grey") -> None:
    bg, fg = PILL_COLORS.get(kind, PILL_COLORS["grey"])
    label.setText(text)
    label.setStyleSheet(
        f"background-color: {bg}; color: {fg}; border-radius: 10px; "
        "padding: 3px 12px; font-weight: bold; font-size: 12px;")


def _score_verdict(total: float) -> str:
    """Display-only word for the institutional total (no logic change)."""
    if total >= 40:
        return "Strongly bullish"
    if total >= 15:
        return "Bullish"
    if total <= -40:
        return "Strongly bearish"
    if total <= -15:
        return "Bearish"
    return "Neutral"


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
        painter.setBrush(QColor(PALETTE["track"]))
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

    The bar-index/price -> pixel mapping lives in ``_geometry()``
    + ``xy()`` so SignalChart (below) draws its markers with the
    exact same mapping — a marker sits on the right bar and the
    right price, never an approximation of it.
    """

    # Plot-area padding (px): top leaves room for the high/low
    # label, bottom for the bars label.
    PAD_L, PAD_R, PAD_T, PAD_B = 8, 8, 30, 22

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

    def _geometry(self) -> dict | None:
        """Plot geometry from the current bars + widget size.

        None when there is nothing to plot. ``closes`` includes the
        live tick as the last point (as the paint does); ``lo`` /
        ``hi`` / ``span`` cover exactly those closes — a subclass
        may widen the range (SignalChart adds its signal levels).
        """
        bars = self._bars
        closes = [float(b["close"]) for b in bars]
        if self._live_tick is not None:
            closes.append(float(self._live_tick["value"]))
        if not closes:
            return None
        lo, hi = min(closes), max(closes)
        span = (hi - lo) or 1.0
        width, height = self.width(), self.height()
        return {
            "width": width, "height": height, "closes": closes,
            "lo": lo, "hi": hi, "span": span, "n": len(closes),
            "pad_l": self.PAD_L, "pad_r": self.PAD_R,
            "pad_t": self.PAD_T, "pad_b": self.PAD_B,
            "plot_w": width - self.PAD_L - self.PAD_R,
            "plot_h": height - self.PAD_T - self.PAD_B,
        }

    def xy(self, i: int, value: float,
           geo: dict | None = None) -> tuple[float, float]:
        """Pixel position of bar ``i`` at price ``value``."""
        geo = geo if geo is not None else self._geometry()
        if geo is None:
            return 0.0, 0.0
        x = geo["pad_l"] + (geo["plot_w"] * i / max(1, geo["n"] - 1))
        y = geo["pad_t"] + geo["plot_h"] * (1.0 - (value - geo["lo"]) / geo["span"])
        return x, y

    def bar_index_for_time(self, when) -> int | None:
        """Index of the bar in effect at ``when`` (last bar with
        ts <= when). None when ``when`` is None or before the first
        bar — a signal fired before the visible window is not
        plotted on a bar it never touched."""
        if when is None or not self._bars:
            return None
        when = _as_aware(when)
        if when is None:
            return None
        idx = None
        for i, bar in enumerate(self._bars):
            ts = bar.get("ts")
            if ts is None:
                continue
            ts = _as_aware(ts)
            if ts is None:
                continue
            if ts <= when:
                idx = i
            else:
                break
        return idx

    def paintEvent(self, event) -> None:  # noqa: D102 — Qt paint hook
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        self._paint_base(painter)

    def _paint_base(self, painter: QPainter) -> dict | None:
        """Draw the gold close line + labels; returns the geometry
        used (None when nothing to plot). SignalChart reuses this
        and then paints its markers with the same geometry."""
        width, height = self.width(), self.height()
        # Explicit background: the fallback chart must be light in the
        # light theme (and match TradingView's dark canvas in dark).
        # A plain QWidget does not paint its own background reliably
        # from the stylesheet alone, so fill it here.
        painter.fillRect(0, 0, width, height, QColor(PALETTE["black"]))
        geo = self._geometry()
        if geo is None or (len(self._bars) < 2
                           and self._live_tick is None):
            painter.setPen(QColor(PALETTE["muted"]))
            painter.drawText(0, 0, width, height, Qt.AlignCenter,
                             "No price history yet — refresh to fetch it.")
            return None
        closes = geo["closes"]
        hi, lo = geo["hi"], geo["lo"]
        bars = self._bars
        n = geo["n"]
        # Gold line through the closes.
        path = QPainterPath()
        x0, y0 = self.xy(0, closes[0], geo)
        path.moveTo(x0, y0)
        for i, v in enumerate(closes[1:], start=1):
            x, y = self.xy(i, v, geo)
            path.lineTo(x, y)
        painter.setPen(QPen(QColor(PALETTE["gold"]), 2))
        painter.drawPath(path)
        # Last stored point + live tick dot.
        lx, ly = self.xy(n - 1, closes[-1], geo)
        painter.setBrush(QColor(PALETTE["gold_light"]))
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(int(lx) - 4, int(ly) - 4, 8, 8)
        painter.setPen(QColor(PALETTE["muted"]))
        painter.setFont(_font(9))
        interval = bars[-1].get("interval", "1d") if bars else "—"
        painter.drawText(self.PAD_L, 4, 300, 12, Qt.AlignLeft,
                         f"high {hi:,.2f}  ·  low {lo:,.2f}")
        painter.drawText(self.PAD_L, height - 8, 300, 12, Qt.AlignLeft,
                         f"{len(bars)} bars ({interval})"
                         + ("  ·  LIVE" if self._live_tick else ""))
        return geo


class SignalChart(PriceChart):
    """Built-in chart with Vanta's own signals plotted on it (v0.8.0).

    The TradingView embed is a cross-origin iframe — NO marker or
    drawing API exists for it. Verified against the embed script
    served by s3.tradingview.com: its only messages are
    iframe-resize and open-in-popup, and its settings list has no
    marker / drawing / annotation parameter (the drawings API
    belongs to the paid Charting Library, not the free widget). So
    accuracy checking happens here, on the built-in chart, where
    every pixel is ours: a buy/sell marker at the signal's bar +
    entry price, dashed entry / SL (red) / TP (green) lines, and
    the outcome for closed signals (TP hit / SL hit / expired,
    with the R multiple) from the existing backtest engine. Signals
    are advisory / shadow-only, and the status line says so.
    """

    # Latest N signals in the visible range are drawn — more would
    # be spaghetti. The status line says how many were plotted.
    MAX_PLOTTED = 10

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        # Entries are (Signal, TradeResult | None): the outcome comes
        # from goldboard_core.backtest on the daily bars; None means
        # the signal is still open (no outcome yet — never invented).
        self._signals: list[tuple] = []

    def set_signals(self, signals) -> None:
        """Signals to plot: (Signal, TradeResult | None) pairs."""
        self._signals = list(signals or [])
        self.update()

    def _geometry(self) -> dict | None:
        """Base geometry, widened so every plotted signal's entry /
        SL / TP is inside the plot (otherwise a level would fall
        off the chart). Only in-range signals widen it."""
        geo = super()._geometry()
        if geo is None:
            return None
        levels: list[float] = []
        for sig, _trade in self._signals:
            if sig.direction not in ("buy", "sell"):
                continue
            if self.bar_index_for_time(sig.generated_at) is None:
                continue
            for value in (sig.entry, sig.sl, sig.tp):
                if value is not None:
                    levels.append(float(value))
        if levels:
            lo = min([geo["lo"], *levels])
            hi = max([geo["hi"], *levels])
            geo = dict(geo)
            geo["lo"] = lo
            geo["hi"] = hi
            geo["span"] = (hi - lo) or 1.0
        return geo

    def markers(self) -> list[dict]:
        """Markers that would be drawn now (for tests + status).

        Latest first, capped at MAX_PLOTTED, only buy/sell signals
        whose generated_at falls inside the visible bars. Each
        marker carries the bar index, the pixel x/y of the entry,
        entry/SL/TP, the outcome ("open" / "tp" / "sl" / "expired")
        and the R multiple."""
        geo = self._geometry()
        if geo is None:
            return []
        ordered = sorted(
            self._signals,
            key=lambda pair: _as_aware(pair[0].generated_at)
            or _MIN_DATETIME,
            reverse=True)
        out: list[dict] = []
        for sig, trade in ordered:
            if sig.direction not in ("buy", "sell"):
                continue
            idx = self.bar_index_for_time(sig.generated_at)
            if idx is None:
                continue
            x, y = self.xy(idx, float(sig.entry), geo)
            out.append({
                "signal": sig, "trade": trade, "bar_index": idx,
                "x": x, "y": y, "direction": sig.direction,
                "entry": float(sig.entry),
                "sl": None if sig.sl is None else float(sig.sl),
                "tp": None if sig.tp is None else float(sig.tp),
                "outcome": trade.outcome if trade is not None else "open",
                "r_multiple": (trade.r_multiple
                               if trade is not None else None),
                "exit_at": (trade.exit_at
                            if trade is not None else None),
                "exit_price": (trade.exit_price
                               if trade is not None else None),
            })
            if len(out) >= self.MAX_PLOTTED:
                break
        return out

    def in_range_count(self) -> int:
        """Buy/sell signals whose generated_at is inside the
        visible bars (before the MAX_PLOTTED cap)."""
        count = 0
        for sig, _trade in self._signals:
            if sig.direction not in ("buy", "sell"):
                continue
            if self.bar_index_for_time(sig.generated_at) is not None:
                count += 1
        return count

    def paintEvent(self, event) -> None:  # noqa: D102 — Qt paint hook
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        geo = self._paint_base(painter)
        if geo is not None:
            self._paint_signals(painter, geo)

    @staticmethod
    def _dashed_line(painter: QPainter, x0: float, x1: float,
                     y: float, color: QColor) -> None:
        painter.setPen(QPen(color, 1, Qt.DashLine))
        painter.drawLine(int(round(x0)), int(round(y)),
                         int(round(x1)), int(round(y)))

    def _paint_signals(self, painter: QPainter, geo: dict) -> None:
        markers = self.markers()
        if not markers:
            painter.setPen(QColor(PALETTE["muted"]))
            painter.setFont(_font(11))
            painter.drawText(geo["pad_l"], geo["pad_t"] + 14,
                             "No Vanta signals in this range yet — "
                             "signals appear here as the board runs.")
            return
        right = geo["pad_l"] + geo["plot_w"]
        for marker in markers:
            x, y = marker["x"], marker["y"]
            buy = marker["direction"] == "buy"
            color = QColor("#2E9E5B" if buy else "#C0392B")
            # Dashed entry / SL / TP lines, from the signal's bar
            # rightwards across the chart.
            self._dashed_line(painter, x, right, y,
                              QColor(PALETTE["gold"]))
            if marker["sl"] is not None:
                _sx, y_sl = self.xy(marker["bar_index"], marker["sl"], geo)
                self._dashed_line(painter, x, right, y_sl,
                                  QColor("#C0392B"))
            if marker["tp"] is not None:
                _tx, y_tp = self.xy(marker["bar_index"], marker["tp"], geo)
                self._dashed_line(painter, x, right, y_tp,
                                  QColor("#2E9E5B"))
            # Marker triangle: buy points up from below the entry,
            # sell points down from above it.
            size = 7
            path = QPainterPath()
            if buy:
                path.moveTo(x, y + 4)
                path.lineTo(x - size, y + 4 + size)
                path.lineTo(x + size, y + 4 + size)
            else:
                path.moveTo(x, y - 4)
                path.lineTo(x - size, y - 4 - size)
                path.lineTo(x + size, y - 4 - size)
            path.closeSubpath()
            painter.setBrush(color)
            painter.setPen(Qt.NoPen)
            painter.drawPath(path)
            # Label: direction + entry + outcome (R multiple when
            # closed, "open" otherwise). Clamped inside the plot.
            outcome = marker["outcome"]
            if outcome == "open":
                outcome_t = "open"
            elif marker["r_multiple"] is not None:
                label = {"tp": "TP", "sl": "SL",
                         "expired": "expired"}[outcome]
                outcome_t = f"{label} {marker['r_multiple']:+.1f}R"
            else:
                outcome_t = outcome
            text = (f"{'BUY' if buy else 'SELL'} "
                    f"{marker['entry']:,.2f} · {outcome_t}")
            painter.setFont(_font(10, bold=True))
            painter.setPen(color)
            text_w = painter.fontMetrics().horizontalAdvance(text)
            text_x = min(max(x + 10, geo["pad_l"]), right - text_w)
            text_y = (y + 4 + size + 14) if buy else (y - 4 - size - 4)
            painter.drawText(int(text_x), int(text_y), text)
            # Exit dot for closed signals, at the exit bar/price.
            if marker["exit_at"] is not None \
                    and marker["exit_price"] is not None:
                ex = self.bar_index_for_time(marker["exit_at"])
                if ex is not None:
                    ex_x, ex_y = self.xy(ex, marker["exit_price"], geo)
                    painter.setBrush(color)
                    painter.setPen(Qt.NoPen)
                    painter.drawEllipse(int(ex_x) - 4, int(ex_y) - 4, 8, 8)


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
                painter.setBrush(QColor(PALETTE["va"]))
            else:
                painter.setBrush(QColor(PALETTE["track"]))
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


class SettingsForm(QWidget):
    """Settings form: API keys + live intervals (v0.7.0).

    Built once as a reusable widget and embedded in the System tab
    (it used to be a separate SettingsDialog — same fields, same
    Test buttons, same apply_to(); the dialog is gone). Keys are
    saved to the config file (never logged); a key that came from
    the environment is shown read-only. Test buttons verify the
    key against the real provider before saving.

    The MainWindow owns the Save / Reset buttons below the form:
    Save calls apply_to() then save_config(); Reset calls
    load_from() to restore the form from the config.
    """

    def __init__(self, config, parent=None) -> None:
        super().__init__(parent)
        self.config = config
        self._test_thread: KeyTestThread | None = None
        self._test_target: str | None = None

        form = QFormLayout(self)

        self._finnhub_edit = QLineEdit(self)
        self._finnhub_edit.setEchoMode(QLineEdit.Password)
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
        self._fedwatch_url_edit = QLineEdit(self)
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

        self._price_spin = QSpinBox(self)
        self._price_spin.setRange(5, 60)
        self._price_spin.setSuffix(" s")
        form.addRow("Live price ticker:", self._price_spin)

        self._full_spin = QSpinBox(self)
        self._full_spin.setRange(5, 1440)
        self._full_spin.setSuffix(" min")
        form.addRow("Full refresh (all sources):", self._full_spin)

        self.load_from(config)

    def load_from(self, config) -> None:
        """(Re)populate the fields from the config (Save's inverse)."""
        self.config = config
        self._finnhub_edit.setText(config.finnhub_api_key)
        self._finnhub_edit.setPlaceholderText("finnhub.io — free, 60 calls/min")
        self._fred_edit.setText(config.fred_api_key)
        self._fred_edit.setPlaceholderText("fred.stlouisfed.org — free")
        self._fedwatch_edit.setText(config.fedwatch_api_key)
        self._fedwatch_edit.setPlaceholderText("$25/mo CME EOD API — optional")
        self._fedwatch_url_edit.setText(config.fedwatch_api_url)
        self._fedwatch_url_edit.setPlaceholderText(
            "Endpoint URL from the CME FedWatch API page")
        self._price_spin.setValue(config.price_refresh_seconds)
        self._full_spin.setValue(config.full_refresh_minutes)
        self._finnhub_edit.setReadOnly(bool(config._finnhub_key_from_env))
        self._fred_edit.setReadOnly(bool(config._fred_key_from_env))
        self._fedwatch_edit.setReadOnly(bool(config._fedwatch_key_from_env))
        self._fedwatch_url_edit.setReadOnly(bool(config._fedwatch_url_from_env))
        if config._finnhub_key_from_env:
            self._finnhub_status.setText(
                "Finnhub key comes from the environment "
                "(GOLD_BOARD_FINNHUB_KEY) — change it there.")
        else:
            self._finnhub_status.setText("Free key: finnhub.io → API Keys.")
        if config._fred_key_from_env:
            self._fred_status.setText(
                "FRED key comes from the environment "
                "(GOLD_BOARD_FRED_KEY) — change it there.")
        else:
            self._fred_status.setText(
                "Free key: fred.stlouisfed.org → My Account → API Keys. "
                "Without it, macro data uses the slow fredgraph.csv fallback.")
        if config._fedwatch_key_from_env:
            self._fedwatch_status.setText(
                "FedWatch key comes from the environment — change it there.")
        else:
            self._fedwatch_status.setText(
                "Paid ($25/mo): cmegroup.com → Market Data API → FedWatch API. "
                "Without it the board uses the manual FOMC calendar.")

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
        """Copy the form values into the config (call before save)."""
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

class ChartDialog(QDialog):
    """Expanded chart window (v0.7.0) — modeless, centred on screen.

    The dashboard chart is a small preview; this dialog holds the
    SAME chart widget (reparented, never duplicated — a
    QWebEngineView cannot live in two places). Modeless so the
    dashboard keeps live-updating behind it. Closing it emits
    ``closed`` so the MainWindow can dock the chart back into the
    preview panel.
    """

    closed = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Gold price (XAUUSD) — TradingView")
        self.setWindowFlags(Qt.Window)
        self.setModal(False)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        collapse_row = QHBoxLayout()
        collapse_row.addStretch(1)
        self._collapse_button = QPushButton(
            "⛶ Collapse back to dashboard", self)
        self._collapse_button.setProperty("class", "ghost")
        self._collapse_button.clicked.connect(self.close)
        collapse_row.addWidget(self._collapse_button)
        layout.addLayout(collapse_row)
        self._chart_layout = QVBoxLayout()
        layout.addLayout(self._chart_layout)

    @property
    def chart_layout(self) -> QVBoxLayout:
        """Layout the chart container is docked into."""
        return self._chart_layout

    def closeEvent(self, event) -> None:  # noqa: D102 — Qt close hook
        super().closeEvent(event)
        self.closed.emit()


class TabDialog(QDialog):
    """Expanded tab window (v0.8.0) — modeless, centred on screen.

    Mirrors ChartDialog: the dashboard tabs stay small on a
    1366x768 screen, so a tab can be opened large here. Holds the
    SAME tab page (its QScrollArea, reparented, never copied).
    Closing it emits ``closed`` so the MainWindow can dock the
    page back into its tab. The tab bar stays usable while it is
    open; switching tabs inside the dialog is out of scope — it
    shows the tab that was expanded.
    """

    closed = Signal()

    def __init__(self, title: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"{title} — Vanta")
        self.setWindowFlags(Qt.Window)
        self.setModal(False)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        collapse_row = QHBoxLayout()
        collapse_row.addStretch(1)
        self._collapse_button = QPushButton(
            "⛶ Collapse back to dashboard", self)
        self._collapse_button.setProperty("class", "ghost")
        self._collapse_button.clicked.connect(self.close)
        collapse_row.addWidget(self._collapse_button)
        layout.addLayout(collapse_row)
        self._tab_layout = QVBoxLayout()
        layout.addLayout(self._tab_layout)

    @property
    def tab_layout(self) -> QVBoxLayout:
        """Layout the tab page is docked into."""
        return self._tab_layout

    def closeEvent(self, event) -> None:  # noqa: D102 — Qt close hook
        super().closeEvent(event)
        self.closed.emit()


class MainWindow(QMainWindow):
    """Vanta main window — one screen, ten panels, black + gold."""

    def __init__(self, db_path: str | Path | None = None, parent=None,
                 enable_live_ticker: bool = True) -> None:
        super().__init__(parent)
        self._enable_live_ticker = enable_live_ticker
        self.config = load_config()
        self.db_path = Path(db_path) if db_path else Path(self.config.db_path)
        self.settings = QSettings("Vanta", "GoldBoard")
        # Theme (v0.7.0): dark by default; the choice is remembered.
        # Applied BEFORE the stylesheet is built, so the first paint
        # already uses the right palette.
        self._theme = self.settings.value("theme", "dark", type=str) or "dark"
        if self._theme not in ("dark", "light"):
            self._theme = "dark"
        set_theme(self._theme)
        # Expanded chart dialog (v0.7.0): None while docked in preview.
        self._chart_dialog: ChartDialog | None = None
        # Expanded tab dialog (v0.8.0): None while docked in its tab.
        # _expanded_tab is (tab key, tab index, tab title) while open.
        self._tab_dialog: TabDialog | None = None
        self._expanded_tab: tuple | None = None
        # Chart view (v0.8.0): "tradingview" (the live embed) or
        # "signals" (the built-in chart with Vanta's signal markers).
        self._chart_view = "tradingview"
        # (Signal, TradeResult | None) pairs for the markers chart —
        # computed from the payload in _apply_payload, never invented.
        self._signal_markers: list[tuple] = []
        self._signals_history: list = []
        self._signal_outcome_by_key: dict = {}
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
            border: none; border-radius: 6px; padding: 8px 16px; font-weight: bold; }}
        QPushButton:hover {{ background: {PALETTE["gold_light"]}; }}
        QPushButton:disabled {{ background: {PALETTE["track"]}; color: {PALETTE["muted"]}; }}
        QPushButton.ghost {{ background: transparent; color: {PALETTE["gold"]};
            border: 1px solid {PALETTE["gold"]}; }}
        QPushButton.ghost:hover {{ background: {PALETTE["hover_bg"]}; }}
        QPushButton.ghost:checked {{ background: {PALETTE["hover_bg"]}; }}
        QPushButton.gold {{ background: {PALETTE["gold"]}; color: {PALETTE["black"]};
            border: none; }}
        QPushButton[compact="true"] {{ padding: 3px 10px; font-size: 12px; }}
        QMenu {{ background: {PALETTE["surface"]}; color: {PALETTE["text"]};
            border: 1px solid {PALETTE["gold"]}; border-radius: 6px; padding: 4px; }}
        QMenu::item:selected {{ background: {PALETTE["hover_bg"]}; }}
        QGroupBox {{ border: 1px solid {PALETTE["border"]}; border-radius: 12px;
            margin-top: 14px; padding: 16px; background: {PALETTE["surface"]}; }}
        QGroupBox::title {{ color: {PALETTE["gold"]}; subcontrol-origin: margin;
            left: 8px; padding: 0 4px; font-weight: bold; font-size: 14px; }}
        QTabWidget::pane {{ border: 1px solid {PALETTE["border"]};
            border-radius: 8px; background: {PALETTE["black"]}; }}
        QTabBar::tab {{ background: {PALETTE["surface"]}; color: {PALETTE["muted"]};
            border: 1px solid {PALETTE["border"]}; border-bottom: none;
            border-top-left-radius: 8px; border-top-right-radius: 8px;
            padding: 5px 14px; font-weight: bold; }}
        QTabBar::tab:selected {{ background: {PALETTE["black"]};
            color: {PALETTE["gold"]}; border-bottom: 2px solid {PALETTE["gold"]}; }}
        QTabBar::tab:hover {{ color: {PALETTE["text"]}; }}
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

    # ------------------------------------------------------------------ theme

    def _sync_theme_controls(self) -> None:
        """Header toggle + Settings-tab checkbox follow _theme."""
        light = self._theme == "light"
        if hasattr(self, "_theme_button"):
            self._theme_button.setChecked(light)
            self._theme_button.setText("🌙 Dark" if light else "☀ Light")
        if hasattr(self, "_theme_check"):
            self._theme_check.setChecked(light)

    def _toggle_theme(self) -> None:
        """Flip dark/light, persist, and repaint everything."""
        self._theme = "light" if self._theme == "dark" else "dark"
        self.settings.setValue("theme", self._theme)
        self._apply_theme()
        self.log(f"Theme switched to {self._theme}.")

    def _apply_theme(self) -> None:
        """Re-apply the active palette everywhere it shows.

        The global stylesheet is rebuilt from PALETTE, inline
        stylesheets (strip cards, price value) are refreshed, the
        custom painters repaint, and the TradingView embed reloads
        with its own light/dark theme.
        """
        set_theme(self._theme)
        self.setStyleSheet(self._stylesheet())
        if self._chart_dialog is not None:
            self._chart_dialog.setStyleSheet(self._stylesheet())
        self._refresh_strip_styles()
        self._price_value.setStyleSheet(f"color: {PALETTE['gold_light']};")
        for widget in (self._gauge, self._cot_chart, self._price_chart,
                       self._volume_widget):
            widget.update()
        self._tv_chart.set_theme(self._theme)
        self._sync_theme_controls()

    def _refresh_strip_styles(self) -> None:
        """Re-apply the strip cards' inline styles for the theme."""
        for card in self._strip_cards.values():
            card["frame"].setStyleSheet(self._kpi_card_style())
            value = card["value"]
            # Pill values restyle themselves via _set_pill (fixed
            # functional colours); plain values use gold_light.
            if value.property("kpi_plain") is True:
                value.setStyleSheet(f"color: {PALETTE['gold_light']};")

    @staticmethod
    def _kpi_card_style() -> str:
        """Frame stylesheet for a strip card (theme-aware)."""
        return (f"#kpiCard {{ background: {PALETTE['surface']}; "
                f"border: 1px solid {PALETTE['border']}; border-radius: 12px; "
                "padding: 10px; }")

    # ------------------------------------------------------------------ build

    def _build_header(self) -> None:
        header = QWidget(self)
        layout = QHBoxLayout(header)
        # Compact header (v0.7.1): the 66px header + 115px strip left
        # the tabs ~85px tall on a 1366x768 screen. Smaller logo,
        # tighter margins and compact buttons buy the tabs ~20px.
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(8)
        logo_path = resource_path("assets/logo/vanta-logo.png")
        if not logo_path.is_file():
            from goldboard_core.branding import get_app_logo_path
            logo_path = get_app_logo_path()
        # Logo in a visible tile: the PNG is a gold ring on black, so
        # on the near-black header it was invisible without a border.
        logo = QLabel(header)
        logo.setFixedSize(36, 36)
        logo.setStyleSheet(
            f"border: 1px solid {PALETTE['gold']}; border-radius: 10px; "
            "background-color: #0a0a0a; padding: 2px;")
        if logo_path is not None and Path(logo_path).is_file():
            pix = QPixmap(str(logo_path)).scaled(
                30, 30, Qt.KeepAspectRatio, Qt.SmoothTransformation)
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
        self._settings_button.setProperty("compact", True)
        self._settings_button.clicked.connect(self._show_settings_tab)
        layout.addWidget(self._settings_button)
        # Theme toggle (v0.7.0): header button + the same toggle in
        # the Settings tab. Persisted in QSettings ("theme").
        self._theme_button = QPushButton(header)
        self._theme_button.setProperty("class", "ghost")
        self._theme_button.setProperty("compact", True)
        self._theme_button.setCheckable(True)
        self._theme_button.clicked.connect(self._toggle_theme)
        layout.addWidget(self._theme_button)
        self._sync_theme_controls()
        # Update button is ALWAYS visible now: before v0.4.0 it only
        # appeared after a successful check found a newer release, so
        # a failed or pre-release check left the user with no button
        # at all. Clicking it with no pending update re-checks.
        self._update_button = QPushButton("Check for updates", header)
        self._update_button.setProperty("class", "ghost")
        self._update_button.setProperty("compact", True)
        self._update_button.clicked.connect(self._on_update_button)
        layout.addWidget(self._update_button)
        self._panels_button = QPushButton("Panels ▾", header)
        self._panels_button.setProperty("class", "ghost")
        self._panels_button.setProperty("compact", True)
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
        self._refresh_button.setProperty("compact", True)
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

    # Tabs for the lower panels (v0.7.0): the board used to be one
    # long 3-column scrolling grid, so finding a panel meant hunting.
    # Each tab holds its panels in a 3-column grid; the summary strip
    # and the chart preview stay ABOVE the tabs, always visible.
    _TAB_SPECS = [
        ("institutions", "Institutions"),
        ("macro_news", "Macro and News"),
        ("technical", "Technical"),
        ("signals", "Signals and Gates"),
        ("system", "System"),
    ]

    def _build_tabs(self) -> None:
        """QTabWidget with one scrolling page per tab."""
        self._tabs = QTabWidget(self)
        self._tab_pages: dict[str, QScrollArea] = {}
        self._tab_content: dict[str, QWidget] = {}
        self._tab_grids: dict[str, QGridLayout] = {}
        self._tab_index: dict[str, int] = {}
        for key, title in self._TAB_SPECS:
            scroll = QScrollArea(self)
            scroll.setWidgetResizable(True)
            content = QWidget(scroll)
            grid = QGridLayout(content)
            grid.setSpacing(14)
            grid.setVerticalSpacing(16)
            grid.setColumnStretch(0, 1)
            grid.setColumnStretch(1, 1)
            grid.setColumnStretch(2, 1)
            content.setLayout(grid)
            scroll.setWidget(content)
            self._tab_pages[key] = scroll
            self._tab_content[key] = content
            self._tab_grids[key] = grid
            self._tab_index[key] = self._tabs.addTab(scroll, title)
        # Double-click a tab opens it large (v0.8.0) — same dialog
        # as the "Expand tab" button below the chart.
        self._tabs.tabBar().tabBarDoubleClicked.connect(
            self._on_tab_double_clicked)
        # "Expand tab" lives in the tab bar's corner (v0.8.0): a
        # separate row above the tabs would cost them ~30px at
        # 1366x768, and the v0.7.1 layout test holds the tabs to
        # >= 250px there.
        self._expand_tab_button = QPushButton("Expand tab", self._tabs)
        self._expand_tab_button.setProperty("class", "ghost")
        self._expand_tab_button.setProperty("compact", True)
        self._expand_tab_button.setToolTip(
            "Open the current tab large in a centred window "
            "(or double-click a tab)")
        self._expand_tab_button.clicked.connect(
            lambda _checked=False: self._expand_tab())
        self._tabs.setCornerWidget(self._expand_tab_button,
                                   Qt.TopRightCorner)

    def _register_panel(self, key: str, box: QGroupBox,
                        wide: bool = False, tab: str | None = None,
                        hideable: bool = True) -> None:
        visible = self.settings.value(
            f"panels/{key}",
            self._PANEL_DEFAULTS.get(key, True), type=bool)
        self._panels.append({"key": key, "box": box, "wide": wide,
                             "tab": tab, "hideable": hideable})
        # Track visibility in a dict, not box.isVisible(): during
        # __init__ the window is not shown yet, so isVisible() is
        # False for every panel and the relayout would add nothing.
        self._panel_visible[key] = bool(visible)
        box.setVisible(bool(visible))

    def _relayout_panels(self) -> None:
        """Re-flow the visible panels into their tabs, 3 per row.

        A tab with no visible panels is hidden entirely; unhiding
        any panel in it restores the tab.
        """
        for tab_key, _title in self._TAB_SPECS:
            if tab_key not in self._tab_index:
                # Tab page is out in the expand dialog (v0.8.0) —
                # nothing to re-flow until it docks back.
                continue
            grid = self._tab_grids[tab_key]
            while grid.count():
                grid.takeAt(0)
            row, col = 0, 0
            any_visible = False
            for panel in self._panels:
                if panel.get("tab") != tab_key:
                    continue
                if not self._panel_visible.get(panel["key"], True):
                    continue
                any_visible = True
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
            self._tabs.setTabVisible(self._tab_index[tab_key], any_visible)

    def _build_panels_menu(self) -> None:
        self._panels_menu.clear()
        for panel in self._panels:
            # Core dashboard elements (chart preview, settings) are
            # not hideable: hiding them would strand the user with
            # no way back except the menu they just used.
            if not panel.get("hideable", True):
                continue
            action = QAction(panel["box"].title(), self)
            action.setCheckable(True)
            action.setChecked(self._panel_visible.get(panel["key"], True))
            key = panel["key"]
            action.triggered.connect(
                lambda checked=False, k=key: self._toggle_panel(k, checked))
            self._panels_menu.addAction(action)
        # Logs drawer (v0.7.1): hidden by default to give the tabs
        # room; the menu is how it comes back. Guarded: the menu is
        # also built before the dock exists.
        if hasattr(self, "_log_dock"):
            self._panels_menu.addSeparator()
            logs_action = QAction("Logs", self)
            logs_action.setCheckable(True)
            logs_action.setChecked(self._log_dock.isVisible())
            logs_action.triggered.connect(
                lambda checked=False: self._toggle_logs(checked))
            self._panels_menu.addAction(logs_action)

    def _toggle_panel(self, key: str, visible: bool) -> None:
        for panel in self._panels:
            if panel["key"] == key:
                self.settings.setValue(f"panels/{key}", bool(visible))
                self._panel_visible[key] = bool(visible)
                panel["box"].setVisible(bool(visible))
                break
        self._relayout_panels()
        self._build_panels_menu()

    # ------------------------------------------------------- summary strip
    # v0.6.0: the essence of the whole analysis, always at the top,
    # above all panels and NOT hideable from the Panels menu. One row
    # of KPI cards, each fed from the payload the worker already
    # produces — no new data sources. Before the first refresh every
    # card shows an honest empty state ("—" / "No data yet").

    def _kpi_card(self, title: str, pill: bool = False,
                  value_size: int = 20, wrap_value: bool = False
                  ) -> tuple[QFrame, QLabel, QLabel]:
        """One strip card: small title, big value, small sub-line.

        Values do not wrap by default: a word-wrapped QLabel reports
        a huge sizeHint before it is laid out (it wraps the 28px
        price into 4 lines at width 0) and the whole strip balloons
        to ~270px tall. Only the event card wraps (long event names).
        """
        frame = QFrame(self)
        # Scoped to this frame by object name: a bare declaration
        # block on a QFrame would cascade to every child label and
        # wrap each of them in its own bordered box (and inflate the
        # strip to ~270px tall).
        frame.setObjectName("kpiCard")
        frame.setStyleSheet(self._kpi_card_style())
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(4)
        title_label = QLabel(title, frame)
        title_label.setFont(_font(10, bold=True))
        title_label.setProperty("class", "muted")
        layout.addWidget(title_label)
        if pill:
            value = _status_pill("—", "grey")
            row = QHBoxLayout()
            row.addWidget(value)
            row.addStretch(1)
            layout.addLayout(row)
        else:
            value = QLabel("—", frame)
            value.setFont(_font(value_size, bold=True))
            value.setStyleSheet(f"color: {PALETTE['gold_light']};")
            value.setProperty("kpi_plain", True)
            value.setWordWrap(wrap_value)
            layout.addWidget(value)
        sub = QLabel("No data yet", frame)
        sub.setFont(_font(11))
        sub.setProperty("class", "muted")
        sub.setWordWrap(True)
        layout.addWidget(sub)
        layout.addStretch(1)
        return frame, value, sub

    def _build_summary_strip(self) -> QWidget:
        strip = QWidget(self)
        row = QHBoxLayout(strip)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(12)
        self._strip_cards: dict[str, dict] = {}
        specs = [
            ("price", "LIVE PRICE · XAUUSD", False, 28, False),
            ("technical", "TECHNICAL SIGNAL", True, 20, False),
            ("regime", "REGIME FILTER", True, 20, False),
            ("score", "INSTITUTIONAL SCORE", False, 20, False),
            ("setups", "ACTIVE SETUPS", False, 20, False),
            ("event", "NEXT MACRO EVENT", False, 15, True),
            ("volume", "VOLUME PROFILE · POC", False, 20, False),
        ]
        for key, title, pill, size, wrap in specs:
            frame, value, sub = self._kpi_card(title, pill=pill,
                                               value_size=size,
                                               wrap_value=wrap)
            self._strip_cards[key] = {"frame": frame, "value": value,
                                      "sub": sub}
            row.addWidget(frame, 1)
        # Guard: the strip is a summary, it must never eat the board.
        strip.setMaximumHeight(150)
        self._strip_widget = strip
        return strip

    def _update_summary_strip(self, payload: dict) -> None:
        """Refresh every strip card from the payload (called on every
        refresh; the live ticker updates the price card separately)."""
        cards = self._strip_cards

        # (1) Live price + change vs yesterday + source. The value
        # shows no unit: the card title already says XAUUSD, and the
        # unit would push the 28px value onto a second line.
        price = payload.get("price")
        if price is not None:
            cards["price"]["value"].setText(_fmt_value(price.value))
            history = payload.get("price_history", []) or []
            if len(history) >= 2:
                prev = history[-2]["value"]
                change = price.value - prev
                pct = (change / prev * 100.0) if prev else 0.0
                sign = "+" if change >= 0 else ""
                change_t = f"{sign}{change:,.2f} ({sign}{pct:.2f}%) vs prev"
            else:
                change_t = "Change: —"
            source = getattr(price, "source", "") or "Yahoo Finance (GC=F)"
            if getattr(price, "is_proxy", False):
                source += " (proxy)"
            live = " · ● LIVE" if self._ticker_ok else ""
            cards["price"]["sub"].setText(f"{change_t} · {source}{live}")
        else:
            cards["price"]["value"].setText("—")
            cards["price"]["sub"].setText(
                "No data yet — refresh to fetch the quote.")

        # (2) Latest advisory signal (v0.8.0): the frozen signal
        # from the institutional score — direction pill, entry,
        # time, confidence. The indicator counts live in the
        # Technical panel now; the strip shows the signal itself.
        # "none" is honest: the score is inside the ±40 band.
        tech = payload.get("technical") or {}
        signal = payload.get("signal")
        if signal is not None and signal.direction in ("buy", "sell"):
            kind = "green" if signal.direction == "buy" else "red"
            _set_pill(cards["technical"]["value"],
                      signal.direction.upper(), kind)
            cards["technical"]["sub"].setText(
                f"@ {_fmt_value(signal.entry)} · "
                f"{_fmt_time(signal.generated_at)} · "
                f"conf {signal.confidence:.0%}")
        elif signal is not None:
            _set_pill(cards["technical"]["value"], "NO SIGNAL", "grey")
            total = payload.get("score")
            total_t = total.total if total is not None else 0.0
            cards["technical"]["sub"].setText(
                f"Score {total_t:+.1f} in ±40 band — no signal")
        else:
            _set_pill(cards["technical"]["value"], "—", "grey")
            cards["technical"]["sub"].setText(
                "No data yet — refresh to compute indicators.")

        # (3) Regime filter label + ER / CHOP.
        rf = tech.get("regime_filter") or {}
        label = str(rf.get("label") or "")
        if label and label != "unknown":
            _set_pill(cards["regime"]["value"], label.upper(),
                      REGIME_PILL_KIND.get(label, "grey"))
            er_t = (f"{rf['er']:.2f}"
                    if rf.get("er") is not None else "—")
            chop_t = (f"{rf['chop']:.1f}"
                      if rf.get("chop") is not None else "—")
            cards["regime"]["sub"].setText(f"ER {er_t} · CHOP {chop_t}")
        else:
            _set_pill(cards["regime"]["value"], "—", "grey")
            cards["regime"]["sub"].setText(
                "No data yet — needs 50+ daily bars.")

        # (4) Institutional score + verdict + top component.
        score = payload.get("score")
        features = payload.get("features") or {}
        pillars = sum(1 for v in features.values() if v is not None)
        if score is not None:
            cards["score"]["value"].setText(f"{score.total:+.1f} / ±100")
            top = None
            if score.components:
                top = max(score.components, key=lambda c: abs(c.score))
            top_t = f" · top: {top.name}" if top is not None else ""
            cards["score"]["sub"].setText(
                f"{_score_verdict(score.total)}{top_t} · "
                f"{pillars} of 5 pillars")
        else:
            cards["score"]["value"].setText("—")
            cards["score"]["sub"].setText("No score yet — no data.")

        # (5) Active setups: count + top setup. A directional setup
        # is the top one (v0.8.0): the event caution used to come
        # first with direction "none", so the card read "· NONE"
        # while real long/short setups existed below it. A caution
        # or conflict shown on its own is named without a fake
        # direction.
        setups = payload.get("setups") or []
        cards["setups"]["value"].setText(str(len(setups)))
        if setups:
            top = next(
                (s for s in setups
                 if str(s.get("direction")) in ("long", "short")),
                setups[0])
            direction = str(top.get("direction", "none"))
            if direction in ("long", "short"):
                cards["setups"]["sub"].setText(
                    f"{top.get('name', '')} · {direction.upper()}")
            else:
                cards["setups"]["sub"].setText(str(top.get("name", "")))
        else:
            cards["setups"]["sub"].setText(
                "No setups yet — refresh to evaluate the rules.")

        # (6) Next macro event + countdown.
        events = payload.get("events") or []
        if events:
            nxt = events[0]
            cards["event"]["value"].setText(nxt.get("name", "—"))
            days = nxt.get("days_away")
            when = (f"in {days} day{'s' if days != 1 else ''}"
                    if days is not None else str(nxt.get("date", "")))
            cards["event"]["sub"].setText(
                f"{when} · {nxt.get('date', '')}")
        else:
            cards["event"]["value"].setText("—")
            cards["event"]["sub"].setText("No events in the calendar yet.")

        # (7) Volume profile POC + value area (no unit: the card
        # title says what it is, and the unit would wrap the value).
        profile = payload.get("volume_profile") or {}
        if profile.get("complete"):
            cards["volume"]["value"].setText(f"{profile['poc']:,.2f}")
            cards["volume"]["sub"].setText(
                f"Value area {profile['value_area_low']:,.0f} – "
                f"{profile['value_area_high']:,.0f}")
        else:
            cards["volume"]["value"].setText("—")
            cards["volume"]["sub"].setText(
                "No data yet — refresh to fetch intraday bars.")

    def _build_panels(self) -> None:
        self._panels: list[dict] = []
        self._panel_visible: dict[str, bool] = {}
        self._build_summary_strip()
        self._build_tabs()

        # (1) Gold price + TradingView chart — full width, directly
        # under the summary strip and ABOVE the tabs (v0.7.0: a small
        # ~240px preview; clicking it opens the centred expand dialog).
        # Not in a tab and not hideable: it is a core dashboard
        # element, like the strip.
        content = QWidget(self)
        box, layout = self._panel("Gold price (XAUUSD) — LIVE · TradingView")
        self._price_box = box
        self._price_layout = layout
        # Tighter box chrome for the hero panel only (v0.7.1): the
        # global QGroupBox padding (16px) + margin-top (14px) cost
        # ~46px the tabs below need on a 1366x768 screen. Border,
        # radius and background still come from the global rule.
        box.setStyleSheet("QGroupBox { padding: 8px; margin-top: 12px; }")
        # Compact price panel (v0.7.1): at 1366x768 the old panel
        # (price / change / source / expand row / 240px chart /
        # status row / range row) was ~530px tall and left the tabs
        # ~85px with no way to scroll to them. Now price + change +
        # Expand share one row, the source is one short line, and
        # status + Retry + range buttons share one row that travels
        # with the chart into the expand dialog.
        layout.setContentsMargins(8, 2, 8, 2)
        layout.setSpacing(2)
        self._price_value = QLabel("—", content)
        self._price_value.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self._price_value.setFont(_font(30, bold=True))
        self._price_value.setStyleSheet(f"color: {PALETTE['gold_light']};")
        self._price_change = QLabel("—", content)
        self._price_change.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        # Expand affordance: a button in the price row, and a click
        # on the chart area itself (see eventFilter). No ⛶ glyph in
        # the label (v0.7.1): the symbol falls back to a tall symbol
        # font and inflates the button (and the whole price row)
        # from 25px to 33px. The dialog's Collapse button keeps its
        # glyph — height is free there.
        price_row = QHBoxLayout()
        price_row.addWidget(self._price_value)
        price_row.addWidget(self._price_change)
        price_row.addStretch(1)
        self._expand_button = QPushButton("Expand chart", content)
        self._expand_button.setProperty("class", "ghost")
        self._expand_button.setProperty("compact", True)
        self._expand_button.setToolTip(
            "Open the chart large in a centred window "
            "(or click the chart itself)")
        self._expand_button.clicked.connect(self._expand_chart)
        price_row.addWidget(self._expand_button)
        self._price_source = QLabel("No data yet.", content)
        self._price_source.setProperty("class", "muted")
        self._price_source.setWordWrap(False)
        self._price_source.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        # Real TradingView candlestick chart; the built-in chart is the
        # fallback when TradingView cannot load (offline / blocked).
        # The chart + status + retry + range buttons live in one
        # container that moves into the expand dialog and back.
        self._price_chart = PriceChart(content)
        self._tv_chart = TradingViewChart(self._price_chart, content,
                                          theme=self._theme)
        self._tv_chart.set_min_height(PREVIEW_MIN_HEIGHT)
        self._tv_chart.status_changed.connect(self._update_chart_status)
        merged_row = QHBoxLayout()
        self._chart_status = QLabel(
            "TradingView loading…", content)
        self._chart_status.setWordWrap(False)
        # Horizontally Ignored (v0.7.1): the fallback status text is
        # ~500px wide; with a normal policy the merged row's minimum
        # width forced the expand dialog wider than its 760px
        # geometry on an 800px screen (dialog came out 801px and
        # off-centre). The label may elide in a narrow dialog; the
        # buttons must never be pushed out.
        self._chart_status.setSizePolicy(
            QSizePolicy.Ignored, QSizePolicy.Preferred)
        self._chart_retry_button = QPushButton(
            "Retry TradingView", content)
        self._chart_retry_button.setProperty("class", "ghost")
        self._chart_retry_button.setProperty("compact", True)
        self._chart_retry_button.clicked.connect(
            self._on_retry_tradingview)
        merged_row.addWidget(self._chart_status, 1)
        merged_row.addWidget(self._chart_retry_button)
        self._range_buttons: dict[str, QPushButton] = {}
        for label in ("1D", "5D", "1M", "1Y", "2Y"):
            btn = QPushButton(label, content)
            btn.setProperty("class", "ghost")
            btn.setProperty("compact", True)
            btn.setCheckable(True)
            btn.setChecked(label == self._chart_range)
            btn.clicked.connect(
                lambda _checked=False, r=label: self._set_chart_range(r))
            self._range_buttons[label] = btn
            merged_row.addWidget(btn)
        self._chart_container = QWidget(content)
        container_layout = QVBoxLayout(self._chart_container)
        container_layout.setContentsMargins(0, 0, 0, 0)
        container_layout.setSpacing(4)
        # Chart view switcher (v0.8.0): TradingView is a
        # cross-origin iframe — no marker API exists for it — so
        # Vanta's signals are plotted on the built-in chart instead.
        # The switcher buttons live in the merged control row below
        # (NOT their own row: a separate row would cost the tabs
        # ~30px at 1366x768, and the v0.7.1 layout test holds the
        # tabs to >= 250px there).
        self._signals_chart = SignalChart(content)
        self._signals_chart.setMinimumHeight(PREVIEW_MIN_HEIGHT)
        self._view_stack = QStackedWidget(content)
        self._view_stack.addWidget(self._tv_chart)
        self._view_stack.addWidget(self._signals_chart)
        self._view_stack.setSizePolicy(
            QSizePolicy.Expanding, QSizePolicy.Ignored)
        self._view_stack.setMinimumHeight(PREVIEW_MIN_HEIGHT)
        self._view_buttons: dict[str, QPushButton] = {}
        for key, label in (("tradingview", "TradingView"),
                           ("signals", "Vanta signals")):
            btn = QPushButton(label, content)
            btn.setProperty("class", "ghost")
            btn.setProperty("compact", True)
            btn.setCheckable(True)
            btn.setChecked(key == self._chart_view)
            btn.setToolTip(
                "TradingView: the live chart. Vanta signals: the "
                "built-in chart with Vanta's buy/sell markers + "
                "SL/TP, so accuracy can be checked.")
            btn.clicked.connect(
                lambda _checked=False, k=key: self._set_chart_view(k))
            self._view_buttons[key] = btn
        # Into the merged control row, ahead of the status label —
        # the row travels into the expand dialog with the chart.
        for i, key in enumerate(("tradingview", "signals")):
            merged_row.insertWidget(i, self._view_buttons[key])
        container_layout.addWidget(self._view_stack)
        container_layout.addLayout(merged_row)
        # Placeholder shown in the preview while the chart is expanded.
        self._chart_placeholder = QWidget(content)
        ph_layout = QHBoxLayout(self._chart_placeholder)
        ph_label = QLabel("Chart is open in the expanded window.", content)
        ph_label.setProperty("class", "muted")
        self._restore_button = QPushButton("Show chart here", content)
        self._restore_button.setProperty("class", "ghost")
        self._restore_button.clicked.connect(self._collapse_chart)
        ph_layout.addWidget(ph_label, 1)
        ph_layout.addWidget(self._restore_button)
        self._chart_placeholder.setVisible(False)
        layout.addLayout(price_row)
        layout.addWidget(self._price_source)
        layout.addWidget(self._chart_container)
        layout.addWidget(self._chart_placeholder)
        layout.addStretch(1)
        self._register_panel("price", box, wide=True, hideable=False)
        self._update_chart_status()
        self._install_chart_click_filters()

        # (2) Total score gauge + components.
        content = self._tab_content["institutions"]
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
        self._register_panel("score", box, tab="institutions")

        # (3) COT chart.
        box, layout = self._panel("COT — managed-money net (contracts)")
        self._cot_chart = CotChart(content)
        self._cot_summary = QLabel("No COT data yet.", content)
        self._cot_summary.setWordWrap(True)
        self._cot_summary.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        layout.addWidget(self._cot_chart)
        layout.addWidget(self._cot_summary)
        self._register_panel("cot", box, tab="institutions")

        # (4) ETF flow.
        box, layout = self._panel("ETF flow — GLD / IAU")
        self._etf_label = QLabel("No ETF data yet.", content)
        self._etf_label.setWordWrap(True)
        self._etf_label.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        layout.addWidget(self._etf_label)
        layout.addStretch(1)
        self._register_panel("etf", box, tab="institutions")

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
        self._register_panel("central_bank", box, tab="institutions")

        # (6) Macro.
        content = self._tab_content["macro_news"]
        box, layout = self._panel("Macro — rates / USD / Fed")
        self._macro_label = QLabel("No macro data yet.", content)
        self._macro_label.setWordWrap(True)
        self._macro_label.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        layout.addWidget(self._macro_label)
        layout.addStretch(1)
        self._register_panel("macro", box, tab="macro_news")

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
        self._register_panel("news", box, tab="macro_news")

        # (8) Signals.
        content = self._tab_content["signals"]
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
        self._register_panel("signals", box, tab="signals")

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
        self._register_panel("gates", box, tab="signals")

        # (10) Data freshness.
        content = self._tab_content["system"]
        box, layout = self._panel("Data freshness")
        self._freshness_list = QListWidget(content)
        self._freshness_list.setWordWrap(True)
        self._freshness_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._freshness_list.setMinimumHeight(150)
        layout.addWidget(self._freshness_list)
        self._register_panel("freshness", box, wide=True, tab="system")

        # (11) History backtest.
        content = self._tab_content["technical"]
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
        self._register_panel("backtest", box, wide=True, tab="technical")

        # (12) Technical summary + setups — computed locally from
        # stored Yahoo history (free; Finnhub's aggregate endpoint
        # is premium-only). Advisory only, never a trade order.
        box, layout = self._panel("Technical summary & setups (advisory)")
        tech_pills_row = QHBoxLayout()
        self._tech_signal_pill = _status_pill("—", "grey")
        self._tech_regime_pill = _status_pill("—", "grey")
        tech_pills_row.addWidget(self._tech_signal_pill)
        tech_pills_row.addWidget(self._tech_regime_pill)
        tech_pills_row.addStretch(1)
        self._technical_label = QLabel(
            "No history yet — refresh to compute RSI / MACD / SMA / ADX.",
            content)
        self._technical_label.setWordWrap(True)
        self._technical_label.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self._setups_list = QListWidget(content)
        self._setups_list.setWordWrap(True)
        self._setups_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._setups_list.setMinimumHeight(120)
        layout.addLayout(tech_pills_row)
        layout.addWidget(self._technical_label)
        layout.addWidget(self._setups_list)
        layout.addStretch(1)
        self._register_panel("technical", box, wide=True, tab="technical")

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
        self._register_panel("volume", box, tab="technical")

        # (14) Macro event calendar — the Live Research Engine diary.
        content = self._tab_content["macro_news"]
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
        self._register_panel("events", box, tab="macro_news")

        # (15) Settings — the same form the old SettingsDialog had
        # (API keys with Test buttons + live intervals), now embedded
        # in the System tab. Not hideable: the System tab must always
        # have somewhere to go.
        content = self._tab_content["system"]
        box, layout = self._panel("Settings — API keys & live data")
        self._settings_form = SettingsForm(self.config, content)
        layout.addWidget(self._settings_form)
        theme_row = QHBoxLayout()
        self._theme_check = QCheckBox("☀ Light theme", content)
        self._theme_check.setChecked(self._theme == "light")
        self._theme_check.toggled.connect(
            lambda checked: self._set_theme_from_check(checked))
        theme_row.addWidget(self._theme_check)
        theme_row.addStretch(1)
        layout.addLayout(theme_row)
        settings_row = QHBoxLayout()
        self._settings_save_button = QPushButton("Save settings", content)
        self._settings_save_button.clicked.connect(
            self._save_settings_from_form)
        self._settings_reset_button = QPushButton("Reset", content)
        self._settings_reset_button.setProperty("class", "ghost")
        self._settings_reset_button.clicked.connect(
            self._reset_settings_form)
        settings_row.addWidget(self._settings_save_button)
        settings_row.addWidget(self._settings_reset_button)
        settings_row.addStretch(1)
        layout.addLayout(settings_row)
        self._register_panel("settings", box, wide=True, tab="system",
                             hideable=False)

        self._relayout_panels()
        self._build_panels_menu()

        central = QWidget(self)
        central_layout = QVBoxLayout(central)
        central_layout.addWidget(self._header)
        central_layout.addWidget(self._strip_widget)
        central_layout.addWidget(self._price_box)
        central_layout.addWidget(self._tabs, 1)
        central_layout.setContentsMargins(8, 4, 8, 4)
        central_layout.setSpacing(2)
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
        # Hidden by default (v0.7.1): the 130px drawer plus a ~530px
        # price panel left the tabs ~85px tall on a 1366x768 screen.
        # The Panels menu has a "Logs" toggle to bring it back.
        dock.setVisible(
            self.settings.value("logs_visible", False, type=bool))
        dock.visibilityChanged.connect(self._on_log_dock_visibility)
        self._build_panels_menu()

    def _on_log_dock_visibility(self, visible: bool) -> None:
        """Dock closed (X) or shown: remember it + refresh the menu.

        The Panels menu's Logs action reads isVisible() at build
        time, so rebuild it to keep the checkmark honest.
        """
        self.settings.setValue("logs_visible", bool(visible))
        self._build_panels_menu()

    def _toggle_logs(self, visible: bool) -> None:
        """Panels-menu Logs action: show/hide the log drawer."""
        self.settings.setValue("logs_visible", bool(visible))
        self._log_dock.setVisible(bool(visible))

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

    def _show_settings_tab(self) -> None:
        """Header Settings button: switch to the System tab (v0.7.0).

        The settings form lives there now — no separate dialog.
        """
        self._tabs.setCurrentIndex(self._tab_index["system"])

    def _save_settings_from_form(self) -> None:
        """Save button in the System tab: persist + apply, as the
        old dialog's Save did."""
        self._settings_form.apply_to(self.config)
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

    def _reset_settings_form(self) -> None:
        """Reset button: restore the form from the saved config."""
        self._settings_form.load_from(self.config)
        self.log("Settings form reset to saved values.")

    def _set_theme_from_check(self, checked: bool) -> None:
        """Settings-tab theme checkbox: same toggle as the header."""
        want = "light" if checked else "dark"
        if want != self._theme:
            self._toggle_theme()

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
        # Summary strip price card follows the live tick too.
        strip_price = self._strip_cards["price"]
        strip_price["value"].setText(_fmt_value(value))
        strip_price["sub"].setText(
            f"{self._price_change.text()} · {source} · ● LIVE")

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

    def _set_chart_view(self, view: str) -> None:
        """Flip between the live TradingView chart and the Vanta
        signals chart (same bars, same range buttons)."""
        if view not in ("tradingview", "signals"):
            return
        self._chart_view = view
        for key, btn in self._view_buttons.items():
            btn.setChecked(key == view)
        self._view_stack.setCurrentIndex(
            0 if view == "tradingview" else 1)
        self._update_chart_status()
        if view == "signals":
            self._refresh_chart()

    def _set_chart_min_height(self, height: int) -> None:
        """Preview (200px) / expanded (480px) height for the chart
        area: the TradingView stack, the signals chart, and the view
        stack that holds them all move together."""
        self._tv_chart.set_min_height(height)
        self._signals_chart.setMinimumHeight(height)
        self._view_stack.setMinimumHeight(height)

    def _refresh_chart(self) -> None:
        if self._chart_range in ("1D", "5D"):
            bars = self._chart_bars_intraday
            if self._chart_range == "1D":
                bars = bars[-96:]
        else:
            count = {"1M": 22, "1Y": 250, "2Y": 500}[self._chart_range]
            bars = self._chart_bars_daily[-count:]
        self._price_chart.set_bars(bars, self._live_tick)
        # The signals chart shows the same bars + Vanta's signals.
        self._signals_chart.set_bars(bars, self._live_tick)
        self._signals_chart.set_signals(self._signal_markers)

    def _update_chart_status(self, live: bool | None = None) -> None:
        """Status line under the chart: TradingView LIVE (green) or
        the built-in fallback (amber) with a Retry button — or, in
        the Vanta signals view, how many signals are plotted."""
        if self._chart_view == "signals":
            plotted = self._signals_chart.markers()
            in_range = self._signals_chart.in_range_count()
            total = len(self._signal_markers)
            self._chart_status.setText(
                f"Vanta signals — {len(plotted)} plotted "
                f"(latest {SignalChart.MAX_PLOTTED} of {in_range} in "
                f"range, {total} stored) · SL red / TP green · "
                "advisory only, shadow mode")
            self._chart_status.setStyleSheet(
                f"color: {PALETTE['muted']};")
            self._chart_retry_button.setVisible(False)
            return
        if live is None:
            live = self._tv_chart.is_live
        if live:
            self._chart_status.setText("● TradingView LIVE")
            self._chart_status.setStyleSheet(
                f"color: {STATUS_COLORS['ok']};")
            self._chart_retry_button.setVisible(False)
        else:
            # Short on purpose (v0.7.1): the status shares one row
            # with Retry + the range buttons now, so it must fit on
            # a single line instead of wrapping to two.
            self._chart_status.setText(
                "Built-in chart — TradingView could not load. "
                "Live data from stored bars.")
            self._chart_status.setStyleSheet(
                f"color: {STATUS_COLORS['stale']};")
            self._chart_retry_button.setVisible(True)

    def _on_retry_tradingview(self) -> None:
        """Retry button: reload TradingView and restart the watchdog."""
        self._chart_status.setText("TradingView reloading…")
        self._chart_status.setStyleSheet(
            f"color: {STATUS_COLORS['stale']};")
        self._chart_retry_button.setEnabled(False)
        self.log("Retrying TradingView chart…")
        self._tv_chart.reload()
        self._chart_retry_button.setEnabled(True)

    # ------------------------------------------------- chart expand / dock

    def _install_chart_click_filters(self) -> None:
        """Clicking either chart page expands it (the preview is a
        thumbnail; interaction happens in the expanded window)."""
        for i in range(self._tv_chart.count()):
            page = self._tv_chart.widget(i)
            if page is not None:
                page.installEventFilter(self)
        self._signals_chart.installEventFilter(self)

    def eventFilter(self, watched, event) -> bool:  # noqa: D102 — Qt hook
        if (self._chart_dialog is None
                and event.type() == QEvent.MouseButtonPress
                and event.button() == Qt.LeftButton):
            if watched is self._signals_chart:
                self._expand_chart()
                return True
            for i in range(self._tv_chart.count()):
                if watched is self._tv_chart.widget(i):
                    # A click on the preview opens the big window; the
                    # click itself is consumed so TradingView does not
                    # also act on it.
                    self._expand_chart()
                    return True
        return super().eventFilter(watched, event)

    def _expand_chart(self) -> None:
        """Move the chart container into a centred modeless dialog."""
        if self._chart_dialog is not None:
            self._chart_dialog.raise_()
            self._chart_dialog.activateWindow()
            return
        dialog = ChartDialog(self)
        dialog.setStyleSheet(self._stylesheet())
        dialog.closed.connect(self._on_chart_dialog_closed)
        screen = QApplication.primaryScreen().availableGeometry()
        width = min(1150, max(400, screen.width() - 40))
        height = min(720, max(300, screen.height() - 40))
        # The SAME chart widget moves — a QWebEngineView cannot live
        # in two places, so it is never duplicated.
        dialog.chart_layout.addWidget(self._chart_container)
        # Defensive: the container must be visibly shown in its new
        # home (the tab dialog needed the same explicit show() —
        # removeTab hides the page and the hidden dialog never
        # showed it back, so the dialog opened black).
        self._chart_container.show()
        self._set_chart_min_height(CHART_MIN_HEIGHT)
        # Size + centre from the ACTUAL size: on a small screen the
        # content's minimum can exceed the computed size, and Qt then
        # grows the dialog after show() while keeping its top-left —
        # leaving it off-centre (Windows CI: centre 38px right of
        # the screen centre). Use minimumSizeHint(), which is the
        # layout's total minimum size — NOT minimumWidth/Height,
        # which only return an explicitly set minimum (0 here) and
        # under-read by ~40px, so the dialog still grew after show.
        # Activate the layout first so the hint is real.
        dialog.layout().activate()
        hint = dialog.minimumSizeHint()
        width = min(max(width, hint.width()), screen.width())
        height = min(max(height, hint.height()), screen.height())
        x = screen.x() + (screen.width() - width) // 2
        y = screen.y() + (screen.height() - height) // 2
        dialog.setGeometry(x, y, width, height)
        self._chart_placeholder.setVisible(True)
        self._expand_button.setEnabled(False)
        self._chart_dialog = dialog
        dialog.show()
        # The chart just moved into a bigger viewport: force the
        # TradingView widget to re-measure (v0.7.1) once the dialog
        # has laid out, otherwise it keeps its preview-sized iframe.
        QTimer.singleShot(100, self._tv_chart.refresh_size)
        self.log("Chart expanded to a centred window.")

    def _on_chart_dialog_closed(self) -> None:
        """Dialog closed (X or Collapse): dock the chart back."""
        dialog = self._chart_dialog
        self._chart_dialog = None
        if dialog is not None:
            dialog.chart_layout.removeWidget(self._chart_container)
        layout = self._price_layout
        layout.insertWidget(layout.indexOf(self._chart_placeholder),
                            self._chart_container)
        self._set_chart_min_height(PREVIEW_MIN_HEIGHT)
        self._chart_placeholder.setVisible(False)
        self._expand_button.setEnabled(True)

    def _collapse_chart(self) -> None:
        """Placeholder button: close the dialog (its closeEvent
        docks the chart back — one code path, no reentrancy)."""
        if self._chart_dialog is not None:
            self._chart_dialog.close()

    # --------------------------------------------------- tab expand / dock

    def _on_tab_double_clicked(self, index: int) -> None:
        """Double-click on a tab bar opens that tab large."""
        if index < 0 or self._tab_dialog is not None:
            return
        for key, tab_index in self._tab_index.items():
            if tab_index == index:
                self._expand_tab(key)
                return

    def _expand_tab(self, key: str | None = None) -> None:
        """Move the current tab's page into a centred modeless
        dialog (same QScrollArea, reparented, never copied)."""
        if self._tab_dialog is not None:
            self._tab_dialog.raise_()
            self._tab_dialog.activateWindow()
            return
        if key is None:
            current = self._tabs.currentWidget()
            key = next((k for k, scroll in self._tab_pages.items()
                        if scroll is current), None)
        if key is None or key not in self._tab_pages:
            return
        scroll = self._tab_pages[key]
        index = self._tabs.indexOf(scroll)
        if index < 0:
            return
        title = self._tabs.tabText(index)
        dialog = TabDialog(title, self)
        dialog.setStyleSheet(self._stylesheet())
        dialog.closed.connect(self._on_tab_dialog_closed)
        screen = QApplication.primaryScreen().availableGeometry()
        width = min(1150, max(400, screen.width() - 40))
        height = min(720, max(300, screen.height() - 40))
        self._tabs.removeTab(index)
        dialog.tab_layout.addWidget(scroll)
        # removeTab hides the page (QStackedWidget::removeWidget)
        # and the dialog was still hidden when the page moved, so
        # the layout never showed it: without this the dialog
        # opens with an empty black area and only its Collapse
        # button visible.
        scroll.show()
        # Size + centre from the ACTUAL size (same reason as the
        # chart dialog: the content's minimum can exceed the
        # computed size on a small screen, and Qt grows the dialog
        # after show() while keeping its top-left, leaving it
        # off-centre). Use minimumSizeHint() — the layout's total
        # minimum — not minimumWidth/Height, which only return an
        # explicitly set minimum (0 here). Activate the layout
        # first so the hint is real.
        dialog.layout().activate()
        hint = dialog.minimumSizeHint()
        width = min(max(width, hint.width()), screen.width())
        height = min(max(height, hint.height()), screen.height())
        x = screen.x() + (screen.width() - width) // 2
        y = screen.y() + (screen.height() - height) // 2
        dialog.setGeometry(x, y, width, height)
        self._expanded_tab = (key, index, title)
        self._refresh_tab_index()
        self._expand_tab_button.setEnabled(False)
        self._tab_dialog = dialog
        dialog.show()
        self.log(f"Tab '{title}' expanded to a centred window.")

    def _on_tab_dialog_closed(self) -> None:
        """Dialog closed (X or Collapse): dock the page back into
        its tab, at the same position and title."""
        dialog = self._tab_dialog
        expanded = self._expanded_tab
        self._tab_dialog = None
        self._expanded_tab = None
        if dialog is not None and expanded is not None:
            key, index, title = expanded
            scroll = self._tab_pages[key]
            dialog.tab_layout.removeWidget(scroll)
            self._tabs.insertTab(index, scroll, title)
            self._tabs.setCurrentIndex(index)
        self._refresh_tab_index()
        self._expand_tab_button.setEnabled(True)

    def _refresh_tab_index(self) -> None:
        """Rebuild key -> tab index from the live tab widget.

        Indices shift when a tab page is removed/inserted for the
        expand dialog; the expanded tab has no index while it is
        out (its panels cannot be hidden from the menu meanwhile —
        _relayout_panels skips it)."""
        self._tab_index = {}
        for key, scroll in self._tab_pages.items():
            index = self._tabs.indexOf(scroll)
            if index >= 0:
                self._tab_index[key] = index

    def _collapse_tab(self) -> None:
        """Close the tab dialog (its closeEvent docks the page
        back — one code path, no reentrancy)."""
        if self._tab_dialog is not None:
            self._tab_dialog.close()

    def _outcome_text(self, signal) -> str:
        """Outcome of one stored signal, from the frozen backtest
        engine (computed in _apply_payload): "open" while it has
        none, else "TP hit +2.0R" / "SL hit −1.0R" / "expired +0.3R".
        Never invented — no TradeResult means still open."""
        trade = self._signal_outcome_by_key.get(
            (signal.direction, signal.entry, signal.generated_at))
        if trade is None:
            return "open"
        label = {"tp": "TP hit", "sl": "SL hit",
                 "expired": "expired"}.get(trade.outcome, trade.outcome)
        if trade.r_multiple is not None:
            return f"{label} {trade.r_multiple:+.1f}R"
        return label

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
        # Signal outcomes for the markers chart + Signals panel
        # (v0.8.0): the frozen backtest engine evaluates the stored
        # signals on the daily bars. A signal with no TradeResult
        # is still open — its outcome is never invented. Display
        # only; no scoring/signal rule is touched.
        self._signals_history = list(
            payload.get("signals_history", []) or [])
        self._signal_outcome_by_key: dict = {}
        try:
            prices = [(b["ts"], b["close"], b["high"], b["low"])
                      for b in self._chart_bars_daily]
            result = backtest(self._signals_history, prices)
            for trade in result.trades:
                key = (trade.direction, trade.entry, trade.generated_at)
                self._signal_outcome_by_key[key] = trade
        except Exception:
            self._signal_outcome_by_key = {}
        self._signal_markers = [
            (sig, self._signal_outcome_by_key.get(
                (sig.direction, sig.entry, sig.generated_at)))
            for sig in self._signals_history]
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
                f"{_fmt_value(past.entry)} · SL {_fmt_value(past.sl)} · "
                f"TP {_fmt_value(past.tp)} · "
                f"{self._outcome_text(past)} · {past.reason}"))

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
        # Pills in the Technical Summary panel (v0.6.0) — the same
        # read as the summary strip, shown next to the detail lines.
        if tech and tech.get("bars_daily"):
            signal = str(tech.get("signal", "neutral"))
            _set_pill(self._tech_signal_pill, signal.upper(),
                      SIGNAL_PILL_KIND.get(signal.lower(), "grey"))
            rf = tech.get("regime_filter") or {}
            label = str(rf.get("label") or "")
            if label and label != "unknown":
                _set_pill(self._tech_regime_pill, label.upper(),
                          REGIME_PILL_KIND.get(label, "grey"))
            else:
                _set_pill(self._tech_regime_pill, "—", "grey")
        else:
            _set_pill(self._tech_signal_pill, "—", "grey")
            _set_pill(self._tech_regime_pill, "—", "grey")
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

        # Summary strip (v0.6.0): the whole analysis at a glance.
        self._update_summary_strip(payload)

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
        # Dock the chart back first: the expand dialog is a child, so
        # it would close anyway — but closing it explicitly re-parents
        # the chart container into the preview (no dangling widget).
        if self._chart_dialog is not None:
            self._chart_dialog.close()
        # Same for the expanded tab dialog (v0.8.0): closing it
        # docks the tab page back into its tab.
        if self._tab_dialog is not None:
            self._tab_dialog.close()
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
