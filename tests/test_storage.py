"""Tests for SQLite storage round-trips (tmp_path, no real data)."""

from datetime import datetime, timezone

from goldboard_core.models import (
    GateResult,
    ScoreResult,
    ShadowTrade,
    Signal,
    SourceSnapshot,
)
from goldboard_core.scoring import score_features
from goldboard_core.storage import (
    close_shadow_trade,
    get_gate_status,
    get_latest_snapshot,
    get_latest_snapshot_by_series,
    get_latest_score,
    get_shadow_trade,
    init_db,
    list_shadow_trades,
    list_signals,
    log_shadow_trade,
    save_gate,
    save_score,
    save_signal,
    save_snapshot,
)


def _db(tmp_path):
    path = tmp_path / "test.db"
    init_db(path)
    return path


def test_snapshot_round_trip(tmp_path):
    db = _db(tmp_path)
    now = datetime.now(timezone.utc)
    snap = SourceSnapshot(
        source="cftc", series_id="cot_managed_money_net_pctile_52w",
        value=72.0, unit="percentile", as_of=now, published_at=now,
        fetched_at=now, status="ok",
        source_url="https://www.cftc.gov/example",
        is_proxy=True, proxy_formula="test-proxy",
        vintage="2026-11-17",
    )
    row_id = save_snapshot(db, snap)
    assert row_id > 0
    got = get_latest_snapshot(db, "cftc", "cot_managed_money_net_pctile_52w")
    assert got is not None
    assert got.value == 72.0
    assert got.is_proxy is True
    assert got.proxy_formula == "test-proxy"
    assert got.vintage == "2026-11-17"
    assert got.source_url == "https://www.cftc.gov/example"


def test_latest_snapshot_is_newest_by_fetched_at(tmp_path):
    db = _db(tmp_path)
    old = datetime(2026, 11, 1, tzinfo=timezone.utc)
    new = datetime(2026, 11, 20, tzinfo=timezone.utc)
    save_snapshot(db, SourceSnapshot("wgc", "x", 1.0, fetched_at=old))
    save_snapshot(db, SourceSnapshot("wgc", "x", 2.0, fetched_at=new))
    assert get_latest_snapshot(db, "wgc", "x").value == 2.0
    assert get_latest_snapshot_by_series(db, "x").value == 2.0
    assert get_latest_snapshot(db, "wgc", "missing") is None


def test_score_round_trip(tmp_path):
    db = _db(tmp_path)
    score = score_features({"cot": 50, "macro": -10})
    save_score(db, score)
    got = get_latest_score(db)
    assert got is not None
    assert got.total == score.total
    assert len(got.components) == 2
    assert got.rule_version == "v0.1.0"


def test_signal_round_trip_and_list(tmp_path):
    db = _db(tmp_path)
    sig = Signal(direction="buy", entry=4000.0, sl=3970.0, tp=4060.0,
                 confidence=0.8, reason="test", rule_version="v0.1.0")
    save_signal(db, sig)
    listed = list_signals(db)
    assert len(listed) == 1
    assert listed[0].direction == "buy"
    assert listed[0].sl == 3970.0
    assert listed[0].tp == 4060.0


def test_shadow_trade_log_and_close_win(tmp_path):
    db = _db(tmp_path)
    trade = ShadowTrade(direction="buy", entry=4000.0, sl=3970.0, tp=4060.0)
    tid = log_shadow_trade(db, trade)
    closed = close_shadow_trade(db, tid, exit_price=4060.0)
    assert closed is not None
    assert closed.status == "closed"
    assert closed.outcome == "win"
    assert closed.r_multiple == 2.0  # (4060-4000) / 30 risk
    assert get_shadow_trade(db, tid).r_multiple == 2.0


def test_shadow_trade_close_loss_sell(tmp_path):
    db = _db(tmp_path)
    trade = ShadowTrade(direction="sell", entry=4000.0, sl=4030.0, tp=3940.0)
    tid = log_shadow_trade(db, trade)
    closed = close_shadow_trade(db, tid, exit_price=4015.0)
    assert closed.outcome == "loss"
    assert closed.r_multiple == -0.5  # (4000-4015) / 30


def test_close_unknown_trade_returns_none(tmp_path):
    db = _db(tmp_path)
    assert close_shadow_trade(db, 999, exit_price=4000.0) is None


def test_gate_status_round_trip(tmp_path):
    db = _db(tmp_path)
    save_gate(db, GateResult(name="G1", passed=False, detail="0 trades",
                             threshold=">= 100"))
    save_gate(db, GateResult(name="G1", passed=True, detail="120 trades",
                             threshold=">= 100"))
    status = get_gate_status(db)
    assert len(status) == 1  # latest per gate only
    assert status[0].passed is True
    assert status[0].detail == "120 trades"


def test_list_shadow_trades_filter(tmp_path):
    db = _db(tmp_path)
    t1 = log_shadow_trade(db, ShadowTrade(direction="buy", entry=4000.0,
                                          sl=3970.0, tp=4060.0))
    log_shadow_trade(db, ShadowTrade(direction="sell", entry=4000.0,
                                     sl=4030.0, tp=3940.0))
    close_shadow_trade(db, t1, exit_price=4030.0)
    assert len(list_shadow_trades(db)) == 2
    assert len(list_shadow_trades(db, status="closed")) == 1
    assert len(list_shadow_trades(db, status="open")) == 1


def test_empty_db_reads_return_empty(tmp_path):
    db = tmp_path / "empty.db"
    init_db(db)
    assert get_latest_snapshot(db, "cftc", "x") is None
    assert get_latest_score(db) is None
    assert list_signals(db) == []
    assert get_gate_status(db) == []
