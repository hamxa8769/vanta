# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for Vanta (Phase 4).

Build on Windows with:  pyinstaller vanta.spec
(or scripts/build_windows.ps1, which does the whole flow).
Produces dist/Vanta/Vanta.exe (onedir) with the logo assets bundled.
"""
import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

SPEC_DIR = Path(SPECPATH)  # noqa: F821 — provided by PyInstaller
SRC_DIR = SPEC_DIR / "src"

block_cipher = None

# PySide6 ships Qt plugins/binaries; collect them explicitly so the
# onedir build works even when PyInstaller's Qt hooks miss a plugin.
tmp_ret = collect_all("PySide6")
datas = tmp_ret[0]
binaries = tmp_ret[1]
hiddenimports = tmp_ret[2]

datas += [
    (str(SPEC_DIR / "assets" / "logo"), "assets/logo"),
    # FOMC calendar: read at runtime by goldboard_data.fedwatch; without
    # this the frozen app reports "data/fomc_calendar.json not found".
    (str(SPEC_DIR / "data" / "fomc_calendar.json"), "data"),
]
hiddenimports += [
    "goldboard_core",
    "goldboard_core.api",
    "goldboard_core.branding",
    "goldboard_core.config",
    "goldboard_core.gates",
    "goldboard_core.models",
    "goldboard_core.scoring",
    "goldboard_core.signals",
    "goldboard_core.storage",
    "goldboard_data",
    "goldboard_data.registry",
    "goldboard_app",
    "goldboard_app.main_window",
    "goldboard_app.updater",
    "goldboard_app.worker",
]

a = Analysis(  # noqa: F821 — provided by PyInstaller
    [str(SRC_DIR / "goldboard_app" / "main.py")],
    pathex=[str(SRC_DIR)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Vanta",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,  # windowed app — no console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(SPEC_DIR / "assets" / "logo" / "vanta.ico"),
)

coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="Vanta",
)
