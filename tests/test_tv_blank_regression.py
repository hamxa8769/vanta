"""TradingView blank-chart regression (v0.8.0) — headless only.

Bug (v0.7.1): the chart went COMPLETELY blank on the user's
Windows — white area, status "TradingView LIVE". Root cause:
``_RESIZE_JS`` subtracted the offsetHeight of EVERY non-iframe
child of the container. The container's children are the
``#tv_chart`` wrapper DIV (empty — the embed script appends the
iframe as a direct child of the container, not inside the
wrapper) with ``height:100%``, so "other" ~= the viewport height
and the iframe got ``max(0, h - h) = 0px`` — actively zeroed the
moment the iframe appeared, while the status stayed LIVE because
the iframe existed. These tests load a MOCK embed page with the
EXACT real structure (local HTML — no network, so it loads in
this VM) in the real QWebEngineView and assert the iframe stays
tall after refresh_size(), and that a 0-height iframe falls back
instead of showing a blank box.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS",
                      "--no-sandbox --disable-gpu")

import json
import time

import pytest

pytest.importorskip("PySide6")

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel

from goldboard_app.tradingview_chart import (
    _RESIZE_JS,
    _iframe_visibly_live,
    TradingViewChart,
)

# Mock of the REAL embed structure (verified against the script
# served by s3.tradingview.com): the container holds the empty
# #tv_chart wrapper (height:100%), the iframe as a DIRECT child
# (height calc(100% - 32px) — the script subtracts the 32px
# copyright bar when one is present), and the copyright div.
# Local HTML only — no tradingview.com, so it loads in this VM.
MOCK_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8"><style>
html,body{height:100%;margin:0;padding:0;overflow:hidden;background:#131722;}
.tradingview-widget-container{height:100%;width:100%;}
</style></head>
<body>
<div class="tradingview-widget-container">
<div id="tv_chart" style="height:100%;width:100%;"></div>
<iframe src="about:blank" style="height:calc(100% - 32px);width:100%;"></iframe>
<div class="tradingview-widget-copyright" style="height:32px;line-height:32px;text-align:center;">TradingView</div>
</div></body></html>"""

# Same structure, but the container never renders: the iframe
# exists (count > 0) yet its clientHeight stays 0 forever, and no
# amount of refresh_size() can fix it — the watchdog must fall
# back instead of declaring it live.
MOCK_ZERO_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8"><style>
html,body{height:100%;margin:0;padding:0;overflow:hidden;background:#131722;}
.tradingview-widget-container{height:100%;width:100%;}
</style></head>
<body>
<div class="tradingview-widget-container" style="display:none">
<div id="tv_chart" style="height:100%;width:100%;"></div>
<iframe src="about:blank" style="height:100%;width:100%;"></iframe>
</div></body></html>"""

_MEASURE_JS = (
    "JSON.stringify({iframe: "
    "(document.querySelector('iframe')||{}).clientHeight || 0,"
    " viewport: window.innerHeight || 0})")


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _wait_for(condition, timeout_ms: int = 10000,
              step_ms: int = 100) -> bool:
    """Poll `condition` while letting the event loop run.

    (This PySide6 has no QTest.qWaitFor — the existing tests use
    QTest.qWait sleeps instead.)"""
    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        if condition():
            return True
        QTest.qWait(step_ms)
    return bool(condition())


def _iframe_size(chart, qapp, timeout_ms: int = 5000) -> dict:
    """Read the mock page's iframe/viewport heights (real page)."""
    results: list[dict] = []
    assert chart._view is not None
    chart._view.page().runJavaScript(
        _MEASURE_JS,
        lambda r: results.append(json.loads(r) if r else {}))
    assert _wait_for(lambda: bool(results), timeout_ms), \
        "measure JS returned nothing"
    return results[0]


def test_resize_js_never_subtracts_the_wrapper():
    """v0.8.0: the sizing JS must not sum non-iframe children — the
    #tv_chart wrapper alone is ~viewport tall. Only the copyright
    bar (32px, when present) may be subtracted."""
    assert "c.children" not in _RESIZE_JS
    assert "tradingview-widget-copyright" in _RESIZE_JS
    assert "innerHeight" in _RESIZE_JS
    assert "resize" in _RESIZE_JS


def test_iframe_visibly_live_threshold():
    """v0.8.0: existence is not liveness — a 0px or half-height
    iframe is not live; a tall one is."""
    assert _iframe_visibly_live(480, 448) is True
    assert _iframe_visibly_live(480, 300) is True
    assert _iframe_visibly_live(480, 240) is False
    assert _iframe_visibly_live(480, 0) is False
    assert _iframe_visibly_live(50, 50) is False
    assert _iframe_visibly_live(0, 0) is False


def test_refresh_size_keeps_mock_iframe_tall(qapp):
    """v0.8.0 REGRESSION: with the exact real embed structure,
    refresh_size() must keep the iframe tall — the old JS zeroed
    it (iframe height 0px, status still LIVE, blank white box)."""
    fallback = QLabel("built-in chart")
    chart = TradingViewChart(fallback, html=MOCK_HTML)
    chart.resize(400, 300)
    chart.show()
    qapp.processEvents()
    # The watchdog's visibility probe must declare it live.
    assert _wait_for(lambda: chart.is_live, 10000), \
        "mock iframe never went live"
    assert chart.currentIndex() == 0
    before = _iframe_size(chart, qapp)
    assert before["viewport"] > 100
    assert before["iframe"] > 0.8 * before["viewport"], before
    # The regression: refresh_size() used to zero the iframe here.
    chart.refresh_size()
    QTest.qWait(300)
    qapp.processEvents()
    after = _iframe_size(chart, qapp)
    assert after["iframe"] > 0.8 * after["viewport"], after
    chart.close()


def test_zero_height_iframe_falls_back_not_blank(qapp, monkeypatch):
    """v0.8.0: an iframe that exists but renders 0px must fall back
    to the built-in chart (status False) — never a blank box that
    claims to be LIVE."""
    import goldboard_app.tradingview_chart as tvc

    monkeypatch.setattr(tvc, "WATCHDOG_SECONDS", 3)
    fallback = QLabel("built-in chart")
    chart = TradingViewChart(fallback, html=MOCK_ZERO_HTML)
    chart.resize(400, 300)
    chart.show()
    qapp.processEvents()
    assert _wait_for(lambda: chart.currentIndex() == 1, 10000), \
        "0-height iframe did not fall back"
    assert chart.is_live is False
    chart.close()
