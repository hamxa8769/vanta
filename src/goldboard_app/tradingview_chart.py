"""Real TradingView candlestick chart inside Vanta (v0.4.0).

The Advanced Real-Time Chart widget (s3.tradingview.com) runs in a
QWebEngineView, symbol OANDA:XAUUSD (spot gold), dark theme, with
the Volume study. It is the genuine TradingView chart — drawing
tools, timeframes and symbol change all work as on the site.

It needs two things the app cannot guarantee, so it degrades
gracefully instead of breaking:
  * PySide6-WebEngine installed — if the import fails, the widget
    shows the built-in QPainter chart instead.
  * Internet at runtime — the widget loads its JS from TradingView.
    A watchdog waits up to 20s for the widget's iframe to appear;
    if it never does (offline, or a network that blocks
    tradingview.com), the built-in chart takes over and the log says
    so. Nothing is ever a blank panel.

The fallback widget is supplied by the caller (the existing
PriceChart) and lives on the second page of a QStackedWidget.
"""

from __future__ import annotations

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import QLabel, QStackedWidget, QWidget

try:
    from PySide6.QtWebEngineWidgets import QWebEngineView

    WEBENGINE_AVAILABLE = True
except Exception:  # pragma: no cover — WebEngine not installed
    QWebEngineView = None  # type: ignore[assignment, misc]
    WEBENGINE_AVAILABLE = False

WIDGET_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8"></head>
<body style="margin:0;background:#131722;">
<div class="tradingview-widget-container" style="height:100vh;width:100vw;">
<div id="tv_chart" style="height:100%;width:100%;"></div>
<script src="https://s3.tradingview.com/external-embedding/embed-widget-advanced-chart.js" async>
{"symbol": "OANDA:XAUUSD", "theme": "dark", "interval": "D",
 "hide_side_toolbar": false, "allow_symbol_change": true,
 "studies": ["Volume@tv-basicstudies"], "locale": "en",
 "backgroundColor": "rgba(19, 23, 34, 1)",
 "gridColor": "rgba(46, 46, 46, 0.06)", "width": "100%", "height": "100%"}
</script></div></body></html>"""

# How long to wait for the widget's iframe before falling back.
WATCHDOG_SECONDS = 20


class TradingViewChart(QStackedWidget):
    """TradingView chart with a built-in fallback.

    ``fallback`` is the widget shown when TradingView cannot load
    (the existing PriceChart). Page 0 = web view, page 1 = fallback.
    """

    def __init__(self, fallback: QWidget, parent: QWidget | None = None,
                 html: str = WIDGET_HTML) -> None:
        super().__init__(parent)
        self._fallback = fallback
        self._live = False
        self._elapsed = 0
        self._html = html
        self._view = None
        if WEBENGINE_AVAILABLE:
            self._view = QWebEngineView(self)
            self._view.setMinimumHeight(380)
            self.addWidget(self._view)
            self.addWidget(self._fallback)
            self.setCurrentIndex(0)
            self._view.loadFinished.connect(self._on_load_finished)
            self._view.setHtml(self._html,
                               baseUrl="https://www.tradingview.com/")
            self._watchdog = QTimer(self)
            self._watchdog.setInterval(1000)
            self._watchdog.timeout.connect(self._on_watchdog_tick)
            self._watchdog.start()
        else:  # pragma: no cover — no WebEngine installed
            self.addWidget(QLabel("TradingView needs PySide6-WebEngine — "
                                  "showing the built-in chart.", self))
            self.addWidget(self._fallback)
            self.setCurrentIndex(1)

    @property
    def is_live(self) -> bool:
        """True once the TradingView iframe has appeared."""
        return self._live

    def show_fallback(self) -> None:
        """Switch to the built-in chart (and stop the watchdog)."""
        if hasattr(self, "_watchdog"):
            self._watchdog.stop()
        self._live = False
        self.setCurrentIndex(1)

    def _on_load_finished(self, ok: bool) -> None:
        if not ok:
            self.show_fallback()

    def _on_watchdog_tick(self) -> None:
        self._elapsed += 1
        if self._view is None:
            self.show_fallback()
            return
        self._view.page().runJavaScript(
            "document.querySelectorAll('iframe').length",
            self._on_iframe_count)

    def _on_iframe_count(self, count) -> None:
        try:
            found = int(count or 0) > 0
        except (TypeError, ValueError):
            found = False
        if found:
            self._live = True
            if hasattr(self, "_watchdog"):
                self._watchdog.stop()
        elif self._elapsed >= WATCHDOG_SECONDS:
            self.show_fallback()
