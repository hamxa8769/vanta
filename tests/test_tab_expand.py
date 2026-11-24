"""Tab expand (v0.8.0) — headless only.

The tabs are small on a 1366x768 screen, so a tab can be opened
large: the "Expand tab" button in the tab bar's corner, or a
double-click on a tab. Like the chart dialog, the expanded tab
holds the SAME page (its QScrollArea, reparented, never copied);
closing the dialog docks it back into its tab at the same
position. Closing the app with the dialog open must not crash.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS",
                      "--no-sandbox --disable-gpu")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from tests.test_dashboard_tabs import dark_theme  # noqa: F401  (fixture)
from tests.test_ui import _open_window


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def test_expand_button_opens_centred_modeless_tab_dialog(
        tmp_path, monkeypatch, qapp, dark_theme):
    """v0.8.0: Expand tab moves the SAME scroll area into a
    centred modeless dialog; closing it docks the page back."""
    window = _open_window(tmp_path, monkeypatch, qapp)
    scroll = window._tab_pages["institutions"]
    index = window._tabs.indexOf(scroll)
    assert index >= 0
    window._tabs.setCurrentIndex(index)
    window._expand_tab_button.click()
    qapp.processEvents()
    dialog = window._tab_dialog
    assert dialog is not None
    assert dialog.isVisible()
    assert dialog.isModal() is False
    assert dialog.windowTitle().startswith("Institutions")
    # The same widget moved, not a copy — and it is VISIBLE in the
    # dialog (a removeTab-hidden page needs an explicit show(), or
    # the dialog opens black with only its Collapse button).
    assert dialog.tab_layout.indexOf(scroll) >= 0
    assert scroll.isVisible()
    assert scroll.geometry().height() > 200
    assert window._tabs.indexOf(scroll) < 0
    # Centred within the screen's available geometry. Exact-pixel
    # equality is not achievable on every platform: when the dialog
    # is as wide as the screen, its window frame hangs off the edge
    # and the platform nudges it ~2px (verified locally), so allow
    # a 5px tolerance — it still catches real mis-centring (the
    # original bug was 38px, the first fix attempt 20px).
    screen = QApplication.primaryScreen().availableGeometry()
    centre = dialog.geometry().center()
    assert abs(centre.x() - screen.center().x()) <= 5
    assert abs(centre.y() - screen.center().y()) <= 5
    # Closing the dialog docks the page back, same position.
    dialog.close()
    qapp.processEvents()
    assert window._tab_dialog is None
    assert window._tabs.indexOf(scroll) == index
    assert window._tabs.tabText(index) == "Institutions"
    assert window._tabs.currentIndex() == index
    window.close()


def test_double_click_on_tab_expands_it(tmp_path, monkeypatch, qapp,
                                        dark_theme):
    """v0.8.0: double-clicking a tab bar opens that tab large."""
    window = _open_window(tmp_path, monkeypatch, qapp)
    scroll = window._tab_pages["technical"]
    index = window._tabs.indexOf(scroll)
    window._on_tab_double_clicked(index)
    qapp.processEvents()
    assert window._tab_dialog is not None
    assert window._tab_dialog.windowTitle().startswith("Technical")
    assert window._tab_dialog.tab_layout.indexOf(scroll) >= 0
    window._tab_dialog.close()
    qapp.processEvents()
    assert window._tabs.indexOf(scroll) == index
    window.close()


def test_expand_while_open_raises_existing_tab_dialog(
        tmp_path, monkeypatch, qapp, dark_theme):
    """v0.8.0: expanding twice raises the same dialog, no second
    one; the expand button is disabled while it is open."""
    window = _open_window(tmp_path, monkeypatch, qapp)
    assert window._expand_tab_button.isEnabled()
    window._expand_tab("signals")
    first = window._tab_dialog
    assert first is not None
    assert not window._expand_tab_button.isEnabled()
    window._expand_tab("technical")
    assert window._tab_dialog is first
    first.close()
    qapp.processEvents()
    assert window._expand_tab_button.isEnabled()
    window.close()


def test_window_close_with_tab_dialog_open_does_not_crash(
        tmp_path, monkeypatch, qapp, dark_theme):
    """v0.8.0: closing the app while the tab dialog is open docks
    the page back and closes cleanly (no dangling widget)."""
    window = _open_window(tmp_path, monkeypatch, qapp)
    window._expand_tab("macro_news")
    qapp.processEvents()
    assert window._tab_dialog is not None
    window.close()
    qapp.processEvents()


def test_expand_tab_button_lives_in_tab_bar_corner(
        tmp_path, monkeypatch, qapp, dark_theme):
    """v0.8.0: the button is the tab widget's corner widget — a
    separate row above the tabs would cost them ~30px at 1366x768
    (the v0.7.1 layout test holds the tabs to >= 250px there)."""
    window = _open_window(tmp_path, monkeypatch, qapp)
    assert window._tabs.cornerWidget(Qt.TopRightCorner) \
        is window._expand_tab_button
    window.close()
