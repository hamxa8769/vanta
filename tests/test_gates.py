"""Tests for gates G0–G8.

Synthetic shadow trades below are TEST FIXTURES ONLY — clearly labelled
in-test, never live data, never presented as real performance.
"""

from datetime import datetime, timedelta, timezone

from goldboard_core.gates import evaluate_gates, ready_for_muuk
from goldboard_core.models import ShadowTrade, SourceSnapshot
from goldboard_core.storage import (
    close_shadow_trade,
    init_db,
    log_shadow_trade,
    save_snapshot,
)

NOW = datetime.now(timezone.utc)


def _seed_fresh_sources(db):
    """Fresh ok snapshots for every G0 required source."""
    for source in ("fred", "cftc", "wgc", "etf", "comex"):
        save_snapshot(
            db,
            SourceSnapshot(source=source, series_id=f"{source}_probe",
                           value=1.0, fetched_at=NOW, status="ok"),
        )


def _seed_synthetic_trades(db, n_wins=80, n_losses=40, span_days=100):
    """Synthetic closed trades: wins +1.0R, losses -0.5R.

    Expectancy = (80*1.0 - 40*0.5) / 120 = +0.5R (> 0.2R).
    Profit factor = 80 / 20 = 4.0 (>= 1.5).
    Interleaved 2-wins-then-1-loss keeps max drawdown ~0.5R (< 10R).
    TEST FIXTURE ONLY.
    """
    start = NOW - timedelta(days=span_days)
    outcomes = []
    w = l = 0
    while w < n_wins or l < n_losses:
        for _ in range(2):
            if w < n_wins:
                outcomes.append(1.0)
                w += 1
        if l < n_losses:
            outcomes.append(-0.5)
            l += 1
    for i, r in enumerate(outcomes):
        gen = start + timedelta(days=span_days * i / len(outcomes))
        close = gen + timedelta(days=1)
        # Buy: entry 4000, SL 3970 (risk 30). Exit for +1R = 4030, -0.5R = 3985.
        exit_price = 4030.0 if r > 0 else 3985.0
        trade = ShadowTrade(direction="buy", entry=4000.0, sl=3970.0,
                            tp=4060.0, generated_at=gen)
        tid = log_shadow_trade(db, trade)
        close_shadow_trade(db, tid, exit_price=exit_price, closed_at=close)


def _by_name(gates):
    return {g.name: g for g in gates}


def test_gates_fail_with_empty_db(tmp_path):
    db = tmp_path / "g.db"
    init_db(db)
    gates = _by_name(evaluate_gates(db))
    assert gates["G0"].passed is False  # no data at all
    assert gates["G1"].passed is False  # 0 trades
    assert gates["G2"].passed is False
    assert gates["G3"].passed is False
    assert gates["G4"].passed is False
    assert gates["G8"].passed is True  # shadow-only, always
    assert gates["G7"].passed is False  # no backtest on record: not a pass
    assert ready_for_muuk(list(gates.values())) is False


def test_gates_pass_with_synthetic_120_trades(tmp_path):
    db = tmp_path / "g.db"
    init_db(db)
    _seed_fresh_sources(db)
    _seed_synthetic_trades(db)
    gates = _by_name(evaluate_gates(db))
    assert gates["G0"].passed is True
    assert gates["G1"].passed is True  # 120 closed
    assert gates["G2"].passed is True  # 100-day span
    assert gates["G3"].passed is True  # expectancy +0.5R
    assert gates["G4"].passed is True  # PF 4.0
    assert "66.7%" in gates["G5"].detail  # informational win rate
    assert gates["G6"].passed is True  # DD ~0.5R < 10R
    assert gates["G7"].passed is False  # no backtest: report-only, not passed
    assert gates["G8"].passed is True
    assert ready_for_muuk(list(gates.values())) is True


def test_g0_fails_when_one_source_stale(tmp_path):
    db = tmp_path / "g.db"
    init_db(db)
    # Fresh snapshots for all required sources EXCEPT cftc, which only
    # has a 3-day-old stale snapshot (so it IS the latest for that source).
    for source in ("fred", "wgc", "etf", "comex"):
        save_snapshot(
            db,
            SourceSnapshot(source=source, series_id=f"{source}_probe",
                           value=1.0, fetched_at=NOW, status="ok"),
        )
    save_snapshot(
        db,
        SourceSnapshot(source="cftc", series_id="cftc_probe", value=1.0,
                       fetched_at=NOW - timedelta(days=3), status="stale"),
    )
    gates = _by_name(evaluate_gates(db))
    assert gates["G0"].passed is False
    assert "cftc" in gates["G0"].detail


def test_g1_threshold_boundary(tmp_path):
    db = tmp_path / "g.db"
    init_db(db)
    _seed_fresh_sources(db)
    _seed_synthetic_trades(db, n_wins=66, n_losses=33, span_days=100)  # 99 trades
    gates = _by_name(evaluate_gates(db))
    assert gates["G1"].passed is False  # 99 < 100
    assert "99 closed" in gates["G1"].detail


def test_g3_fails_on_negative_expectancy(tmp_path):
    db = tmp_path / "g.db"
    init_db(db)
    _seed_fresh_sources(db)
    _seed_synthetic_trades(db, n_wins=20, n_losses=100, span_days=100)
    gates = _by_name(evaluate_gates(db))
    assert gates["G3"].passed is False  # expectancy negative
    assert gates["G4"].passed is False  # PF < 1.5
    assert ready_for_muuk(list(gates.values())) is False


def test_g7_passes_when_backtest_aligns(tmp_path):
    from goldboard_core.storage import set_meta

    db = tmp_path / "g.db"
    init_db(db)
    _seed_fresh_sources(db)
    _seed_synthetic_trades(db)
    # Shadow expectancy is +0.5R; store a backtest within 0.25R of it.
    set_meta(db, "backtest_expectancy_r", "0.45")
    set_meta(db, "backtest_trades", "120")
    gates = _by_name(evaluate_gates(db))
    assert gates["G7"].passed is True
    assert "0.45" in gates["G7"].detail


def test_evaluate_gates_read_only_mode_does_not_write(tmp_path):
    db = tmp_path / "g.db"
    init_db(db)
    evaluate_gates(db, store=False)
    from goldboard_core.storage import get_gate_status

    assert get_gate_status(db) == []
