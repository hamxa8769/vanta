"""Configuration — JSON file, per-OS location, env-first API keys.

Config file:
    Windows: %APPDATA%/GoldBoard/config.json
    Linux:   ~/.config/goldboard/config.json (or $XDG_CONFIG_HOME/goldboard/)
    Override (tests / portable use): $GOLD_BOARD_CONFIG_DIR/config.json

API keys are read from the environment FIRST
(GOLD_BOARD_FRED_KEY / GOLD_BOARD_FINNHUB_KEY / GOLD_BOARD_FEDWATCH_KEY
/ GOLD_BOARD_FEDWATCH_URL), then from the config file. They are never
hardcoded and never logged. Keys that came from the environment are NOT
written back to the config file by save_config().
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

ENV_FRED_KEY = "GOLD_BOARD_FRED_KEY"
ENV_FINNHUB_KEY = "GOLD_BOARD_FINNHUB_KEY"
ENV_FEDWATCH_KEY = "GOLD_BOARD_FEDWATCH_KEY"
ENV_FEDWATCH_URL = "GOLD_BOARD_FEDWATCH_URL"
ENV_CONFIG_DIR = "GOLD_BOARD_CONFIG_DIR"

# Live price ticker: Yahoo quote every N seconds (default 10). Gentle
# enough to stay under Yahoo's informal limits; the full connector
# refresh still runs on its own (slower) cadence.
DEFAULT_PRICE_REFRESH_SECONDS = 10
DEFAULT_FULL_REFRESH_MINUTES = 15

DEFAULT_REFRESH_MINUTES: dict[str, int] = {
    "fred": 1440,  # daily macro/rates
    "cftc": 10080,  # weekly COT (Fri 15:30 ET)
    "wgc": 43200,  # monthly central-bank / ETF reports
    "etf": 1440,  # daily GLD/IAU holdings
    "comex": 1440,  # daily warehouse / OI
    "lbma": 43200,  # monthly vault / clearing
    "gdelt": 60,  # news tone
    "news": 60,
}


def get_config_path() -> Path:
    """Absolute path of the config file for this OS."""
    override = os.environ.get(ENV_CONFIG_DIR)
    if override:
        return Path(override) / "config.json"
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        return base / "GoldBoard" / "config.json"
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "goldboard" / "config.json"


def _default_data_dir() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return base / "GoldBoard" / "data"
    return Path.home() / ".local" / "share" / "goldboard"


@dataclass
class GoldBoardConfig:
    db_path: str = ""
    refresh_minutes: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_REFRESH_MINUTES))
    fred_api_key: str = ""
    finnhub_api_key: str = ""
    fedwatch_api_key: str = ""
    fedwatch_api_url: str = ""
    price_refresh_seconds: int = DEFAULT_PRICE_REFRESH_SECONDS
    full_refresh_minutes: int = DEFAULT_FULL_REFRESH_MINUTES
    paid_sources_enabled: bool = False
    data_dir: str = ""

    # Internal only (never serialised): whether each key came from env.
    _fred_key_from_env: bool = field(default=False, init=False, repr=False)
    _finnhub_key_from_env: bool = field(default=False, init=False, repr=False)
    _fedwatch_key_from_env: bool = field(default=False, init=False, repr=False)
    _fedwatch_url_from_env: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        if not self.data_dir:
            self.data_dir = str(_default_data_dir())
        if not self.db_path:
            self.db_path = str(Path(self.data_dir) / "goldboard.db")


def load_config() -> GoldBoardConfig:
    """Load config from disk (defaults when absent), then apply env keys."""
    path = get_config_path()
    data: dict = {}
    if path.is_file():
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            raise ValueError(f"config file is not a JSON object: {path}")

    cfg = GoldBoardConfig(
        db_path=str(data.get("db_path", "")),
        refresh_minutes={**DEFAULT_REFRESH_MINUTES, **dict(data.get("refresh_minutes", {}))},
        fred_api_key=str(data.get("fred_api_key", "")),
        finnhub_api_key=str(data.get("finnhub_api_key", "")),
        fedwatch_api_key=str(data.get("fedwatch_api_key", "")),
        fedwatch_api_url=str(data.get("fedwatch_api_url", "")),
        price_refresh_seconds=int(data.get(
            "price_refresh_seconds", DEFAULT_PRICE_REFRESH_SECONDS)),
        full_refresh_minutes=int(data.get(
            "full_refresh_minutes", DEFAULT_FULL_REFRESH_MINUTES)),
        paid_sources_enabled=bool(data.get("paid_sources_enabled", False)),
        data_dir=str(data.get("data_dir", "")),
    )

    env_fred = os.environ.get(ENV_FRED_KEY)
    if env_fred:
        cfg.fred_api_key = env_fred
        cfg._fred_key_from_env = True
    env_finnhub = os.environ.get(ENV_FINNHUB_KEY)
    if env_finnhub:
        cfg.finnhub_api_key = env_finnhub
        cfg._finnhub_key_from_env = True
    env_fedwatch = os.environ.get(ENV_FEDWATCH_KEY)
    if env_fedwatch:
        cfg.fedwatch_api_key = env_fedwatch
        cfg._fedwatch_key_from_env = True
    env_fedwatch_url = os.environ.get(ENV_FEDWATCH_URL)
    if env_fedwatch_url:
        cfg.fedwatch_api_url = env_fedwatch_url
        cfg._fedwatch_url_from_env = True
    return cfg


def save_config(cfg: GoldBoardConfig) -> Path:
    """Write config to disk. Env-sourced API keys are never persisted."""
    path = get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "db_path": cfg.db_path,
        "refresh_minutes": cfg.refresh_minutes,
        # Never write a key that came from the environment.
        "fred_api_key": "" if cfg._fred_key_from_env else cfg.fred_api_key,
        "finnhub_api_key": "" if cfg._finnhub_key_from_env else cfg.finnhub_api_key,
        "fedwatch_api_key": "" if cfg._fedwatch_key_from_env else cfg.fedwatch_api_key,
        "fedwatch_api_url": "" if cfg._fedwatch_url_from_env else cfg.fedwatch_api_url,
        "price_refresh_seconds": cfg.price_refresh_seconds,
        "full_refresh_minutes": cfg.full_refresh_minutes,
        "paid_sources_enabled": cfg.paid_sources_enabled,
        "data_dir": cfg.data_dir,
    }
    with path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
        fh.write("\n")
    return path
