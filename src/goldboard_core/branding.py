"""Branding — single source of truth for display name, logo and palette.

Product name: **Vanta**. App title: Vanta. (Internal identifiers stay
as they are for v0.1.0 — workspace folder ``gold-board/`` and package
``goldboard_core`` — so Phase 2 imports do not break; see README.)

When the display name ever needs to change, change ``APP_DISPLAY_NAME``
here — and nowhere else — and the title updates consistently (CLI
banner now, desktop window/header later).

Logo: gold eclipse ring on black, source
``assets/logo/media-generation-vanta-logo-0-e629295d-eb4a-474a-a1ef-083625bf172a.webp``,
Windows icon ``assets/logo/vanta.ico`` (generated from that source,
16/32/48/64/128/256). Other files in ``assets/logo/`` are rejected
drafts only and must not be used as the product/app name or icon.
"""

from __future__ import annotations

from pathlib import Path

# Display name shown in the UI / CLI. Internal identifiers (package name,
# config directory, database file) stay "goldboard"/"GoldBoard" regardless.
APP_DISPLAY_NAME = "Vanta"

# Directory holding the logo files (repo-relative, read-only here).
LOGO_DIR = Path(__file__).resolve().parents[2] / "assets" / "logo"

# Chosen logo source (gold eclipse ring on black) inside LOGO_DIR.
APP_LOGO_FILE = "media-generation-vanta-logo-0-e629295d-eb4a-474a-a1ef-083625bf172a.webp"

# Windows app icon generated from APP_LOGO_FILE (16–256px, in LOGO_DIR).
APP_ICON_FILE = "vanta.ico"

# Black + gold palette, kept consistent across the whole desktop UI.
PALETTE = {
    "black": "#0B0B0C",
    "gold": "#C9A227",
    "gold_light": "#E3C565",
    "surface": "#161617",
    "text": "#F2EDE3",
    "muted": "#9A958A",
}


def get_app_logo_path() -> Path | None:
    """Absolute path of the chosen logo file, or None if the file is missing."""
    path = LOGO_DIR / APP_LOGO_FILE
    return path if path.is_file() else None


def get_app_icon_path() -> Path | None:
    """Absolute path of the Windows .ico app icon, or None if missing."""
    path = LOGO_DIR / APP_ICON_FILE
    return path if path.is_file() else None
