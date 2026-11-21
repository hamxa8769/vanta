"""Tests for goldboard_data.price_sources (v0.3.0) — the multi-source
live price aggregator. Network is fully faked; the priority order
itself (gold-api → Yahoo → metals.live → Stooq) is what is tested."""

import pytest

from goldboard_data.base import ConnectorError
from goldboard_data.price_sources import (
    GoldAPIConnector,
    MetalsLiveConnector,
    StooqConnector,
    fetch_best_price,
)


def _quote(source: str, value: float, kind: str = "spot") -> dict:
    return {"value": value, "as_of": None, "source": source, "kind": kind}


def test_gold_api_parses_spot_quote(monkeypatch):
    monkeypatch.setattr(
        GoldAPIConnector, "_get_json",
        lambda self, url, params=None, timeout=None, retries=None:
        {"price": 3539.0, "updatedAt": "2026-11-21T20:10:38Z"})
    quote = GoldAPIConnector().fetch_quote()
    assert quote["value"] == 3539.0
    assert quote["kind"] == "spot"
    assert quote["as_of"] is not None


def test_metals_live_parses_spot_quote(monkeypatch):
    monkeypatch.setattr(
        MetalsLiveConnector, "_get_json",
        lambda self, url, params=None, timeout=None, retries=None:
        {"price": 3540.5, "updatedAt": "2026-11-21T20:10:38Z"})
    quote = MetalsLiveConnector().fetch_quote()
    assert quote["value"] == 3540.5
    assert quote["kind"] == "spot"


def test_stooq_parses_daily_close(monkeypatch):
    csv = ("Symbol,Date,Time,Open,High,Low,Close,Volume\n"
           "GC.F,2026-11-21,22:00,3510.0,3520.0,3505.0,3515.4,12345\n")
    monkeypatch.setattr(
        StooqConnector, "_get_text",
        lambda self, url, params=None, timeout=None, retries=None: csv)
    quote = StooqConnector().fetch_quote()
    assert quote["value"] == 3515.4
    assert quote["kind"] == "daily close"


def test_best_price_prefers_first_working_source(monkeypatch):
    def _fail():
        raise ConnectorError("gold-api down")
    monkeypatch.setattr(
        "goldboard_data.price_sources.SOURCES",
        [("gold-api.com", _fail),
         ("Yahoo Finance (GC=F)",
          lambda: _quote("Yahoo Finance (GC=F)", 3515.4, "futures")),
         ("metals.live", _fail), ("stooq.com", _fail)])
    best = fetch_best_price()
    assert best["value"] == 3515.4
    assert best["source"] == "Yahoo Finance (GC=F)"
    assert best["tried"][0]["ok"] is False
    assert best["tried"][1]["ok"] is True


def test_best_price_spot_consensus_median(monkeypatch):
    monkeypatch.setattr(
        "goldboard_data.price_sources.SOURCES",
        [("gold-api.com", lambda: _quote("gold-api.com", 3539.0)),
         ("Yahoo Finance (GC=F)",
          lambda: _quote("Yahoo Finance (GC=F)", 3515.4, "futures")),
         ("metals.live", lambda: _quote("metals.live", 3541.0)),
         ("stooq.com", lambda: _quote("stooq.com", 3510.0, "daily close"))])
    best = fetch_best_price()
    # Two spots within 0.5% → median 3540.0, labelled consensus.
    assert best["value"] == pytest.approx(3540.0)
    assert "consensus" in best["source"]
    assert best["diverged"] is False


def test_best_price_flags_spot_futures_divergence(monkeypatch):
    monkeypatch.setattr(
        "goldboard_data.price_sources.SOURCES",
        [("gold-api.com", lambda: _quote("gold-api.com", 3600.0)),
         ("Yahoo Finance (GC=F)",
          lambda: _quote("Yahoo Finance (GC=F)", 3515.4, "futures")),
         ("metals.live", lambda: (_ for _ in ()).throw(
             ConnectorError("metals.live down"))),
         ("stooq.com", lambda: (_ for _ in ()).throw(
             ConnectorError("stooq down")))])
    best = fetch_best_price()
    assert best["value"] == 3600.0
    assert best["diverged"] is True


def test_best_price_all_fail_raises(monkeypatch):
    def _fail():
        raise ConnectorError("down")
    monkeypatch.setattr(
        "goldboard_data.price_sources.SOURCES",
        [("gold-api.com", _fail), ("Yahoo Finance (GC=F)", _fail),
         ("metals.live", _fail), ("stooq.com", _fail)])
    with pytest.raises(ConnectorError):
        fetch_best_price()
