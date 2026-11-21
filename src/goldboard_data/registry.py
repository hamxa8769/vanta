"""Connector registry for Vanta (Phase 2).

``REGISTRY`` maps a short source name to its connector class.
``refresh_all()`` runs every connector in parallel (one stalled
endpoint must not block the rest — a sequential refresh took 5+ min
when fredgraph.csv stalled), catches ``ConnectorError`` per source,
persists every fetched snapshot to the DB when ``db_path`` is given,
and returns a per-source status report — one source failing never
raises and never blocks the others.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed

from .base import BaseConnector, ConnectorError, utcnow
from .comex import COMEXConnector
from .cot import COTConnector
from .etf import ETFConnector
from .fedwatch import FedWatchConnector
from .finnhub import FinnhubConnector
from .fred import FREDConnector
from .gdelt import GDELTConnector
from .lbma import LBMAConnector
from .microstructure import MicrostructureConnector
from .price import PriceConnector
from .supply import SupplyConnector
from .wgc import WGCConnector

REGISTRY: dict[str, type[BaseConnector]] = {
    "cot": COTConnector,
    "fred": FREDConnector,
    "wgc": WGCConnector,
    "etf": ETFConnector,
    "comex": COMEXConnector,
    "lbma": LBMAConnector,
    "fedwatch": FedWatchConnector,
    "gdelt": GDELTConnector,
    "finnhub": FinnhubConnector,
    "supply": SupplyConnector,
    "microstructure": MicrostructureConnector,
    "price": PriceConnector,
}


def get_connector(name: str) -> BaseConnector:
    """Instantiate the connector for ``name`` (KeyError if unknown)."""
    try:
        return REGISTRY[name]()
    except KeyError:
        raise KeyError(f"unknown connector: {name!r} "
                       f"(known: {', '.join(sorted(REGISTRY))})")


def list_connectors() -> list[str]:
    return sorted(REGISTRY)


def _fetch_one(name: str):
    """Fetch one connector; never raises (result carries the error)."""
    conn = REGISTRY[name]()
    try:
        snaps = conn.fetch()
    except ConnectorError as exc:
        return name, [], str(exc), None
    except Exception as exc:  # never let one connector kill the refresh
        return name, [], f"unexpected: {exc!r}", None
    return name, snaps, None, conn.last_error


def refresh_all(db_path: str | None = None) -> dict:
    """Run every connector once (in parallel); return a status report.

    Never raises on a single source failing. When ``db_path`` is
    given, every fetched snapshot is persisted to the DB with
    ``storage.save_snapshot`` — without this the dashboard reads an
    empty DB forever (v0.1.1 bug: refresh fetched but stored nothing,
    so every panel stayed on "no data yet"). Fetches run concurrently
    (max 6 threads) so one stalled endpoint cannot hold up the rest;
    persistence stays sequential in the calling thread to keep SQLite
    writes contention-free. Report shape:
      {name: {"status": "ok"|"stale"|"failed", "snapshots": int,
              "persisted": int, "error": str|None,
              "fetched_at": iso}}
    """
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(_fetch_one, name) for name in sorted(REGISTRY)]
        results: dict = {}
        for fut in as_completed(futures):
            name, snaps, error, last_error = fut.result()
            results[name] = (snaps, error, last_error)
    report: dict = {}
    for name in sorted(REGISTRY):
        snaps, error, last_error = results[name]
        if error is not None:
            report[name] = {"status": "failed", "snapshots": 0,
                            "persisted": 0,
                            "error": error,
                            "fetched_at": utcnow().isoformat()}
            continue
        if not snaps:
            report[name] = {"status": "failed", "snapshots": 0,
                            "persisted": 0,
                            "error": last_error or "no snapshots returned",
                            "fetched_at": utcnow().isoformat()}
            continue
        persisted = 0
        persist_error: str | None = None
        if db_path is not None:
            try:
                from goldboard_core.storage import save_snapshot

                for snap in snaps:
                    save_snapshot(db_path, snap)
                    persisted += 1
            except Exception as exc:  # storage failure must be visible
                persist_error = f"persist failed: {exc!r}"
        status = "stale" if all(s.status == "stale" for s in snaps) else "ok"
        if persist_error is not None:
            status = "failed"
        report[name] = {"status": status, "snapshots": len(snaps),
                        "persisted": persisted,
                        "error": persist_error or last_error,
                        "fetched_at": utcnow().isoformat()}
    return report
