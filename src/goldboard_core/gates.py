"""Validation gates G0–G8 — pure logic over the DB, no fake passing.

    G0  data freshness: every required source fetched in the last 24h
        with status ok (a missing or stale source FAILS — a stale feed
        is reported, never treated as fresh).
    G1  >= 100 closed shadow trades
    G2  >= 90 days of shadow history (first signal -> last close)
    G3  expectancy > 0.2R over closed trades
    G4  profit factor >= 1.5 over closed trades
    G5  win rate — reported only, no pass threshold (informational)
    G6  max drawdown < 10R over the closed-trade equity curve (reported)
    G7  backtest vs shadow alignment — reported only if BOTH exist
        (a backtest summary must be stored in meta first; without one
        the gate is NOT evaluated and does not pass). Passes when
        |backtest expectancy - shadow expectancy| < 0.3R.
    G8  no live-trading flag — always passes; shadow_only=True

ready_for_muuk = G0 AND G1 AND G2 AND G3 AND G4 all passed.
G5–G8 never gate readiness; they are reported alongside.

Phase 3 adds :func:`gates_summary`, a UI-ready dict (overall bool +
per-gate detail) built on :func:`evaluate_gates`, which already reads
real backtest + shadow data from the DB — no fake passing.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from .models import GateResult
from .storage import (
    get_latest_snapshot_for_source,
    get_meta,
    list_shadow_trades,
    save_gate,
)

REQUIRED_SOURCES = ["fred", "cftc", "wgc", "etf", "comex"]
FRESHNESS_WINDOW = timedelta(hours=24)

GATE_THRESHOLDS = {
    "G0": "all required sources fetched <24h ago, status ok",
    "G1": ">= 100 closed shadow trades",
    "G2": ">= 90 days shadow history",
    "G3": "expectancy > 0.2R",
    "G4": "profit factor >= 1.5",
    "G5": "informational — no threshold",
    "G6": "max drawdown < 10R",
    "G7": "backtest vs shadow expectancy within 0.3R (both must exist)",
    "G8": "shadow_only=True, no live-trading flag",
}

ALIGNMENT_TOLERANCE_R = 0.30


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _to_aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _expectancy(r_values: list[float]) -> float | None:
    if not r_values:
        return None
    return sum(r_values) / len(r_values)


def _profit_factor(r_values: list[float]) -> float | None:
    wins = sum(r for r in r_values if r > 0)
    losses = abs(sum(r for r in r_values if r < 0))
    if not r_values:
        return None
    if losses == 0:
        return float("inf") if wins > 0 else 0.0
    return wins / losses


def _max_drawdown_r(r_values: list[float]) -> float:
    """Max peak-to-trough drawdown of the cumulative-R curve, in R."""
    peak = 0.0
    running = 0.0
    worst = 0.0
    for r in r_values:
        running += r
        peak = max(peak, running)
        worst = max(worst, peak - running)
    return worst


def evaluate_gates(db_path: str | Path, store: bool = True) -> list[GateResult]:
    """Evaluate G0–G8 against the DB. Persists results unless store=False."""
    now = _now()
    results: list[GateResult] = []

    # ---- G0: data freshness -------------------------------------------
    fresh_lines: list[str] = []
    g0_pass = True
    for source in REQUIRED_SOURCES:
        snap = get_latest_snapshot_for_source(db_path, source)
        if snap is None or snap.fetched_at is None:
            g0_pass = False
            fresh_lines.append(f"{source}: no snapshot")
            continue
        fetched = _to_aware(snap.fetched_at)
        age = now - fetched if fetched else None
        if snap.status != "ok":
            g0_pass = False
            fresh_lines.append(f"{source}: status={snap.status}")
        elif age is not None and age <= FRESHNESS_WINDOW:
            fresh_lines.append(f"{source}: fresh ({age.total_seconds() / 3600:.1f}h ago)")
        else:
            g0_pass = False
            age_h = age.total_seconds() / 3600 if age else float("nan")
            fresh_lines.append(f"{source}: stale ({age_h:.1f}h ago)")
    results.append(
        GateResult(
            name="G0",
            passed=g0_pass,
            detail="Data freshness — " + "; ".join(fresh_lines),
            threshold=GATE_THRESHOLDS["G0"],
        )
    )

    # ---- Closed shadow trades (shared by G1–G6) ------------------------
    closed = [t for t in list_shadow_trades(db_path, status="closed")]
    r_values = [t.r_multiple for t in closed if t.r_multiple is not None]

    # ---- G1: trade count ----------------------------------------------
    results.append(
        GateResult(
            name="G1",
            passed=len(closed) >= 100,
            detail=f"{len(closed)} closed shadow trades.",
            threshold=GATE_THRESHOLDS["G1"],
        )
    )

    # ---- G2: history span ----------------------------------------------
    span_days: float | None = None
    if closed:
        starts = [t.generated_at for t in closed if t.generated_at is not None]
        ends = [t.closed_at for t in closed if t.closed_at is not None]
        if starts and ends:
            span_days = (
                max(_to_aware(e) for e in ends) - min(_to_aware(s) for s in starts)
            ).total_seconds() / 86400
    results.append(
        GateResult(
            name="G2",
            passed=span_days is not None and span_days >= 90,
            detail=(
                f"Shadow history spans {span_days:.0f} days."
                if span_days is not None
                else "No closed shadow trades — no history yet."
            ),
            threshold=GATE_THRESHOLDS["G2"],
        )
    )

    # ---- G3: expectancy --------------------------------------------------
    expectancy = _expectancy(r_values)
    results.append(
        GateResult(
            name="G3",
            passed=expectancy is not None and expectancy > 0.2,
            detail=(
                f"Expectancy {expectancy:+.3f}R over {len(r_values)} closed trades."
                if expectancy is not None
                else "No closed trades with R recorded."
            ),
            threshold=GATE_THRESHOLDS["G3"],
        )
    )

    # ---- G4: profit factor ----------------------------------------------
    pf = _profit_factor(r_values)
    results.append(
        GateResult(
            name="G4",
            passed=pf is not None and pf >= 1.5,
            detail=(
                f"Profit factor {pf:.2f} over {len(r_values)} closed trades."
                if pf is not None
                else "No closed trades with R recorded."
            ),
            threshold=GATE_THRESHOLDS["G4"],
        )
    )

    # ---- G5: win rate (informational) ------------------------------------
    if r_values:
        wins = sum(1 for r in r_values if r > 0)
        win_rate = wins / len(r_values)
        detail = f"Win rate {win_rate:.1%} ({wins}/{len(r_values)} closed trades)."
    else:
        detail = "Win rate not available — no closed trades."
    results.append(
        GateResult(
            name="G5", passed=True, detail=detail + " (informational, no threshold).",
            threshold=GATE_THRESHOLDS["G5"],
        )
    )

    # ---- G6: max drawdown -------------------------------------------------
    ordered_r = [
        t.r_multiple for t in sorted(
            closed, key=lambda t: _to_aware(t.closed_at) or _to_aware(t.generated_at) or now
        )
        if t.r_multiple is not None
    ]
    dd = _max_drawdown_r(ordered_r)
    results.append(
        GateResult(
            name="G6",
            passed=dd < 10,
            detail=f"Max drawdown {dd:.2f}R over {len(ordered_r)} closed trades.",
            threshold=GATE_THRESHOLDS["G6"],
        )
    )

    # ---- G7: backtest vs shadow alignment (report only if both exist) -----
    backtest_raw = get_meta(db_path, "backtest_expectancy_r")
    backtest_n = get_meta(db_path, "backtest_trades")
    if backtest_raw is None or expectancy is None:
        results.append(
            GateResult(
                name="G7",
                passed=False,
                detail=(
                    "Not evaluated — "
                    + (
                        "no backtest summary stored in meta "
                        "(set backtest_expectancy_r / backtest_trades)."
                        if backtest_raw is None
                        else "no closed shadow trades yet."
                    )
                    + " Report-only gate; absence is not a pass."
                ),
                threshold=GATE_THRESHOLDS["G7"],
            )
        )
    else:
        try:
            backtest_exp = float(backtest_raw)
        except ValueError:
            backtest_exp = None
        if backtest_exp is None:
            results.append(
                GateResult(
                    name="G7", passed=False,
                    detail="Backtest summary in meta is not a number — not evaluated.",
                    threshold=GATE_THRESHOLDS["G7"],
                )
            )
        else:
            diff = abs(expectancy - backtest_exp)
            results.append(
                GateResult(
                    name="G7",
                    # Pass only when BOTH exist and agree within 0.3R;
                    # otherwise an informational fail with the reason.
                    passed=diff < ALIGNMENT_TOLERANCE_R,
                    detail=(
                        f"Shadow expectancy {expectancy:+.3f}R vs backtest "
                        f"{backtest_exp:+.3f}R (n={backtest_n or '?'}); "
                        f"|diff| {diff:.3f}R "
                        + (
                            f"— within {ALIGNMENT_TOLERANCE_R}R."
                            if diff < ALIGNMENT_TOLERANCE_R
                            else f"— exceeds {ALIGNMENT_TOLERANCE_R}R tolerance; "
                                 "backtest and shadow disagree."
                        )
                    ),
                    threshold=GATE_THRESHOLDS["G7"],
                )
            )

    # ---- G8: shadow-only ---------------------------------------------------
    results.append(
        GateResult(
            name="G8",
            passed=True,
            detail="Shadow-only mode enforced (shadow_only=True); "
            "no live-trading flag exists in this phase.",
            threshold=GATE_THRESHOLDS["G8"],
        )
    )

    if store:
        for gate in results:
            save_gate(db_path, gate, evaluated_at=now)
    return results


def ready_for_muuk(gates: list[GateResult]) -> bool:
    """True only when G0–G4 ALL pass. G5–G8 are reported, never gating."""
    required = {"G0", "G1", "G2", "G3", "G4"}
    by_name = {g.name: g.passed for g in gates}
    return all(by_name.get(name, False) for name in required)


def gates_summary(db_path: str | Path, store: bool = True) -> dict:
    """UI-ready summary of the gates for one DB.

    Returns {
        "ready_for_muuk": bool,   # G0–G4 all pass
        "overall": bool,           # same as ready_for_muuk
        "evaluated_at": ISO-8601 str,
        "gates": {
            "G0": {"passed": bool, "detail": str, "threshold": str},
            ... "G8": {...},
        },
    }
    The per-gate evaluations are the real ones from
    :func:`evaluate_gates` (backtest + shadow data from the DB).
    """
    now = _now()
    gates = evaluate_gates(db_path, store=store)
    ready = ready_for_muuk(gates)
    return {
        "ready_for_muuk": ready,
        "overall": ready,
        "evaluated_at": now.isoformat(),
        "gates": {
            g.name: {
                "passed": bool(g.passed),
                "detail": g.detail,
                "threshold": g.threshold,
            }
            for g in gates
        },
    }
