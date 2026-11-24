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
    so. Nothing is ever a blank panel. (v0.8.0: the watchdog also
    verifies the iframe RENDERS tall — an iframe that exists but is
    ~0px tall used to count as live and show a blank white box.)

The fallback widget is supplied by the caller (the existing
PriceChart) and lives on the second page of a QStackedWidget.
"""

from __future__ import annotations

import json

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtWidgets import QLabel, QSizePolicy, QStackedWidget, QWidget

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

    Sizing (v0.7.1 — this was the collapsed-chart bug): the embed
    script sets the widget container's height to the settings'
    ``"height": "100%"`` and the iframe to ``height: 100%`` of it,
    but it never listens for resize and never re-measures. A
    percentage height only resolves when every ancestor has a
    definite height — the old HTML set no height on ``html`` /
    ``body``, so the whole chain collapsed and the iframe fell back
    to the HTML default of 150px, however tall the QWebEngineView
    was. With ``html, body`` at ``height: 100%`` the container and
    the iframe resolve against the viewport and track resizes in
    pure CSS (``refresh_size`` below is the belt-and-braces for
    reparenting). ``100vh``/``100vw`` are deliberately NOT used:
    the script overwrites the container height with ``100%``
    anyway, and ``100vw`` can overflow horizontally.
    """
    light = theme == "light"
    bg = "#ffffff" if light else "#131722"
    bg_rgba = "rgba(255, 255, 255, 1)" if light else "rgba(19, 23, 34, 1)"
    grid = "rgba(46, 46, 46, 0.12)" if light else "rgba(46, 46, 46, 0.06)"
    tv_theme = "light" if light else "dark"
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>
html,body{{height:100%;margin:0;padding:0;overflow:hidden;background:{bg};}}
.tradingview-widget-container{{height:100%;width:100%;}}
</style></head>
<body>
<div class="tradingview-widget-container">
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
# PREVIEW_MIN_HEIGHT and set_min_height(). v0.7.1: preview 200px
# (was 240) so the tab content below gets real room on a 1366x768
# screen.
CHART_MIN_HEIGHT = 480
PREVIEW_MIN_HEIGHT = 200

# JS run by refresh_size(): force the widget container and its
# iframe to the current viewport height in pixels, then dispatch a
# resize event. The embed script never re-measures on its own (no
# resize listener, no clientHeight reads — verified against the
# script served by s3.tradingview.com), so this is what makes the
# chart actually fill the view after a resize or a reparent into
# the expand dialog.
#
# v0.8.0 — this JS was the blank-chart bug (v0.7.1): it subtracted
# the offsetHeight of EVERY non-iframe child of the container. The
# container's children are the #tv_chart wrapper DIV (empty — the
# embed script appends the iframe as a direct child of the
# container, not inside the wrapper) with height:100%, the <script>
# tag, and the injected <style> tag. So "other" was ~= the viewport
# height and the iframe got max(0, h - h) = 0px — the chart was
# actively zeroed the moment the iframe appeared, while the status
# stayed "LIVE" because the iframe existed. Now only the
# .tradingview-widget-copyright bar (32px, present only when the
# markup carries a copyright element — ours does not, so the
# subtraction is 0) is ever subtracted; the wrapper div is never
# subtracted. An iframe that renders ~0px is not live — see
# _VISIBILITY_JS and _iframe_visibly_live() below.
_RESIZE_JS = """(function(){
  var c = document.querySelector('.tradingview-widget-container');
  if (!c) return JSON.stringify({ok:false,reason:'no-container'});
  var h = window.innerHeight || document.documentElement.clientHeight || 0;
  c.style.height = h + 'px'; c.style.width = '100%';
  var w = document.getElementById('tv_chart');
  if (w) { w.style.height = h + 'px'; w.style.width = '100%'; }
  var copy = c.querySelector('.tradingview-widget-copyright');
  var other = copy ? (copy.offsetHeight || 0) : 0;
  var f = c.querySelector('iframe');
  var ih = 0;
  if (f) {
    f.style.height = Math.max(0, h - other) + 'px';
    f.style.width = '100%';
    ih = f.clientHeight;
  }
  window.dispatchEvent(new Event('resize'));
  return JSON.stringify({ok:true, viewport:h, iframe:ih});
})()"""

# JS run by the watchdog to check the iframe is VISIBLY live: an
# iframe that exists but renders ~0px tall (a sizing bug) must never
# count as live — the built-in chart is always preferable to a
# blank white box. Returns {iframe, viewport} in CSS pixels.
_VISIBILITY_JS = """(function(){
  var f = document.querySelector('iframe');
  var h = window.innerHeight || document.documentElement.clientHeight || 0;
  return JSON.stringify({iframe: f ? (f.clientHeight || 0) : 0,
                         viewport: h});
})()"""


def _iframe_visibly_live(viewport: float, iframe: float) -> bool:
    """True when the iframe renders a real fraction of the view.

    Existence is not liveness: a 0px iframe (sizing bug) must fall
    back, not show a blank box. The bar is >50% of the viewport so
    a half-rendered widget still counts, but a collapsed one never
    does. Pure function — unit-tested directly.
    """
    try:
        viewport = float(viewport or 0)
        iframe = float(iframe or 0)
    except (TypeError, ValueError):
        return False
    return viewport > 100 and iframe > 0.5 * viewport


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
        # v0.7.1: a QWebEngineView reports an 800x600 sizeHint. With
        # a normal policy the dashboard layout hands the price panel
        # ~600px and squeezes the tabs to ~100px at 1366x768 — the
        # tabs were cut off on the user's screen even after the
        # panel was compacted, because the web view's sizeHint, not
        # the panel chrome, was eating the space. Ignored vertically:
        # layouts use the 200/480px minimum instead, and the chart
        # still expands to fill whatever the panel gives it.
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Ignored)
        self._fallback = fallback
        self._fallback.setMinimumHeight(CHART_MIN_HEIGHT)
        self._live = False
        self._elapsed = 0
        self._theme = theme if theme in ("dark", "light") else "dark"
        self._html = html if html is not None else _widget_html(self._theme)
        self._view = None
        # refresh_size() bookkeeping (v0.7.1): one probe-triggered
        # reload per page load at most, and a debounced resize hook.
        self._probe_reloaded = False
        self._resize_pending = False
        self._shown_once = False
        # Visibility probe (v0.8.0): one in flight at a time — the
        # watchdog ticks every second and must not stack probes.
        self._visibility_pending = False
        if WEBENGINE_AVAILABLE:
            self._view = QWebEngineView(self)
            self._view.setMinimumHeight(CHART_MIN_HEIGHT)
            self._view.setSizePolicy(
                QSizePolicy.Expanding, QSizePolicy.Ignored)
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
        """True once the TradingView iframe is visibly live (renders
        a real fraction of the view — existence alone is not live)."""
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
        # New page load: a probe-triggered reload is allowed again.
        self._probe_reloaded = False
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
        self._visibility_pending = False
        if self._view is None:
            self.show_fallback()
            return
        self.setCurrentIndex(0)
        self._view.setHtml(self._html,
                           baseUrl="https://www.tradingview.com/")
        if hasattr(self, "_watchdog"):
            self._watchdog.start()

    def refresh_size(self) -> None:
        """Force the TradingView widget to fill the current view.

        Runs _RESIZE_JS in the page: the widget container and its
        iframe get the viewport height in pixels, and a resize event
        is dispatched. No-op without a web view, or while the view
        is still (near) zero-sized — sizing a 0px viewport would
        collapse the chart to nothing. Called debounced from
        resizeEvent, once on first show, when the iframe first goes
        live, and after reparenting into the expand dialog.
        """
        if self._view is None or self.height() < 50:
            return
        self._view.page().runJavaScript(
            _RESIZE_JS, self._on_refresh_size_result)

    def _on_refresh_size_result(self, result) -> None:
        """Probe after refresh_size(): if the iframe is still far
        below the viewport, reload the HTML at the current size.

        At most one probe-triggered reload per page load — reload()
        is the last resort, not a loop. A fresh load at the right
        size (with the fixed html/body heights) fills the view.
        """
        try:
            data = json.loads(result) if isinstance(result, str) else {}
        except (TypeError, ValueError):
            return
        if not isinstance(data, dict) or not data.get("ok"):
            return
        viewport = data.get("viewport") or 0
        iframe = data.get("iframe") or 0
        if (viewport > 100 and iframe < 0.8 * viewport
                and not self._probe_reloaded):
            self._probe_reloaded = True
            self.reload()

    def resizeEvent(self, event) -> None:  # noqa: D102 — Qt hook
        super().resizeEvent(event)
        if self._view is None or self._resize_pending:
            return
        self._resize_pending = True
        QTimer.singleShot(150, self._on_resize_debounced)

    def _on_resize_debounced(self) -> None:
        self._resize_pending = False
        self.refresh_size()

    def showEvent(self, event) -> None:  # noqa: D102 — Qt hook
        super().showEvent(event)
        if self._shown_once:
            return
        self._shown_once = True
        QTimer.singleShot(100, self.refresh_size)

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
            # v0.8.0: existence is NOT liveness. An iframe that
            # renders ~0px (a sizing bug) used to count as live and
            # show a blank white box. Now the visibility probe must
            # confirm a real rendered height first; until then the
            # watchdog keeps ticking (and refresh_size() keeps
            # trying to fix the size) instead of declaring live.
            if self._live:
                return
            if not self._visibility_pending:
                self._visibility_pending = True
                self._view.page().runJavaScript(
                    _VISIBILITY_JS, self._on_visibility_result)
            return
        if self._elapsed >= WATCHDOG_SECONDS:
            self.show_fallback()

    def _on_visibility_result(self, result) -> None:
        """Watchdog visibility probe: only a visibly-tall iframe is
        live. Too short -> try to fix the size and keep waiting;
        still short when the watchdog expires -> fall back to the
        built-in chart (never a blank box)."""
        self._visibility_pending = False
        try:
            data = json.loads(result) if isinstance(result, str) else {}
        except (TypeError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        if _iframe_visibly_live(data.get("viewport") or 0,
                                data.get("iframe") or 0):
            if not self._live:
                self._live = True
                self.status_changed.emit(True)
                # The iframe just went visibly live — force it to
                # fill the view now, not at whatever size it first
                # measured.
                self.refresh_size()
            if hasattr(self, "_watchdog"):
                self._watchdog.stop()
            return
        # Found but not visibly live: try to fix the size, keep
        # waiting for the watchdog to expire.
        self.refresh_size()
        if self._elapsed >= WATCHDOG_SECONDS:
            self.show_fallback()
