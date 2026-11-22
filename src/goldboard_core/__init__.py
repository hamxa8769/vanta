"""Vanta — Gold Institutional Analysis Board (core package).

Standalone, shadow/advisory only. No live trading. The core is a
separate importable package so a later JSON/HTTP wrapper can expose it
to Muuk C++ without touching Muuk's code.

Stable Muuk-facing interface (see api.py — signatures are frozen):
    get_features(db_path) -> dict[str, float | None]
    get_score(db_path)    -> ScoreResult | None
    get_signal(db_path, price, atr) -> Signal | None
"""

from __future__ import annotations

from .api import get_features, get_score, get_signal
from .branding import APP_DISPLAY_NAME, APP_ICON_FILE, APP_LOGO_FILE, get_app_icon_path, get_app_logo_path
from .scoring import score_features
from .signals import generate_signal

__version__ = "0.7.0"

__all__ = [
    "__version__",
    "APP_DISPLAY_NAME",
    "APP_LOGO_FILE",
    "APP_ICON_FILE",
    "get_app_logo_path",
    "get_app_icon_path",
    "score_features",
    "generate_signal",
    "get_score",
    "get_signal",
    "get_features",
]
