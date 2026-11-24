"""Dashboard tabs + chart expand + light theme (v0.7.0) — headless only.

The dashboard chart is a small preview (click / ⛶ Expand opens a
centred modeless window with the SAME chart widget reparented into
it); the lower panels live in top tabs; settings live in the System
tab; a header toggle + Settings-tab checkbox switch dark/light.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS",
                      "--no-sandbox --disable-gpu")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from goldboard_app.main_window import MainWindow
from goldboard_core.branding import PALETTE, set_theme
from tests.test_ui import _open_window, _seed_db

TAB_PANELS = {
    "institutions": {"score", "cot", "etf", "central_bank"},
    "macro_news": {"macro", "news", "events"},
    "technical": {"technical", "volume", "backtest"},
    "signals": {"signals", "gates"},
    "system": {"freshness", "settings"},
}


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture()
def dark_theme(qapp):
    """Every test starts dark and leaves the global palette dark."""
    from PySide6.QtCore import QSettings
    settings = QSettings("Vanta", "GoldBoard")
    settings.setValue("theme", "dark")
    set_theme("dark")
    yield
    settings.setValue("theme", "dark")
    set_theme("dark")


def _tab_of(window, key: str) -> str:
    for tab, keys in TAB_PANELS.items():
        if key in keys:
            return tab
    raise AssertionError(f"panel {key} is in no tab spec")


def test_tabs_exist_with_right_panels(tmp_path, monkeypatch, qapp,
                                     dark_theme):
    """v0.7.0: five top tabs; every lower panel lives in exactly one."""
    window = _open_window(tmp_path, monkeypatch, qapp)
    titles = [window._tabs.tabText(i)
              for i in range(window._tabs.count())]
    assert titles == ["Institutions", "Macro and News", "Technical",
                      "Signals and Gates", "System"]
    seen: dict[str, str] = {}
    for panel in window._panels:
        key = panel["key"]
        if key == "price":
            # The chart preview is above the tabs, not in one.
            assert panel["tab"] is None
            continue
        seen[key] = panel["tab"]
    assert set(seen) == set().union(*TAB_PANELS.values())
    for key, tab in seen.items():
        assert tab == _tab_of(window, key), f"{key} in wrong tab: {tab}"
    # The strip and the chart preview stay above the tabs.
    assert window._strip_widget.isVisible()
    assert window._price_box.isVisible()
    assert window._tv_chart.minimumHeight() == 200
    window.close()


def test_expand_button_opens_centred_modeless_dialog(
        tmp_path, monkeypatch, qapp, dark_theme):
    """v0.7.0: ⛶ Expand moves the SAME chart widget into a centred
    modeless dialog; closing it docks the chart back in the preview."""
    window = _open_window(tmp_path, monkeypatch, qapp)
    chart = window._tv_chart
    window._expand_button.click()
    qapp.processEvents()
    dialog = window._chart_dialog
    assert dialog is not None
    assert dialog.isVisible()
    assert dialog.isModal() is False
    assert dialog.windowTitle().startswith("Gold price")
    # The same widget moved, not a copy — and it is VISIBLE in the
    # dialog (the tab dialog opened black until its page got an
    # explicit show(); the chart container gets the same guard).
    assert chart.parent() is not None
    assert dialog.chart_layout.indexOf(window._chart_container) >= 0
    assert window._chart_container.isVisible()
    assert window._chart_container.geometry().height() > 200
    # Centred within the screen's available geometry.
    screen = QApplication.primaryScreen().availableGeometry()
    assert dialog.geometry().center() == screen.center()
    # Big in the dialog.
    assert chart.minimumHeight() >= 480
    assert window._chart_placeholder.isVisible()
    assert window._chart_container not in (
        window._price_layout.itemAt(i).widget()
        for i in range(window._price_layout.count()))
    # Closing the dialog docks the chart back, small again.
    dialog.close()
    qapp.processEvents()
    assert window._chart_dialog is None
    assert chart.minimumHeight() == 200
    assert not window._chart_placeholder.isVisible()
    assert window._price_layout.indexOf(window._chart_container) >= 0
    window.close()


def test_click_on_preview_chart_expands(tmp_path, monkeypatch, qapp,
                                       dark_theme):
    """v0.7.0: clicking the preview chart area opens the dialog."""
    window = _open_window(tmp_path, monkeypatch, qapp)
    qapp.processEvents()
    # Force the fallback deterministically: in CI the WebEngine page
    # loads (network reachable) and the 20s watchdog has not fired
    # yet, so "TradingView cannot load here" is not true at once —
    # the old assertion failed there on both runners. The click
    # behaviour itself does not depend on which page is current.
    window._tv_chart.show_fallback()
    qapp.processEvents()
    fallback = window._price_chart
    assert window._tv_chart.currentWidget() is fallback
    QTest.mouseClick(fallback, Qt.LeftButton)
    qapp.processEvents()
    assert window._chart_dialog is not None
    assert window._chart_dialog.isVisible()
    window._chart_dialog.close()
    qapp.processEvents()
    window.close()


def test_expand_while_open_raises_existing_dialog(
        tmp_path, monkeypatch, qapp, dark_theme):
    """v0.7.0: expanding twice raises the same dialog, no second one."""
    window = _open_window(tmp_path, monkeypatch, qapp)
    window._expand_chart()
    first = window._chart_dialog
    window._expand_chart()
    assert window._chart_dialog is first
    first.close()
    qapp.processEvents()
    window.close()


def test_window_close_with_dialog_open_does_not_crash(
        tmp_path, monkeypatch, qapp, dark_theme):
    """v0.7.0: closing the app while the chart dialog is open docks
    the chart back and closes cleanly (no dangling widget)."""
    window = _open_window(tmp_path, monkeypatch, qapp)
    window._expand_chart()
    qapp.processEvents()
    assert window._chart_dialog is not None
    window.close()
    qapp.processEvents()
    assert window._chart_dialog is None
    assert window._price_layout.indexOf(window._chart_container) >= 0


def test_hiding_all_panels_of_a_tab_hides_the_tab(
        tmp_path, monkeypatch, qapp, dark_theme):
    """v0.7.0: a tab with no visible panels disappears; unhiding any
    panel in it brings the tab back."""
    window = _open_window(tmp_path, monkeypatch, qapp)
    idx = window._tab_index["signals"]
    assert window._tabs.isTabVisible(idx)
    window._toggle_panel("signals", False)
    window._toggle_panel("gates", False)
    qapp.processEvents()
    assert not window._tabs.isTabVisible(idx)
    window._toggle_panel("signals", True)
    qapp.processEvents()
    assert window._tabs.isTabVisible(idx)
    window.close()


def test_price_and_settings_not_in_panels_menu(
        tmp_path, monkeypatch, qapp, dark_theme):
    """v0.7.0: the chart preview and the settings panel are core
    dashboard elements — hiding them would strand the user, so the
    Panels menu does not offer them."""
    window = _open_window(tmp_path, monkeypatch, qapp)
    actions = [a.text() for a in window._panels_menu.actions()]
    assert not any("Gold price" in t for t in actions)
    assert not any("Settings" in t for t in actions)
    assert any("COT" in t for t in actions)
    window.close()


def test_theme_toggle_switches_palette_and_tv_theme(
        tmp_path, monkeypatch, qapp, dark_theme):
    """v0.7.0: the header toggle flips the palette, the stylesheet,
    and the TradingView embed theme; the choice is persisted."""
    from PySide6.QtCore import QSettings

    window = _open_window(tmp_path, monkeypatch, qapp)
    assert PALETTE["black"] == "#0B0B0C"
    assert '"theme": "dark"' in window._tv_chart._html
    window._theme_button.click()
    qapp.processEvents()
    assert window._theme == "light"
    assert PALETTE["black"] == "#F4F1EA"
    assert '"theme": "light"' in window._tv_chart._html
    assert QSettings("Vanta", "GoldBoard").value(
        "theme", type=str) == "light"
    # The Settings-tab checkbox follows the header toggle.
    assert window._theme_check.isChecked()
    # And back to dark via the checkbox.
    window._theme_check.setChecked(False)
    qapp.processEvents()
    assert window._theme == "dark"
    assert PALETTE["black"] == "#0B0B0C"
    assert '"theme": "dark"' in window._tv_chart._html
    window.close()


def test_fallback_chart_paints_light_in_light_theme(
        tmp_path, monkeypatch, qapp, dark_theme):
    """v0.7.0: in the light theme the built-in (fallback) chart's
    background is actually light, not a dark hole in a light page."""
    window = _open_window(tmp_path, monkeypatch, qapp)
    window.show()
    qapp.processEvents()
    chart = window._price_chart
    dark_px = chart.grab().toImage().pixelColor(2, 2)
    assert dark_px.lightness() < 60  # dark canvas in dark theme
    window._theme_button.click()
    qapp.processEvents()
    chart.update()
    qapp.processEvents()
    light_px = chart.grab().toImage().pixelColor(2, 2)
    assert light_px.lightness() > 200  # light canvas in light theme
    window.close()


def test_light_theme_persists_across_windows(
        tmp_path, monkeypatch, qapp, dark_theme):
    """v0.7.0: a window opened after the toggle starts light."""
    from PySide6.QtCore import QSettings

    window = _open_window(tmp_path, monkeypatch, qapp)
    window._theme_button.click()
    qapp.processEvents()
    assert QSettings("Vanta", "GoldBoard").value("theme", type=str) == "light"
    window.close()
    window2 = _open_window(tmp_path, monkeypatch, qapp)
    assert window2._theme == "light"
    assert PALETTE["black"] == "#F4F1EA"
    window2.close()
