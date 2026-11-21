"""Tests for the Phase 3 backtest engine.

The fixture (data/fixtures/backtest_sample.csv) is HAND-MADE and its
expected outcomes below were computed BY HAND from the price path,
not eyeballed from the engine's output:
  * 70 daily bars (2026-01-05..2026-03-15), high = close + 8, low = close - 8
  * 20 frozen signals, all SL 1.5xATR (risk 30) / TP 3xATR (60):
    9 TP wins (+2R), 9 SL losses (-1R), 2 expired (+0.5R / -0.5R)
  * total +9R, expectancy +0.45R, win rate 50%, PF 18.5/9.5 = 1.9474
  * equity curve is EXIT-ordered (trades realise when they exit):
    R in exit order: -1,+2,+2,-1,+2,-1,+2,-1,+2,+2,-1,-1,-1,+2,+2,-1,+2,-1,+0.5,-0.5
    cumulative: -1,1,3,2,4,3,5,4,6,8,7,6,5,7,9,8,10,9,9.5,9
    worst peak-to-trough: peak 8 -> trough 5 = 3.0R
  * per-trade Sharpe: mean 0.45, population std sqrt(41.45/20) = 1.4396
    -> 0.3126 (per-trade only, NOT annualised)
  * walk-forward 2 folds (chronological halves): 0.8 / 0.1
"""

from datetime import datetime, timedelta, timezone

import pytest

from goldboard_core.backtest import (
    backtest,
    load_cot_price_history,
    split_backtest,
)

FIXTURE = "data/fixtures/backtest_sample.csv"


def _dt(day: int) -> datetime:
    return datetime(2026, 1, 5, tzinfo=timezone.utc) + timedelta(days=day)


def _bars():
    """Tiny synthetic bars: (day, close, high, low)."""
    return [
        (_dt(0), 100.0, 101.0, 99.0),
        (_dt(1), 100.0, 120.0, 99.0),
        (_dt(2), 100.0, 101.0, 99.0),
        (_dt(3), 105.0, 106.0, 104.0),
    ]


def test_loader_parses_fixture(tmp_path):
    import shutil

    data = load_cot_price_history(FIXTURE)
    assert len(data["prices"]) == 70
    assert len(data["signals"]) == 20
    assert data["prices"][0][0] == _dt(0)
    assert data["prices"][-1][0] == _dt(69)
    assert all(s["direction"] in ("buy", "sell") for s in data["signals"])
    # Loader must not depend on the repo layout: a copy elsewhere works.
    copy = tmp_path / "copy.csv"
    shutil.copy(FIXTURE, copy)
    assert len(load_cot_price_history(copy)["signals"]) == 20


def test_loader_missing_file_raises():
    with pytest.raises(FileNotFoundError, match="historical dataset not found"):
        load_cot_price_history("data/fixtures/does_not_exist.csv")


def test_loader_rejects_bad_columns(tmp_path):
    bad = tmp_path / "bad.csv"
    bad.write_text("foo,bar\n1,2\n", encoding="utf-8")
    with pytest.raises(ValueError, match="at least 'date' and 'price'"):
        load_cot_price_history(bad)


def test_fixture_aggregates_match_hand_computation():
    data = load_cot_price_history(FIXTURE)
    result = backtest(data["signals"], data["prices"])
    assert result.n_trades == 20
    assert result.skipped == 0
    assert result.wins == 10
    assert result.losses == 10
    assert result.breakevens == 0
    assert result.tp_hits == 9
    assert result.sl_hits == 9
    assert result.expired == 2
    assert result.total_r == pytest.approx(9.0)
    assert result.expectancy_r == pytest.approx(0.45)
    assert result.win_rate == pytest.approx(0.5)
    assert result.profit_factor == pytest.approx(18.5 / 9.5, abs=1e-6)
    assert result.max_drawdown_r == pytest.approx(3.0)
    assert result.sharpe_per_trade == pytest.approx(0.3126, abs=1e-3)


def test_fixture_individual_outcomes():
    """Spot-check per-signal R against the hand-computed outcomes."""
    data = load_cot_price_history(FIXTURE)
    result = backtest(data["signals"], data["prices"])
    by_generated = {}
    for trade in result.trades:
        by_generated.setdefault(trade.generated_at, []).append(trade)
    # Day 5 (2026-01-10): buy TP +2R, sell SL -1R.
    day5 = sorted(by_generated[_dt(5)], key=lambda t: t.direction)
    assert [(t.direction, t.outcome, t.r_multiple) for t in day5] == [
        ("buy", "tp", pytest.approx(2.0)),
        ("sell", "sl", pytest.approx(-1.0)),
    ]
    # Day 60 (2026-03-06): both expire at the 2026-03-15 close 3895.
    day60 = sorted(by_generated[_dt(60)], key=lambda t: t.direction)
    assert [(t.direction, t.outcome, t.r_multiple, t.exit_price) for t in day60] == [
        ("buy", "expired", pytest.approx(0.5), 3895.0),
        ("sell", "expired", pytest.approx(-0.5), 3895.0),
    ]
    # Day 20: buy SL -1R (exit 2026-01-26), sell TP +2R (exit 2026-01-27).
    day20 = sorted(by_generated[_dt(20)], key=lambda t: t.direction)
    assert [(t.direction, t.outcome, t.r_multiple) for t in day20] == [
        ("buy", "sl", pytest.approx(-1.0)),
        ("sell", "tp", pytest.approx(2.0)),
    ]


def test_tp_is_plus_2r_and_sl_is_minus_1r():
    bars = _bars()
    buy_tp = {"direction": "buy", "entry": 100.0, "sl": 95.0, "tp": 110.0,
              "generated_at": _dt(0)}
    buy_sl = {"direction": "buy", "entry": 100.0, "sl": 99.5, "tp": 110.0,
              "generated_at": _dt(0)}
    result = backtest([buy_tp, buy_sl], bars)
    assert result.n_trades == 2
    by_sl = {t.sl: t for t in result.trades}
    assert by_sl[95.0].outcome == "tp"
    assert by_sl[95.0].r_multiple == pytest.approx(2.0)
    assert by_sl[99.5].outcome == "sl"
    assert by_sl[99.5].r_multiple == pytest.approx(-1.0)
    # Sell TP needs its own bars: the shared set's day-2 high of 120
    # would stop out any sell with SL < 120 first.
    sell_bars = [
        (_dt(0), 100.0, 101.0, 99.0),
        (_dt(1), 95.0, 96.0, 94.0),
        (_dt(2), 90.0, 91.0, 89.0),
    ]
    sell_tp = {"direction": "sell", "entry": 100.0, "sl": 105.0, "tp": 90.0,
               "generated_at": _dt(0)}
    sell_result = backtest([sell_tp], sell_bars)
    assert sell_result.trades[0].outcome == "tp"
    assert sell_result.trades[0].exit_at == _dt(2)
    assert sell_result.trades[0].r_multiple == pytest.approx(2.0)


def test_no_lookahead_signal_must_not_use_its_own_bar():
    """A signal generated ON bar 2 must be evaluated from bar 3 only.

    Bar 2 (index 1) spikes to a high of 120 — if the engine looked
    ahead and used it, the buy (TP 110) would print +2R. Correctly
    it only sees bars 3-4, neither hits, so it expires at bar 4's
    close 105: R = (105 - 100) / 5 = +1.0R.
    """
    bars = _bars()
    signal = {"direction": "buy", "entry": 100.0, "sl": 95.0, "tp": 110.0,
              "generated_at": _dt(1)}  # generated ON bar 2
    result = backtest([signal], bars, horizon_days=10)
    assert result.n_trades == 1
    trade = result.trades[0]
    assert trade.outcome == "expired"
    assert trade.exit_at == _dt(3)
    assert trade.r_multiple == pytest.approx(1.0)
    assert trade.r_multiple != pytest.approx(2.0)


def test_signal_after_last_bar_is_skipped():
    bars = _bars()
    signal = {"direction": "buy", "entry": 100.0, "sl": 95.0, "tp": 110.0,
              "generated_at": _dt(3)}  # generated on the last bar
    result = backtest([signal], bars)
    assert result.n_trades == 0
    assert result.skipped == 1


def test_none_signal_and_missing_levels_are_skipped():
    bars = _bars()
    signals = [
        {"direction": "none", "entry": 100.0, "sl": None, "tp": None,
         "generated_at": _dt(0)},
        {"direction": "buy", "entry": 100.0, "sl": None, "tp": 110.0,
         "generated_at": _dt(0)},
    ]
    result = backtest(signals, bars)
    assert result.n_trades == 0
    assert result.skipped == 2


def test_expired_exits_at_last_in_horizon_close():
    bars = [
        (_dt(0), 100.0, 101.0, 99.0),
        (_dt(1), 102.0, 103.0, 101.0),
        (_dt(2), 104.0, 105.0, 103.0),
        (_dt(3), 103.0, 104.0, 102.0),  # outside a 2-day horizon
    ]
    signal = {"direction": "buy", "entry": 100.0, "sl": 90.0, "tp": 120.0,
              "generated_at": _dt(0)}
    result = backtest([signal], bars, horizon_days=2)
    assert result.n_trades == 1
    trade = result.trades[0]
    assert trade.outcome == "expired"
    assert trade.exit_at == _dt(2)
    assert trade.exit_price == pytest.approx(104.0)
    assert trade.r_multiple == pytest.approx(0.4)  # (104-100)/10


def test_same_bar_tp_and_sl_takes_sl_first():
    """Both levels inside one bar's range -> SL (conservative)."""
    bars = [(_dt(0), 100.0, 101.0, 99.0),
            (_dt(1), 100.0, 115.0, 90.0)]  # high >= TP 110 AND low <= SL 95
    signal = {"direction": "buy", "entry": 100.0, "sl": 95.0, "tp": 110.0,
              "generated_at": _dt(0)}
    result = backtest([signal], bars)
    assert result.trades[0].outcome == "sl"
    assert result.trades[0].r_multiple == pytest.approx(-1.0)


def test_close_only_bars_use_close_for_high_low():
    bars = [(_dt(0), 100.0), (_dt(1), 112.0)]  # (datetime, close) only
    signal = {"direction": "buy", "entry": 100.0, "sl": 95.0, "tp": 110.0,
              "generated_at": _dt(0)}
    result = backtest([signal], bars)
    assert result.trades[0].outcome == "tp"
    assert result.trades[0].r_multiple == pytest.approx(2.0)


def test_walk_forward_folds_match_hand_computation():
    data = load_cot_price_history(FIXTURE)
    folds = split_backtest(data["signals"], data["prices"], n_splits=2)
    assert [f["n_trades"] for f in folds] == [10, 10]
    # Fold 1 (first 10 signals, chronological): total +8R -> 0.8R.
    # Fold 2 (last 10): total +1R -> 0.1R.
    assert folds[0]["expectancy_r"] == pytest.approx(0.8)
    assert folds[1]["expectancy_r"] == pytest.approx(0.1)
    # Dict-form input (as returned by the loader) also works.
    folds_dict = split_backtest(data, 2)
    assert [f["expectancy_r"] for f in folds_dict] == pytest.approx([0.8, 0.1])
    folds4 = split_backtest(data, 4)
    assert len(folds4) == 4
    assert sum(f["n_trades"] for f in folds4) == 20


def test_empty_backtest_is_zeroed_not_crash():
    result = backtest([], _bars())
    assert result.n_trades == 0
    assert result.expectancy_r == 0.0
    assert result.profit_factor == 0.0
    assert result.sharpe_per_trade == 0.0
    assert result.max_drawdown_r == 0.0
    assert result.to_dict()["sharpe_note"].startswith("per-trade only")


def test_backtest_cli_writes_json(tmp_path):
    import json
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    out = tmp_path / "bt.json"
    proc = subprocess.run(
        [sys.executable, str(root / "scripts" / "backtest.py"),
         "--prices", str(root / FIXTURE), "--out", str(out)],
        capture_output=True, text=True, cwd=str(root),
    )
    assert proc.returncode == 0, proc.stderr
    summary = json.loads(out.read_text(encoding="utf-8"))
    assert summary["backtest"]["expectancy_r"] == pytest.approx(0.45)
    assert summary["backtest"]["n_trades"] == 20
    assert summary["walk_forward_folds"][0]["expectancy_r"] == pytest.approx(0.8)
    assert "SHADOW" not in proc.stdout  # backtest CLI is not the shadow runner
