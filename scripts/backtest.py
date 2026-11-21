"""Backtest CLI (Phase 3).

Runs the pure backtest engine on a supplied CSV of daily gold prices
+ frozen signals and prints / writes a JSON summary.

Usage:
    python scripts/backtest.py --prices data/fixtures/backtest_sample.csv
    python scripts/backtest.py --prices data/fixtures/backtest_sample.csv \\
        --out results.json --splits 4 --db data/vanta.db

The CSV needs columns: date, price, high, low, signal_direction,
signal_entry, signal_sl, signal_tp, signal_horizon_days (see
data/fixtures/backtest_sample.csv). ``--db`` is optional: when given,
the backtest summary is stored in the DB meta table
(backtest_expectancy_r / backtest_trades) so gate G7 can compare it
against live shadow expectancy. No live fetching, no invented prices.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from goldboard_core.backtest import (  # noqa: E402
    backtest,
    load_cot_price_history,
    split_backtest,
)
from goldboard_core.storage import set_meta  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prices", required=True,
                        help="CSV with date, price, high, low, signal_* columns")
    parser.add_argument("--out", default=None,
                        help="write the JSON summary to this file (in addition to stdout)")
    parser.add_argument("--horizon-days", type=int, default=10,
                        help="evaluation horizon in days (default 10)")
    parser.add_argument("--splits", type=int, default=2,
                        help="walk-forward folds (default 2)")
    parser.add_argument("--db", default=None,
                        help="optional DB path: store the backtest summary in meta for gate G7")
    args = parser.parse_args(argv)

    try:
        data = load_cot_price_history(args.prices)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except (ValueError, KeyError) as exc:
        print(f"error: cannot parse {args.prices}: {exc}", file=sys.stderr)
        return 2

    if not data["signals"]:
        print(f"error: no signals found in {args.prices} — the CSV needs "
              "signal_direction/signal_entry/signal_sl/signal_tp columns",
              file=sys.stderr)
        return 2

    result = backtest(data["signals"], data["prices"], horizon_days=args.horizon_days)
    folds = split_backtest(data, args.splits)

    summary = {
        "source": os.path.abspath(args.prices),
        "price_bars": len(data["prices"]),
        "signals_in_file": len(data["signals"]),
        "horizon_days": args.horizon_days,
        "backtest": result.to_dict(),
        "walk_forward_folds": folds,
    }
    text = json.dumps(summary, indent=2)
    print(text)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
        print(f"wrote {args.out}", file=sys.stderr)
    if args.db:
        set_meta(args.db, "backtest_expectancy_r", f"{result.expectancy_r:.6f}")
        set_meta(args.db, "backtest_trades", str(result.n_trades))
        print(f"stored backtest summary in {args.db} meta "
              f"(expectancy {result.expectancy_r:+.3f}R, n={result.n_trades})",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
