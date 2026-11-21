"""Connector registry for Vanta (Phase 2).

``REGISTRY`` maps a short source name to its connector class.
``refresh_all()`` runs every connector, catches ``ConnectorError`` per
source, and returns a per-source status report — one source failing
never raises and never blocks the others. ``db_path`` is accepted for
the Phase 1 storage wiring; persistence itself belongs to Phase 1, so
refresh_all currently returns the report only and stores nothing.
"""

from __future__ import annotations

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


def refresh_all(db_path: str | None = None) -> dict:
    """Run every connector once; return a per-source status report.

    Never raises on a single source failing. ``db_path`` is reserved
    for Phase 1 storage wiring (currently unused — no persistence here).
    Report shape:
      {name: {"status": "ok"|"stale"|"failed", "snapshots": int,
              "error": str|None, "fetched_at": iso}}
    """
    report: dict = {}
    for name in sorted(REGISTRY):
        conn = REGISTRY[name]()
        try:
            snaps = conn.fetch()
        except ConnectorError as exc:
            report[name] = {"status": "failed", "snapshots": 0,
                            "error": str(exc),
                            "fetched_at": utcnow().isoformat()}
            continue
        except Exception as exc:  # never let one connector kill the refresh
            report[name] = {"status": "failed", "snapshots": 0,
                            "error": f"unexpected: {exc!r}",
                            "fetched_at": utcnow().isoformat()}
            continue
        if not snaps:
            report[name] = {"status": "failed", "snapshots": 0,
                            "error": conn.last_error or "no snapshots returned",
                            "fetched_at": utcnow().isoformat()}
        else:
            status = "stale" if all(s.status == "stale" for s in snaps) else "ok"
            report[name] = {"status": status, "snapshots": len(snaps),
                            "error": conn.last_error,
                            "fetched_at": utcnow().isoformat()}
    return report
