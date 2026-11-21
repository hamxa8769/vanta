"""Shadow run CLI (Phase 3) — SHADOW ONLY, never a trade.

One daily cycle:
  1. refresh all connectors (registry.refresh_all) and persist the
     snapshots of every source that reported ok;
  2. score the latest features (api.get_score) and generate the
     current advisory signal (api.get_signal) at the latest price;
  3. open the signal as a shadow trade (ShadowRunner);
  4. expire stale open trades at the current price;
  5. mark open trades and print the gate status.

Nothing here places an order, touches a broker, or moves real money.
The output always says SHADOW ONLY.

Persistence note: registry.refresh_all() returns a per-source status
report only and stores nothing (Phase 2 owns the registry, so this
script does not change it). To persist, the script re-fetches each
source that reported "ok" once via get_connector(name).fetch() and
saves the snapshots with storage.save_snapshot.

Usage:
    python scripts/run_shadow.py [--db PATH] [--price P] [--atr A]
                                 [--no-refresh]
Default DB comes from the app config (load_config().db_path).
--price overrides the price taken from the latest price snapshot
(no price is ever invented: without a snapshot or --price the run
stops before scoring). --no-refresh skips the network refresh and
works off the snapshots already in the DB (offline / test use).
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from goldboard_core import get_score, get_signal  # noqa: E402
from goldboard_core.config import load_config  # noqa: E402
from goldboard_core.gates import gates_summary  # noqa: E402
from goldboard_core.shadow import ShadowRunner  # noqa: E402
from goldboard_core.storage import (  # noqa: E402
    get_latest_snapshot_by_series,
    save_snapshot,
)
from goldboard_data import get_connector, refresh_all  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Vanta shadow run — SHADOW ONLY")
    parser.add_argument("--db", default=None,
                        help="DB path (default: from app config)")
    parser.add_argument("--price", type=float, default=None,
                        help="override price (USD/oz); otherwise the latest "
                             "price snapshot is used")
    parser.add_argument("--atr", type=float, default=None,
                        help="ATR override (default: signal rule uses 1% of price)")
    parser.add_argument("--no-refresh", action="store_true",
                        help="skip the network refresh; use snapshots already in the DB")
    args = parser.parse_args(argv)

    db = args.db or load_config().db_path
    print("Vanta shadow run — SHADOW ONLY (advisory; no order is placed, no real money)")
    print(f"DB: {db}")

    # ---- 1. refresh + persist -------------------------------------------
    if not args.no_refresh:
        report = refresh_all()
        for name in sorted(report):
            entry = report[name]
            print(f"  refresh {name:16s} {entry['status']} "
                  f"({entry['snapshots']} snapshots)"
                  + (f" — {entry['error']}" if entry.get("error") else ""))
            if entry["status"] != "ok":
                continue
            try:
                for snap in get_connector(name).fetch():
                    save_snapshot(db, snap)
            except Exception as exc:  # one source must not kill the run
                print(f"  persist {name:16s} FAILED: {exc!r}")
    else:
        print("  refresh skipped (--no-refresh); using snapshots already in the DB")

    # ---- 2. price --------------------------------------------------------
    price = args.price
    if price is None:
        snap = get_latest_snapshot_by_series(db, "price_xauusd")
        if snap is not None:
            price = float(snap.value)
            print(f"latest price snapshot: {price:.2f} USD/oz "
                  f"(source {snap.source}, as_of {snap.as_of})")
    if price is None or price <= 0:
        print("error: no usable price — no price snapshot in the DB and "
              "no --price given. Refusing to invent one.", file=sys.stderr)
        return 1

    # ---- 3. score + signal ------------------------------------------------
    score = get_score(db)
    if score is None:
        print("no score: no usable pillar data in the DB yet.", file=sys.stderr)
        return 1
    print(f"score: {score.total:+.1f} ({score.rule_version})")
    for component in score.components:
        print(f"  {component.name:20s} {component.score:+.0f} "
              f"(weight {component.weight:.0%})")

    signal = get_signal(db, price, args.atr)
    if signal is None:
        print("no signal: scoring returned nothing.", file=sys.stderr)
        return 1
    print(f"signal: {signal.direction} entry {signal.entry:.2f} "
          f"SL {signal.sl} TP {signal.tp} "
          f"confidence {signal.confidence:.2f}")
    print(f"  reason: {signal.reason}")
    print("  levels are board-generated (ATR rule), never institutional stops.")

    runner = ShadowRunner(db)
    if signal.direction in ("buy", "sell"):
        trade_id = runner.open_signal(signal, price, args.atr)
        print(f"shadow trade opened: id {trade_id} — SHADOW ONLY")
    else:
        print("no directional signal — nothing opened in shadow mode.")

    # ---- 4. expire stale + mark -------------------------------------------
    expired = runner.expire_stale(horizon_days=signal.horizon_days, price=price)
    for trade in expired:
        print(f"shadow trade expired: id {trade.id} ({trade.direction}) "
              f"exit {trade.exit_price:.2f} R {trade.r_multiple:+.3f}")
    marks = runner.daily_mark(price)
    print(f"marked {len(marks)} open shadow trade(s) at {price:.2f}")

    # ---- 5. gates -----------------------------------------------------------
    summary = gates_summary(db)
    print("gates:")
    for name in sorted(summary["gates"]):
        gate = summary["gates"][name]
        mark = "PASS" if gate["passed"] else "FAIL"
        print(f"  [{mark}] {name}: {gate['detail']}")
    print(f"ready_for_muuk: {summary['ready_for_muuk']}")
    print("SHADOW ONLY — end of run.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
