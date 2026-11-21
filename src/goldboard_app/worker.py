"""Refresh worker for Vanta (Phase 4).

``collect_dashboard_data`` does the real work without Qt so it is
unit-testable: refresh every connector via the registry, then score
and signal from the DB. ``RefreshWorker`` runs it on a QThread so the
UI never blocks on network I/O.

Note: ``registry.refresh_all(db_path)`` persists every fetched
snapshot to the DB (v0.1.2 fix — before that it stored nothing and
the dashboard always showed "no data yet"). The score/signal
therefore always come from the DB (whatever is stored there), and a
source whose connector failed is shown as FAILED/STALE, never with
an invented value.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from goldboard_core.api import get_features, get_score, get_signal
from goldboard_core.gates import evaluate_gates, ready_for_muuk
from goldboard_core.storage import (
    get_latest_snapshot_by_series,
    get_latest_snapshot_for_source,
    list_signals,
)
from goldboard_data.registry import refresh_all

try:
    from PySide6.QtCore import QThread, Signal

    _QT_AVAILABLE = True
except Exception:  # pragma: no cover — Qt missing (tests importorskip)
    QThread = object  # type: ignore[assignment, misc]
    Signal = None  # type: ignore[assignment]
    _QT_AVAILABLE = False

# Sources shown in the freshness panel, in display order.
FRESHNESS_SOURCES = [
    "price", "fred", "cftc", "wgc", "etf", "comex",
    "lbma", "fedwatch", "gdelt", "finnhub", "supply", "microstructure",
]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _series_history(
    db_path: str | Path, series_id: str, limit: int = 52
) -> list[dict]:
    """Oldest→newest (as_of, value) points for one series, read-only."""
    path = Path(db_path)
    if not path.is_file():
        return []
    try:
        conn = sqlite3.connect(str(path))
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT as_of, fetched_at, value FROM snapshots "
                "WHERE series_id = ? AND status != 'failed' "
                "ORDER BY COALESCE(as_of, fetched_at) ASC, id ASC LIMIT ?",
                (series_id, limit),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return []
    out = []
    for row in rows:
        stamp = row["as_of"] or row["fetched_at"]
        out.append({"as_of": stamp, "value": float(row["value"])})
    return out


def _freshness(db_path: str | Path, report: dict | None) -> dict[str, dict]:
    """Per-source freshness from the DB, merged with the refresh report."""
    freshness: dict[str, dict] = {}
    for source in FRESHNESS_SOURCES:
        snap = get_latest_snapshot_for_source(db_path, source)
        entry: dict[str, object | None] = {
            "status": "failed",
            "fetched_at": None,
            "as_of": None,
            "source_url": "",
            "error": None,
        }
        if snap is not None:
            entry["status"] = snap.status
            entry["fetched_at"] = snap.fetched_at.isoformat() if snap.fetched_at else None
            entry["as_of"] = snap.as_of.isoformat() if snap.as_of else None
            entry["source_url"] = snap.source_url
        if report and source in report:
            rep = report[source]
            # The live refresh report is authoritative for "just tried".
            if rep.get("status") == "failed":
                entry["status"] = "failed"
                entry["error"] = rep.get("error")
            elif snap is None and rep.get("status") == "ok":
                entry["status"] = "ok"
                entry["fetched_at"] = rep.get("fetched_at")
        freshness[source] = entry
    return freshness


def collect_dashboard_data(db_path: str | Path, refresh: bool = True) -> dict:
    """Gather everything the dashboard needs. Never raises for bad data.

    With ``refresh=True`` the connectors run first (network I/O — call
    from a worker thread). With ``refresh=False`` only the DB is read
    (fast, safe on the UI thread at startup). Failures are recorded in
    ``payload["errors"]`` and the affected panel shows FAILED/STALE.
    """
    db_path = str(db_path)
    payload: dict = {
        "refreshed_at": _now_iso(),
        "report": {},
        "features": {},
        "score": None,
        "signal": None,
        "signals_history": [],
        "gates": [],
        "ready_for_muuk": False,
        "price": None,
        "price_history": [],
        "cot_history": [],
        "snapshots": {},
        "freshness": {},
        "errors": [],
    }

    if refresh:
        try:
            payload["report"] = refresh_all(db_path)
        except Exception as exc:  # registry must never kill the refresh
            payload["errors"].append(f"refresh failed: {exc!r}")
            payload["report"] = {}

    try:
        payload["features"] = get_features(db_path)
    except Exception as exc:
        payload["errors"].append(f"scoring failed: {exc!r}")

    try:
        payload["score"] = get_score(db_path)
    except Exception as exc:
        payload["errors"].append(f"score failed: {exc!r}")

    try:
        price_snap = get_latest_snapshot_by_series(db_path, "price_xauusd")
    except Exception as exc:
        price_snap = None
        payload["errors"].append(f"price lookup failed: {exc!r}")
    if price_snap is not None:
        payload["price"] = price_snap
        try:
            payload["signal"] = get_signal(db_path, float(price_snap.value))
        except Exception as exc:
            payload["errors"].append(f"signal failed: {exc!r}")
    else:
        payload["price"] = None

    try:
        payload["signals_history"] = list_signals(db_path, limit=50)
    except Exception as exc:
        payload["errors"].append(f"signal history failed: {exc!r}")

    try:
        gates = evaluate_gates(db_path, store=True)
        payload["gates"] = gates
        payload["ready_for_muuk"] = ready_for_muuk(gates)
    except Exception as exc:
        payload["errors"].append(f"gates failed: {exc!r}")

    # Series the panels read directly (None = no snapshot stored yet).
    series_ids = [
        "price_xauusd",
        "cot_gold_mm_net", "cot_gold_mm_net_pct_oi",
        "cot_managed_money_net_pctile_52w",
        "etf_gld_tonnes", "etf_gld_daily_change_tonnes",
        "etf_iau_tonnes", "etf_iau_daily_change_tonnes",
        "wgc_cb_net_tonnes", "central_bank_net_tonnes_3m",
        "fred_dfii10", "fred_dgs10", "fred_dtwexbgs", "fred_sofr",
        "gdelt_gold_tone_latest", "gdelt_gold_tone_7d_avg",
        "gdelt_gold_mentions_latest",
        "finnhub_gld_bullish_pct", "finnhub_gold_news_count",
        "comex_registered_share_pct",
    ]
    snapshots: dict[str, object | None] = {}
    for series_id in series_ids:
        try:
            snapshots[series_id] = get_latest_snapshot_by_series(db_path, series_id)
        except Exception:
            snapshots[series_id] = None
    payload["snapshots"] = snapshots
    payload["price_history"] = _series_history(db_path, "price_xauusd", limit=60)
    payload["cot_history"] = _series_history(db_path, "cot_gold_mm_net", limit=52)
    payload["freshness"] = _freshness(db_path, payload["report"] or None)
    return payload


if _QT_AVAILABLE:

    class RefreshWorker(QThread):  # type: ignore[misc]
        """QThread refresh: registry -> score -> signal, results via signals."""

        finished = Signal(dict)  # type: ignore[assignment]
        failed = Signal(str)  # type: ignore[assignment]

        def __init__(self, db_path: str | Path, parent=None) -> None:
            super().__init__(parent)
            self.db_path = str(db_path)

        def run(self) -> None:  # noqa: D102 — QThread entry point
            try:
                payload = collect_dashboard_data(self.db_path, refresh=True)
            except Exception as exc:  # pragma: no cover — defensive
                self.failed.emit(f"refresh failed: {exc!r}")
                return
            self.finished.emit(payload)

else:  # pragma: no cover — Qt missing

    class RefreshWorker:  # type: ignore[no-redef]
        """Stub: raises a clear error when PySide6 is not installed."""

        def __init__(self, *args, **kwargs) -> None:
            raise ImportError(
                "PySide6 is required for the Vanta desktop app "
                "(pip install PySide6)"
            )

