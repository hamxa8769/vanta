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
# Keys are semantic roles, not literal colours: "black" is the app
# background (near-black in dark, warm off-white in light).
PALETTE_DARK = {
    "black": "#0B0B0C",
    "gold": "#C9A227",
    "gold_light": "#E3C565",
    "surface": "#161617",
    "text": "#F2EDE3",
    "muted": "#A8A39A",  # bumped from #9A958A (v0.6.0): a touch lighter
    "border": "#2e2a1e",
    "track": "#3A3A3C",   # gauge / volume-profile neutral bars
    "hover_bg": "#2a2415",
    "va": "#8a6d2b",      # volume-profile value-area bars
}

# Light theme (v0.7.0): same gold brand, warm paper background.
# Gold is darkened so titles / prices stay readable on white.
PALETTE_LIGHT = {
    "black": "#F4F1EA",
    "gold": "#A8860F",
    "gold_light": "#7A5F0E",
    "surface": "#FFFFFF",
    "text": "#1C1A16",
    "muted": "#5C574B",
    "border": "#D9D2BC",
    "track": "#DAD5C7",
    "hover_bg": "#F0E7C8",
    "va": "#D9C27A",
}

PALETTES = {"dark": PALETTE_DARK, "light": PALETTE_LIGHT}

# The active palette. Every consumer reads PALETTE at use time (the
# stylesheet is built from it, painters read it in paintEvent), so
# switching theme is one in-place update — no re-import, no plumbing.
PALETTE = dict(PALETTE_DARK)


def set_theme(name: str) -> dict:
    """Switch the active palette in place; returns it.

    Unknown names fall back to dark. Callers must re-apply the
    stylesheet and repaint after calling this (MainWindow does).
    """
    palette = PALETTES.get(name, PALETTE_DARK)
    PALETTE.clear()
    PALETTE.update(palette)
    return PALETTE


def get_app_logo_path() -> Path | None:
    """Absolute path of the chosen logo file, or None if the file is missing."""
    path = LOGO_DIR / APP_LOGO_FILE
    return path if path.is_file() else None


def get_app_icon_path() -> Path | None:
    """Absolute path of the Windows .ico app icon, or None if missing."""
    path = LOGO_DIR / APP_ICON_FILE
    return path if path.is_file() else None
