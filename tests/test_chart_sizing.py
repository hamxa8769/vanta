"""TradingView chart sizing (v0.7.1) — headless only.

Bug 1: the live TradingView chart rendered as a thin ~150px band
at the top of its container (preview AND expanded dialog), with a
huge empty area below. Root cause, verified against the embed
script served by s3.tradingview.com: the script sets the widget
container to ``height: 100%`` and the iframe to ``height: 100%``
of it, but never listens for resize and never re-measures — and
our HTML set no height on ``html``/``body``, so the percentage
chain collapsed and the iframe fell back to the HTML default of
150px. The fix: ``html, body`` at ``height: 100%`` (percentages
resolve, the iframe fills and tracks resizes in pure CSS) plus
``refresh_size()`` — JS that forces container + iframe to the
viewport height in pixels — on resize, first show, when the
iframe goes live, and after reparenting into the expand dialog.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS",
                      "--no-sandbox --disable-gpu")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QLabel

from goldboard_app.tradingview_chart import (
    PREVIEW_MIN_HEIGHT,
    TradingViewChart,
    _widget_html,
)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


class _FakePage:
    """Stand-in for QWebEnginePage: records scripts, answers the
    refresh_size() probe with a canned JSON result."""

    def __init__(self, result: str):
        self.scripts: list[str] = []
        self.result = result

    def runJavaScript(self, script, callback=None):
        self.scripts.append(script)
        if callback is not None:
            callback(self.result)


def _chart_with_fake_page(qapp, result: str):
    """TradingViewChart with a fake page; resized (not shown) so
    refresh_size() is not skipped by its <50px guard. Not shown:
    showEvent would schedule its own refresh_size() timer and
    muddy the recorded scripts."""
    fallback = QLabel("built-in chart")
    chart = TradingViewChart(fallback)
    chart.resize(400, 300)
    assert chart.height() >= 50
    # Stop the watchdog: its 1s tick would otherwise run the same
    # fake page's runJavaScript (iframe-count probe) and muddy the
    # recorded scripts.
    if hasattr(chart, "_watchdog"):
        chart._watchdog.stop()
    fake = _FakePage(result)
    assert chart._view is not None
    chart._view.page = lambda: fake  # type: ignore[method-assign]
    return chart, fake


def test_embed_html_has_definite_height_chain():
    """v0.7.1: html/body carry height:100% so the embed's 100%
    heights resolve; no 100vh/100vw (the script overwrites the
    container height with 100% anyway)."""
    for theme in ("dark", "light"):
        html = _widget_html(theme)
        assert "html,body" in html
        assert "height:100%" in html
        assert "100vh" not in html
        assert "100vw" not in html


def test_preview_min_height_is_200():
    """v0.7.1: preview 240px -> 200px, so the tabs below get room."""
    assert PREVIEW_MIN_HEIGHT == 200


def test_web_view_size_hint_does_not_eat_the_tabs(qapp):
    """v0.7.1: a QWebEngineView reports an 800x600 sizeHint; with a
    normal policy the dashboard layout hands the price panel ~600px
    and squeezes the tabs to ~100px at 1366x768. The chart (and its
    web view) must be vertically Ignored so layouts use the
    200/480px minimum instead."""
    from PySide6.QtWidgets import QSizePolicy

    fallback = QLabel("built-in chart")
    chart = TradingViewChart(fallback)
    assert chart.sizePolicy().verticalPolicy() == QSizePolicy.Ignored
    if chart._view is not None:
        assert chart._view.sizePolicy().verticalPolicy() == QSizePolicy.Ignored
    chart.close()


def test_refresh_size_runs_sizing_js(qapp):
    """v0.7.1: refresh_size() runs JS that sets the container and
    iframe to the viewport height and dispatches a resize event."""
    chart, fake = _chart_with_fake_page(
        qapp, '{"ok":true,"viewport":500,"iframe":500}')
    chart.refresh_size()
    assert fake.scripts
    script = fake.scripts[0]
    assert "tradingview-widget-container" in script
    assert "innerHeight" in script
    assert "iframe" in script
    assert "resize" in script
    chart.close()


def test_refresh_size_reloads_once_when_iframe_stays_short(qapp, monkeypatch):
    """v0.7.1: if the probe finds the iframe far below the viewport
    after the resize JS, refresh_size() reloads the HTML once —
    and only once, so it can never loop."""
    chart, fake = _chart_with_fake_page(
        qapp, '{"ok":true,"viewport":500,"iframe":100}')
    reloads: list[None] = []
    monkeypatch.setattr(chart, "reload", lambda: reloads.append(None))
    chart.refresh_size()
    assert len(reloads) == 1
    chart.refresh_size()
    assert len(reloads) == 1
    chart.close()


def test_refresh_size_skips_zero_sized_view(qapp, monkeypatch):
    """v0.7.1: refresh_size() must not size a 0px viewport — that
    would collapse the chart to nothing. The height guard is
    unit-tested by faking a tiny height (a real stacked widget
    cannot be resized below its 200px minimum)."""
    fallback = QLabel("built-in chart")
    chart = TradingViewChart(fallback)
    chart.resize(400, 300)
    monkeypatch.setattr(chart, "height", lambda: 10)
    calls: list[str] = []

    class _RecordingPage(_FakePage):
        def runJavaScript(self, script, callback=None):
            calls.append(script)

    assert chart._view is not None
    chart._view.page = lambda: _RecordingPage("{}")  # type: ignore[method-assign]
    chart.refresh_size()
    assert calls == []
    chart.close()


def test_fallback_fills_its_page_without_webengine(monkeypatch, qapp):
    """v0.7.1: without PySide6-WebEngine the built-in chart fills
    the stacked widget at both sizes (it already did — verify)."""
    import goldboard_app.tradingview_chart as tvc

    monkeypatch.setattr(tvc, "WEBENGINE_AVAILABLE", False)
    fallback = QLabel("built-in chart")
    chart = TradingViewChart(fallback)
    chart.resize(400, 300)
    chart.show()
    qapp.processEvents()
    assert chart.currentWidget() is fallback
    assert chart.currentWidget().height() == chart.height()
    # The preview path lowers both pages to 200px (they start at
    # CHART_MIN_HEIGHT 480, the expanded size).
    chart.set_min_height(PREVIEW_MIN_HEIGHT)
    assert fallback.minimumHeight() == PREVIEW_MIN_HEIGHT
    assert chart.minimumHeight() == PREVIEW_MIN_HEIGHT
    chart.close()


def test_expand_dialog_calls_refresh_size_after_reparenting(
        tmp_path, monkeypatch, qapp):
    """v0.7.1: opening the expand dialog calls refresh_size() on
    the SAME chart widget after it is reparented into the dialog
    (the 100ms delayed call), so the iframe is re-sized to the
    dialog's viewport instead of staying at the preview height."""
    from PySide6.QtTest import QTest

    from tests.test_ui import _open_window

    window = _open_window(tmp_path, monkeypatch, qapp)
    calls: list[None] = []
    original = window._tv_chart.refresh_size

    def _spy():
        calls.append(None)
        original()

    monkeypatch.setattr(window._tv_chart, "refresh_size", _spy)
    window._expand_chart()
    qapp.processEvents()
    assert window._chart_dialog is not None
    QTest.qWait(250)
    qapp.processEvents()
    assert calls, "refresh_size() was not called after reparenting"
    window._chart_dialog.close()
    qapp.processEvents()
    window.close()
