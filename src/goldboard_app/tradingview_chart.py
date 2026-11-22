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

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtWidgets import QLabel, QStackedWidget, QWidget

try:
    from PySide6.QtWebEngineWidgets import QWebEngineView

    WEBENGINE_AVAILABLE = True
except Exception:  # pragma: no cover — WebEngine not installed
    QWebEngineView = None  # type: ignore[assignment, misc]
    WEBENGINE_AVAILABLE = False

def _widget_html(theme: str = "dark") -> str:
    """TradingView embed HTML for the given theme.

    Dark matches the board's black + gold; light is TradingView's
    own light theme (white background) so the chart never looks
    like a dark hole inside a light dashboard.
    """
    light = theme == "light"
    bg = "#ffffff" if light else "#131722"
    bg_rgba = "rgba(255, 255, 255, 1)" if light else "rgba(19, 23, 34, 1)"
    grid = "rgba(46, 46, 46, 0.12)" if light else "rgba(46, 46, 46, 0.06)"
    tv_theme = "light" if light else "dark"
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"></head>
<body style="margin:0;background:{bg};">
<div class="tradingview-widget-container" style="height:100vh;width:100vw;">
<div id="tv_chart" style="height:100%;width:100%;"></div>
<script src="https://s3.tradingview.com/external-embedding/embed-widget-advanced-chart.js" async>
{{"symbol": "OANDA:XAUUSD", "theme": "{tv_theme}", "interval": "D",
 "hide_side_toolbar": false, "allow_symbol_change": true,
 "studies": ["Volume@tv-basicstudies"], "locale": "en",
 "backgroundColor": "{bg_rgba}",
 "gridColor": "{grid}", "width": "100%", "height": "100%"}}
</script></div></body></html>"""


WIDGET_HTML = _widget_html("dark")

# How long to wait for the widget's iframe before falling back.
WATCHDOG_SECONDS = 20

# Both pages must be this tall in the EXPANDED view: the fallback
# chart used to have no minimum height at all, so when TradingView
# failed to load the whole panel collapsed and the chart looked
# invisible. The stacked widget itself gets the same minimum so the
# panel never shrinks. The dashboard preview is smaller — see
# PREVIEW_MIN_HEIGHT and set_min_height().
CHART_MIN_HEIGHT = 480
PREVIEW_MIN_HEIGHT = 240


class TradingViewChart(QStackedWidget):
    """TradingView chart with a built-in fallback.

    ``fallback`` is the widget shown when TradingView cannot load
    (the existing PriceChart). Page 0 = web view, page 1 = fallback.

    ``status_changed`` fires True when the TradingView iframe is
    live and False when the widget falls back, so the window can
    show an honest status line + a Retry button.
    """

    status_changed = Signal(bool)

    def __init__(self, fallback: QWidget, parent: QWidget | None = None,
                 html: str | None = None, theme: str = "dark") -> None:
        super().__init__(parent)
        self.setMinimumHeight(CHART_MIN_HEIGHT)
        self._fallback = fallback
        self._fallback.setMinimumHeight(CHART_MIN_HEIGHT)
        self._live = False
        self._elapsed = 0
        self._theme = theme if theme in ("dark", "light") else "dark"
        self._html = html if html is not None else _widget_html(self._theme)
        self._view = None
        if WEBENGINE_AVAILABLE:
            self._view = QWebEngineView(self)
            self._view.setMinimumHeight(CHART_MIN_HEIGHT)
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

    def set_theme(self, theme: str) -> None:
        """Switch the TradingView embed theme and reload it.

        The fallback chart needs no reload — it paints from the
        app palette, which the window re-applies separately. With
        no WebEngine there is nothing to reload; the fallback stays.
        """
        theme = theme if theme in ("dark", "light") else "dark"
        if theme == self._theme:
            return
        self._theme = theme
        self._html = _widget_html(theme)
        self.reload()

    def set_min_height(self, height: int) -> None:
        """Minimum height for the stacked widget and BOTH pages.

        The dashboard preview uses PREVIEW_MIN_HEIGHT; the expanded
        dialog uses CHART_MIN_HEIGHT. Setting it on the pages (not
        just the stack) is what keeps the fallback from collapsing.
        """
        self.setMinimumHeight(height)
        for i in range(self.count()):
            page = self.widget(i)
            if page is not None:
                page.setMinimumHeight(height)

    @property
    def theme(self) -> str:
        """Current embed theme ("dark" / "light")."""
        return self._theme

    def reload(self) -> None:
        """Retry TradingView: reset the watchdog and load it again.

        With no WebEngine there is nothing to reload — the fallback
        stays. The window shows "reloading…" until the watchdog
        either finds the iframe (live) or falls back again.
        """
        self._live = False
        self._elapsed = 0
        if self._view is None:
            self.show_fallback()
            return
        self.setCurrentIndex(0)
        self._view.setHtml(self._html,
                           baseUrl="https://www.tradingview.com/")
        if hasattr(self, "_watchdog"):
            self._watchdog.start()

    def show_fallback(self) -> None:
        """Switch to the built-in chart (and stop the watchdog)."""
        was_on_tv = self.currentIndex() == 0
        if hasattr(self, "_watchdog"):
            self._watchdog.stop()
        self._live = False
        self.setCurrentIndex(1)
        if was_on_tv:
            self.status_changed.emit(False)

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
            if not self._live:
                self._live = True
                self.status_changed.emit(True)
            if hasattr(self, "_watchdog"):
                self._watchdog.stop()
        elif self._elapsed >= WATCHDOG_SECONDS:
            self.show_fallback()
