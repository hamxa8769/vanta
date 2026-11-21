"""Entry point for the Vanta desktop app (also the PyInstaller target)."""

from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

# Absolute import (not relative): PyInstaller runs this file as a
# top-level script ("main"), which has no parent package — a relative
# import here crashed v0.1.0 on launch with "attempted relative import
# with no known parent package". The package is on sys.path (pathex in
# vanta.spec), so the absolute form works as a script and as a module.
from goldboard_app.main_window import MainWindow


def main(argv: list[str] | None = None) -> int:
    """Create the app and show the main window; returns the exit code."""
    app = QApplication(list(argv) if argv is not None else sys.argv)
    app.setApplicationName("Vanta")
    app.setOrganizationName("Vanta")
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
