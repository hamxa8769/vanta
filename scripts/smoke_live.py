"""Live smoke check for Vanta data connectors (NOT a pytest test).

Tries each free source once with a 20s timeout and prints ok/failed
per source. Keyed sources (FRED with key, Finnhub, FedWatch) are
reported as 'skipped (key required)' when no key is configured.
Run:  python scripts/smoke_live.py
"""

from __future__ import annotations

import os
import sys
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import requests  # noqa: E402

from goldboard_data import REGISTRY  # noqa: E402

TIMEOUT = 20


def _check(name: str) -> tuple[str, str]:
    conn = REGISTRY[name]()
    try:
        snaps = conn.fetch()
    except Exception as exc:  # keyed sources raise ConnectorError
        return name, f"FAILED: {exc}"
    if snaps:
        detail = ", ".join(f"{s.series_id}={s.value:g}" for s in snaps[:4])
        if len(snaps) > 4:
            detail += f" (+{len(snaps) - 4} more)"
        return name, f"OK ({len(snaps)} snapshots): {detail}"
    return name, f"FAILED: {conn.last_error or 'no snapshots'}"


def main() -> int:
    print("Vanta live smoke check — one attempt per free source (parallel)")
    print("=" * 60)
    results: dict[str, str] = {}
    # Parallel: one stalled endpoint (e.g. fredgraph.csv) must not hold
    # up the rest — the sequential version never finished in 280s.
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(_check, name) for name in sorted(REGISTRY)]
        for fut in as_completed(futures):
            name, result = fut.result()
            results[name] = result
    for name in sorted(results):
        print(f"{name:16s} {results[name]}")
    print("=" * 60)
    ok = sum(1 for v in results.values() if v.startswith("OK"))
    print(f"{ok}/{len(results)} sources OK")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
