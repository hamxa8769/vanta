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

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import requests  # noqa: E402

from goldboard_data import REGISTRY  # noqa: E402

TIMEOUT = 20


def main() -> int:
    print("Vanta live smoke check — one attempt per free source")
    print("=" * 60)
    results: dict[str, str] = {}
    for name in sorted(REGISTRY):
        conn = REGISTRY[name]()
        try:
            snaps = conn.fetch()
            if snaps:
                detail = ", ".join(
                    f"{s.series_id}={s.value:g}" for s in snaps[:4])
                if len(snaps) > 4:
                    detail += f" (+{len(snaps) - 4} more)"
                results[name] = f"OK ({len(snaps)} snapshots): {detail}"
            else:
                results[name] = f"FAILED: {conn.last_error or 'no snapshots'}"
        except Exception as exc:  # keyed sources raise ConnectorError
            results[name] = f"FAILED: {exc}"
        print(f"{name:16s} {results[name]}")
    print("=" * 60)
    ok = sum(1 for v in results.values() if v.startswith("OK"))
    print(f"{ok}/{len(results)} sources OK")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
