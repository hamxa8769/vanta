"""Tests for gates against REAL backtest + shadow data in the DB
(Phase 3 — gates_summary and the G7 alignment gate).

All trades below are SYNTHETIC TEST FIXTURES ONLY — clearly labelled,
never live data, never presented as real performance. They exist to
prove the gates pass/fail exactly at their thresholds:
  * 120 closed trades meeting every threshold -> ready_for_muuk True
  * 99 closed trades -> G1 fails
  * 0 trades -> G1-G4 fail
  * 120 trades with expectancy exactly 0.1R -> G3 fails
  * backtest expectancy within 0.3R of shadow -> G7 passes,
    0.5R away -> G7 fails with the reason in the detail.
"""

from datetime import datetime, timedelta, timezone

import pytest

from goldboard_core.gates import evaluate_gates, gates_summary, ready_for_muuk
from goldboard_core.models import ShadowTrade, SourceSnapshot
from goldboard_core.storage import (
    close_shadow_trade,
    init_db,
    log_shadow_trade,
    save_snapshot,
    set_meta,
)

NOW = datetime.now(timezone.utc)


def _seed_fresh_sources(db):
    """Fresh ok snapshots for every G0 required source (TEST FIXTURE)."""
    for source in ("fred", "cftc", "wgc", "etf", "comex"):
        save_snapshot(
            db,
            SourceSnapshot(source=source, series_id=f"{source}_probe",
                           value=1.0, fetched_at=NOW, status="ok"),
        )


def _seed_synthetic_trades(db, r_values, span_days=100):
    """Close synthetic trades with the given R list (TEST FIXTURE ONLY).

    Buy, entry 4000, SL 3970 (risk 30): exit 4000 + 30*R.
    Generated spread evenly over ``span_days``; each closes 1 day later.
    """
    start = NOW - timedelta(days=span_days)
    for i, r in enumerate(r_values):
        gen = start + timedelta(days=span_days * i / len(r_values))
        close = gen + timedelta(days=1)
        trade = ShadowTrade(direction="buy", entry=4000.0, sl=3970.0,
                            tp=4060.0, generated_at=gen)
        tid = log_shadow_trade(db, trade)
        close_shadow_trade(db, tid, exit_price=4000.0 + 30.0 * r,
                           closed_at=close)


def _passing_r_values():
    """80 wins +1.0R, 40 losses -0.5R, interleaved 2-1 to keep DD low.

    Expectancy (80 - 20) / 120 = +0.5R; PF 80/20 = 4.0; win rate 66.7%.
    """
    values, w, l = [], 0, 0
    while w < 80 or l < 40:
        for _ in range(2):
            if w < 80:
                values.append(1.0)
                w += 1
        if l < 40:
            values.append(-0.5)
            l += 1
    return values


def _by_name(gates):
    return {g.name: g for g in gates}


def test_gates_summary_passes_with_120_trades_meeting_thresholds(tmp_path):
    db = tmp_path / "g.db"
    init_db(db)
    _seed_fresh_sources(db)
    _seed_synthetic_trades(db, _passing_r_values())
    summary = gates_summary(db)
    assert summary["ready_for_muuk"] is True
    assert summary["overall"] is True
    assert "evaluated_at" in summary
    gates = summary["gates"]
    assert set(gates) == {f"G{i}" for i in range(9)}
    for name in ("G0", "G1", "G2", "G3", "G4", "G6", "G8"):
        assert gates[name]["passed"] is True, name
    assert gates["G5"]["passed"] is True  # informational
    assert "66.7%" in gates["G5"]["detail"]
    # G7 without a backtest on record: fail with a reason, never a pass.
    assert gates["G7"]["passed"] is False
    assert "no backtest summary" in gates["G7"]["detail"]
    # The stored evaluation agrees with a fresh read.
    assert ready_for_muuk(evaluate_gates(db, store=False)) is True


def test_gates_summary_fails_with_99_trades(tmp_path):
    db = tmp_path / "g.db"
    init_db(db)
    _seed_fresh_sources(db)
    _seed_synthetic_trades(db, _passing_r_values()[:99])
    summary = gates_summary(db)
    assert summary["ready_for_muuk"] is False
    assert summary["overall"] is False
    assert summary["gates"]["G1"]["passed"] is False
    assert "99 closed" in summary["gates"]["G1"]["detail"]


def test_gates_summary_fails_with_zero_trades(tmp_path):
    db = tmp_path / "g.db"
    init_db(db)
    _seed_fresh_sources(db)
    summary = gates_summary(db)
    assert summary["ready_for_muuk"] is False
    assert summary["overall"] is False
    gates = summary["gates"]
    assert gates["G1"]["passed"] is False  # 0 trades
    assert gates["G2"]["passed"] is False  # no history
    assert gates["G3"]["passed"] is False  # no expectancy
    assert gates["G4"]["passed"] is False  # no profit factor
    assert gates["G7"]["passed"] is False  # no backtest, no shadow
    assert "No closed" in gates["G3"]["detail"] or "No closed trades" in gates["G3"]["detail"]


def test_gates_fail_with_expectancy_exactly_0_1r(tmp_path):
    db = tmp_path / "g.db"
    init_db(db)
    _seed_fresh_sources(db)
    # 66 wins +1R, 54 losses -1R -> total +12R / 120 = +0.1R (<= 0.2R).
    r_values = [1.0] * 66 + [-1.0] * 54
    _seed_synthetic_trades(db, r_values)
    summary = gates_summary(db)
    gates = summary["gates"]
    assert gates["G1"]["passed"] is True  # 120 trades
    assert gates["G2"]["passed"] is True  # 100-day span
    assert gates["G3"]["passed"] is False  # expectancy 0.1R, not > 0.2R
    assert "0.100" in gates["G3"]["detail"]
    assert gates["G4"]["passed"] is False  # PF 66/54 = 1.22 < 1.5
    assert summary["ready_for_muuk"] is False


def test_g7_passes_within_0_3r_and_fails_beyond(tmp_path):
    db = tmp_path / "g.db"
    init_db(db)
    _seed_fresh_sources(db)
    _seed_synthetic_trades(db, _passing_r_values())  # shadow expectancy +0.5R
    # Backtest 0.35R: |0.5 - 0.35| = 0.15R < 0.3R -> pass.
    set_meta(db, "backtest_expectancy_r", "0.35")
    set_meta(db, "backtest_trades", "120")
    gates = _by_name(evaluate_gates(db))
    assert gates["G7"].passed is True
    assert "0.15" in gates["G7"].detail or "0.150" in gates["G7"].detail
    # Backtest 0.0R: |0.5 - 0.0| = 0.5R >= 0.3R -> informational fail.
    set_meta(db, "backtest_expectancy_r", "0.0")
    gates = _by_name(evaluate_gates(db))
    assert gates["G7"].passed is False
    assert "exceeds 0.3R" in gates["G7"].detail
    # G7 is report-only: it never changes ready_for_muuk by itself.
    assert ready_for_muuk(list(gates.values())) is True


def test_g7_boundary_just_inside_and_outside(tmp_path):
    db = tmp_path / "g.db"
    init_db(db)
    _seed_fresh_sources(db)
    _seed_synthetic_trades(db, _passing_r_values())  # shadow +0.5R
    # Diff 0.29R -> pass; diff exactly 0.30R -> fail (strict < 0.3R).
    set_meta(db, "backtest_expectancy_r", "0.21")
    assert _by_name(evaluate_gates(db))["G7"].passed is True
    set_meta(db, "backtest_expectancy_r", "0.20")
    assert _by_name(evaluate_gates(db))["G7"].passed is False


def test_gates_summary_does_not_fake_pass_on_empty_db(tmp_path):
    db = tmp_path / "g.db"
    init_db(db)
    summary = gates_summary(db)
    assert summary["ready_for_muuk"] is False
    assert summary["overall"] is False
    assert summary["gates"]["G0"]["passed"] is False
    assert summary["gates"]["G8"]["passed"] is True  # shadow-only, always
