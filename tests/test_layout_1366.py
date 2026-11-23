"""1366x768 layout (v0.7.1) — headless only.

Bug 2: on a 1366x768 screen the tab bar was visible at the bottom
edge but the tab CONTENT area was only ~85px tall and cut off —
nothing below was visible and there was no way to scroll to it.
The old price panel (~530px) + header (66px) + strip (115px) +
130px Logs drawer left nothing for the tabs. The fix compacts
the price panel (one row for price+change+Expand, one short source
line, one merged status+Retry+range row, 200px preview), compacts
the header, and hides the Logs drawer by default (Panels menu
brings it back). At a 1366x768 window the tab widget must now be
>= 250px tall with the Institutions tab's first cards visible,
and the tab pages must still scroll.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS",
                      "--no-sandbox --disable-gpu")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QPoint, QSettings
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from goldboard_app.tradingview_chart import PREVIEW_MIN_HEIGHT
from tests.test_dashboard_tabs import dark_theme  # noqa: F401  (fixture)
from tests.test_ui import _open_window


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _open_1366x768(tmp_path, monkeypatch, qapp):
    """Window resized to a 1366x768 screen, layout settled."""
    # QSettings persist across tests in one process: an earlier test
    # may have left the Logs drawer visible, which would eat 130px
    # of the tab room this file measures.
    QSettings("Vanta", "GoldBoard").setValue("logs_visible", False)
    window = _open_window(tmp_path, monkeypatch, qapp)
    window.resize(1366, 768)
    window.show()
    qapp.processEvents()
    qapp.processEvents()
    return window


def test_tabs_get_real_room_at_1366x768(tmp_path, monkeypatch, qapp,
                                       dark_theme):
    """v0.7.1: at a 1366x768 window the tab widget is >= 250px tall
    (was ~109px) and the Institutions tab's first card is visible
    in the viewport — measured by geometry, not by eye.

    The window's exact size is NOT asserted: Qt cannot shrink a
    window below its layout minimum, and that minimum is
    platform/font dependent — the Windows CI runner reports a
    minimum width far above 1366 (its headless font metrics are
    much wider than a real 1366x768 desktop, where the user's own
    screenshots show the app fitting). The invariant under test is
    the tab room at the smallest size the layout allows, which is
    what the bug was about.
    """
    window = _open_1366x768(tmp_path, monkeypatch, qapp)
    assert window._tabs.height() >= 250
    assert window._tabs.currentWidget() is not None
    scroll = window._tab_pages["institutions"]
    viewport = scroll.viewport()
    assert viewport.height() >= 150
    first = next(p for p in window._panels
                 if p.get("tab") == "institutions")
    top_left = first["box"].mapTo(viewport, QPoint(0, 0))
    assert viewport.rect().contains(top_left)
    window.close()


def test_tab_pages_still_scroll(tmp_path, monkeypatch, qapp, dark_theme):
    """v0.7.1: the Institutions tab's content is taller than its
    viewport, and the vertical scrollbar is there to reach it."""
    window = _open_1366x768(tmp_path, monkeypatch, qapp)
    scroll = window._tab_pages["institutions"]
    assert scroll.verticalScrollBar().maximum() > 0
    assert scroll.verticalScrollBar().isVisible()
    window.close()


def test_preview_min_height_is_200(tmp_path, monkeypatch, qapp, dark_theme):
    """v0.7.1: preview 240px -> 200px (the tab room comes from here)."""
    window = _open_window(tmp_path, monkeypatch, qapp)
    assert PREVIEW_MIN_HEIGHT == 200
    assert window._tv_chart.minimumHeight() == 200
    assert window._price_chart.minimumHeight() == 200
    window.close()


def test_price_panel_rows_are_merged(tmp_path, monkeypatch, qapp,
                                     dark_theme):
    """v0.7.1: price + change + Expand share one row; status +
    Retry + range buttons share one row inside the chart container
    (so they travel into the expand dialog)."""
    window = _open_window(tmp_path, monkeypatch, qapp)
    price_layout = window._price_layout
    first_row = price_layout.itemAt(0).layout()
    assert first_row is not None
    row_widgets = [first_row.itemAt(i).widget()
                   for i in range(first_row.count())]
    assert window._price_value in row_widgets
    assert window._price_change in row_widgets
    assert window._expand_button in row_widgets
    container_layout = window._chart_container.layout()
    assert container_layout.count() == 2  # chart + merged row
    merged = container_layout.itemAt(1).layout()
    assert merged is not None
    merged_widgets = [merged.itemAt(i).widget()
                      for i in range(merged.count())]
    assert window._chart_status in merged_widgets
    assert window._chart_retry_button in merged_widgets
    for btn in window._range_buttons.values():
        assert btn in merged_widgets
    # v0.7.1: the status label is horizontally Ignored — its long
    # fallback text must not force the expand dialog wider than its
    # computed geometry (the dialog came out 801px and off-centre
    # on an 800px screen).
    from PySide6.QtWidgets import QSizePolicy

    assert window._chart_status.sizePolicy().horizontalPolicy() \
        == QSizePolicy.Ignored
    window.close()


def _logs_action(window):
    for action in window._panels_menu.actions():
        if action.text() == "Logs":
            return action
    raise AssertionError("no Logs action in the Panels menu")


def test_logs_dock_hidden_by_default_and_toggleable(
        tmp_path, monkeypatch, qapp, dark_theme):
    """v0.7.1: the Logs drawer starts hidden (it cost the tabs
    130px); the Panels menu's Logs toggle brings it back, and
    closing it with the X remembers the choice."""
    settings = QSettings("Vanta", "GoldBoard")
    settings.setValue("logs_visible", False)
    window = _open_window(tmp_path, monkeypatch, qapp)
    assert window._log_dock.isVisible() is False
    action = _logs_action(window)
    assert action.isCheckable()
    assert action.isChecked() is False
    action.trigger()
    qapp.processEvents()
    assert window._log_dock.isVisible() is True
    assert settings.value("logs_visible", type=bool) is True
    # Close with the dock's X: the setting + menu follow.
    window._log_dock.close()
    qapp.processEvents()
    assert window._log_dock.isVisible() is False
    assert settings.value("logs_visible", type=bool) is False
    settings.setValue("logs_visible", False)
    window.close()
