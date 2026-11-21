"""Tests for the Phase 3 ShadowRunner (tmp DBs only — no live data).

Round-trip: open a frozen signal -> shadow trade open in the DB ->
close at TP/SL -> R recorded -> expire stale trades -> daily mark
updates unrealised R without closing anything.
"""

from datetime import datetime, timedelta, timezone

import pytest

from goldboard_core.models import ScoreResult, Signal
from goldboard_core.shadow import ShadowRunner
from goldboard_core.signals import generate_signal
from goldboard_core.storage import (
    get_meta,
    get_shadow_trade,
    list_shadow_trades,
)

NOW = datetime.now(timezone.utc)


def _strong_buy_signal(generated_at=None) -> Signal:
    """A frozen buy signal: entry 4000, SL 3970 (risk 30), TP 4060."""
    score = ScoreResult(total=60.0, components=[], reasons=[],
                        as_of=NOW, rule_version="v0.1.0")
    signal = generate_signal(score, price=4000.0, atr=20.0)
    assert signal.direction == "buy"
    assert signal.entry == 4000.0
    assert signal.sl == 3970.0
    assert signal.tp == 4060.0
    if generated_at is not None:
        signal.generated_at = generated_at
    return signal


def test_open_close_round_trip_tp_win(tmp_path):
    db = tmp_path / "s.db"
    runner = ShadowRunner(db)
    trade_id = runner.open_signal(_strong_buy_signal(), price=4000.0, atr=20.0)
    assert isinstance(trade_id, int) and trade_id > 0

    trade = get_shadow_trade(db, trade_id)
    assert trade.status == "open"
    assert trade.direction == "buy"
    assert trade.entry == 4000.0
    assert trade.sl == 3970.0
    assert trade.tp == 4060.0
    assert trade.signal_id is not None  # linked to the saved signal row

    closed = runner.close_signal(trade_id, exit_price=4060.0,
                                 exit_at=NOW + timedelta(hours=1),
                                 reason="TP hit")
    assert closed.status == "closed"
    assert closed.outcome == "win"
    assert closed.r_multiple == pytest.approx(2.0)
    assert closed.exit_price == 4060.0


def test_close_at_sl_is_minus_1r_and_uses_trade_id_or_signal_id(tmp_path):
    db = tmp_path / "s.db"
    runner = ShadowRunner(db)
    trade_id = runner.open_signal(_strong_buy_signal())
    trade = get_shadow_trade(db, trade_id)
    # Close by the SIGNALS-table id (not the trade id) also resolves.
    closed = runner.close_signal(trade.signal_id, exit_price=3970.0,
                                 exit_at=NOW + timedelta(hours=2))
    assert closed.id == trade_id
    assert closed.outcome == "loss"
    assert closed.r_multiple == pytest.approx(-1.0)


def test_close_unknown_id_returns_none(tmp_path):
    db = tmp_path / "s.db"
    runner = ShadowRunner(db)
    assert runner.close_signal(9999, exit_price=4000.0) is None


def test_close_twice_returns_existing_trade_unchanged(tmp_path):
    db = tmp_path / "s.db"
    runner = ShadowRunner(db)
    trade_id = runner.open_signal(_strong_buy_signal())
    first = runner.close_signal(trade_id, exit_price=4060.0,
                                exit_at=NOW + timedelta(hours=1))
    second = runner.close_signal(trade_id, exit_price=3900.0,
                                 exit_at=NOW + timedelta(hours=2))
    assert second.r_multiple == pytest.approx(2.0)  # not overwritten
    assert second.exit_price == 4060.0
    assert first.id == second.id


def test_open_rejects_none_signal(tmp_path):
    db = tmp_path / "s.db"
    runner = ShadowRunner(db)
    score = ScoreResult(total=0.0, components=[], reasons=[],
                        as_of=NOW, rule_version="v0.1.0")
    none_signal = generate_signal(score, price=4000.0, atr=20.0)
    assert none_signal.direction == "none"
    with pytest.raises(ValueError, match="only buy/sell"):
        runner.open_signal(none_signal, price=4000.0)


def test_open_rejects_bad_price(tmp_path):
    db = tmp_path / "s.db"
    runner = ShadowRunner(db)
    with pytest.raises(ValueError, match="price must be a positive"):
        runner.open_signal(_strong_buy_signal(), price=0.0)


def test_expire_stale_closes_old_trade_at_price(tmp_path):
    db = tmp_path / "s.db"
    runner = ShadowRunner(db)
    old = runner.open_signal(_strong_buy_signal(
        generated_at=NOW - timedelta(days=20)))
    fresh = runner.open_signal(_strong_buy_signal(
        generated_at=NOW - timedelta(days=2)))
    expired = runner.expire_stale(horizon_days=10, price=4015.0, now=NOW)
    assert [t.id for t in expired] == [old]
    trade = get_shadow_trade(db, old)
    assert trade.status == "closed"
    assert trade.outcome == "expired"
    assert trade.exit_price == 4015.0
    assert trade.r_multiple == pytest.approx(0.5)  # (4015-4000)/30
    # The fresh trade is untouched and still open.
    assert get_shadow_trade(db, fresh).status == "open"
    assert len(runner.list_open()) == 1


def test_expire_stale_without_price_closes_at_entry(tmp_path):
    db = tmp_path / "s.db"
    runner = ShadowRunner(db)
    old = runner.open_signal(_strong_buy_signal(
        generated_at=NOW - timedelta(days=15)))
    expired = runner.expire_stale(horizon_days=10, price=None, now=NOW)
    assert len(expired) == 1
    trade = get_shadow_trade(db, old)
    assert trade.outcome == "expired"
    assert trade.exit_price == 4000.0  # entry — R=0, never an invented price
    assert trade.r_multiple == pytest.approx(0.0)
    assert "no current price supplied" in trade.reason


def test_expire_stale_uses_trade_horizon_when_none(tmp_path):
    db = tmp_path / "s.db"
    runner = ShadowRunner(db)
    signal = _strong_buy_signal(generated_at=NOW - timedelta(days=6))
    signal.horizon_days = 5
    trade_id = runner.open_signal(signal)
    expired = runner.expire_stale(horizon_days=None, price=4000.0, now=NOW)
    assert [t.id for t in expired] == [trade_id]


def test_daily_mark_stores_unrealised_r_without_closing(tmp_path):
    db = tmp_path / "s.db"
    runner = ShadowRunner(db)
    trade_id = runner.open_signal(_strong_buy_signal())
    marks = runner.daily_mark(4030.0)
    assert marks == {trade_id: pytest.approx(1.0)}  # (4030-4000)/30
    assert runner.get_unrealised_r(trade_id) == pytest.approx(1.0)
    assert get_meta(db, f"shadow_unrealised_r:{trade_id}") is not None
    # Still open — a mark is never a close.
    assert get_shadow_trade(db, trade_id).status == "open"
    assert list_shadow_trades(db, status="closed") == []
    # A second mark overwrites the stored value.
    runner.daily_mark(3985.0)
    assert runner.get_unrealised_r(trade_id) == pytest.approx(-0.5)


def test_daily_mark_rejects_bad_price(tmp_path):
    db = tmp_path / "s.db"
    runner = ShadowRunner(db)
    with pytest.raises(ValueError, match="price must be a positive"):
        runner.daily_mark(0.0)


def test_run_shadow_cli_offline_opens_shadow_trade(tmp_path):
    """scripts/run_shadow.py --no-refresh on a seeded DB: prints
    SHADOW ONLY, opens the trade, and never places an order."""
    import subprocess
    import sys
    from pathlib import Path

    from goldboard_core.models import SourceSnapshot
    from goldboard_core.storage import init_db, save_snapshot

    root = Path(__file__).resolve().parents[1]
    db = tmp_path / "shadow.db"
    init_db(db)
    # Seed one pillar snapshot per component series so get_score works,
    # plus a price snapshot. Values are TEST FIXTURES ONLY.
    for series_id, value in [
        ("cot_score", 80.0),
        ("central_bank_etf_score", 70.0),
        ("macro_score", 60.0),
        ("news_score", 50.0),
        ("microstructure_score", 40.0),
    ]:
        save_snapshot(db, SourceSnapshot(
            source="test", series_id=series_id, value=value,
            fetched_at=NOW, status="ok"))
    save_snapshot(db, SourceSnapshot(
        source="price", series_id="price_xauusd", value=4000.0,
        fetched_at=NOW, status="ok"))
    proc = subprocess.run(
        [sys.executable, str(root / "scripts" / "run_shadow.py"),
         "--db", str(db), "--no-refresh"],
        capture_output=True, text=True, cwd=str(root),
    )
    assert proc.returncode == 0, proc.stderr
    assert "SHADOW ONLY" in proc.stdout
    assert "shadow trade opened" in proc.stdout
    assert "score: +62.5" in proc.stdout  # 80*.25+70*.20+60*.25+50*.15+40*.15
    assert "signal: buy" in proc.stdout
    assert len(list_shadow_trades(db, status="open")) == 1
