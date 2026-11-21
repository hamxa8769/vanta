"""Tests for config: paths, defaults, env-first keys, never persisting env keys."""

import json

import pytest

from goldboard_core import config as config_mod


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path, monkeypatch):
    monkeypatch.setenv("GOLD_BOARD_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.delenv("GOLD_BOARD_FRED_KEY", raising=False)
    monkeypatch.delenv("GOLD_BOARD_FINNHUB_KEY", raising=False)
    monkeypatch.delenv("GOLD_BOARD_FEDWATCH_KEY", raising=False)
    monkeypatch.delenv("GOLD_BOARD_FEDWATCH_URL", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    yield


def test_config_path_uses_override_dir(tmp_path):
    assert str(config_mod.get_config_path()).startswith(str(tmp_path))


def test_defaults_when_no_file():
    cfg = config_mod.load_config()
    assert cfg.fred_api_key == ""
    assert cfg.finnhub_api_key == ""
    assert cfg.paid_sources_enabled is False
    assert cfg.refresh_minutes["cftc"] == 10080
    assert cfg.refresh_minutes["fred"] == 1440
    assert cfg.db_path.endswith("goldboard.db")


def test_save_and_load_round_trip():
    cfg = config_mod.load_config()
    cfg.paid_sources_enabled = True
    cfg.refresh_minutes["news"] = 30
    path = config_mod.save_config(cfg)
    assert path.is_file()
    loaded = config_mod.load_config()
    assert loaded.paid_sources_enabled is True
    assert loaded.refresh_minutes["news"] == 30
    assert loaded.db_path == cfg.db_path


def test_env_key_beats_config_file(monkeypatch):
    cfg = config_mod.load_config()
    cfg.fred_api_key = "from-config-file"
    config_mod.save_config(cfg)
    monkeypatch.setenv("GOLD_BOARD_FRED_KEY", "from-env")
    loaded = config_mod.load_config()
    assert loaded.fred_api_key == "from-env"


def test_env_key_never_written_to_disk(monkeypatch):
    monkeypatch.setenv("GOLD_BOARD_FRED_KEY", "secret-env-key")
    monkeypatch.setenv("GOLD_BOARD_FINNHUB_KEY", "secret-env-key-2")
    cfg = config_mod.load_config()
    assert cfg.fred_api_key == "secret-env-key"
    path = config_mod.save_config(cfg)
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["fred_api_key"] == ""
    assert on_disk["finnhub_api_key"] == ""
    assert "secret-env-key" not in path.read_text(encoding="utf-8")


def test_config_file_key_is_persisted():
    cfg = config_mod.load_config()
    cfg.fred_api_key = "file-key"
    path = config_mod.save_config(cfg)
    assert json.loads(path.read_text(encoding="utf-8"))["fred_api_key"] == "file-key"
    assert config_mod.load_config().fred_api_key == "file-key"


def test_corrupt_config_raises():
    path = config_mod.get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        config_mod.load_config()


def test_v020_fields_round_trip():
    """FedWatch key/URL + live intervals persist (v0.2.0 settings)."""
    cfg = config_mod.load_config()
    assert cfg.price_refresh_seconds == 10
    assert cfg.full_refresh_minutes == 15
    cfg.fedwatch_api_key = "fw-key"
    cfg.fedwatch_api_url = "https://example.invalid/fedwatch"
    cfg.price_refresh_seconds = 5
    cfg.full_refresh_minutes = 30
    config_mod.save_config(cfg)
    loaded = config_mod.load_config()
    assert loaded.fedwatch_api_key == "fw-key"
    assert loaded.fedwatch_api_url == "https://example.invalid/fedwatch"
    assert loaded.price_refresh_seconds == 5
    assert loaded.full_refresh_minutes == 30


def test_fedwatch_env_beats_config_and_never_persists(monkeypatch):
    cfg = config_mod.load_config()
    cfg.fedwatch_api_key = "from-config-file"
    cfg.fedwatch_api_url = "https://example.invalid/from-config"
    config_mod.save_config(cfg)
    monkeypatch.setenv("GOLD_BOARD_FEDWATCH_KEY", "from-env")
    monkeypatch.setenv("GOLD_BOARD_FEDWATCH_URL", "https://example.invalid/from-env")
    loaded = config_mod.load_config()
    assert loaded.fedwatch_api_key == "from-env"
    assert loaded.fedwatch_api_url == "https://example.invalid/from-env"
    path = config_mod.save_config(loaded)
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["fedwatch_api_key"] == ""
    assert on_disk["fedwatch_api_url"] == ""
    assert "from-env" not in path.read_text(encoding="utf-8")
