"""Derived series — scored quantities computed from stored history.

Why this module exists: the connectors store RAW latest values
(``cot_gold_mm_net_pct_oi``, ``etf_gld_tonnes``, ``fred_dfii10``,
``comex_registered_share_pct``) but the frozen score contract
(``api.COMPONENT_SERIES``) asks for derived quantities — a 52-week
percentile, a 13-week flow, 30-day changes. Before this module the
pillars simply stayed None even though the raw data was in the DB.

Each computation reads ONLY stored snapshots (never the network),
returns None — and saves nothing — when there is not enough history,
and saves its result as a snapshot with ``source="derived"`` and
``is_proxy=True`` + a ``proxy_formula`` describing the computation,
so ``api.get_features`` finds it through COMPONENT_SERIES with no
change to api.py or scoring.py. Nothing here is extrapolated or
invented: a missing past value means no derived value.

``compute_derived_series`` is idempotent: a derived snapshot whose
``as_of`` already equals the latest raw data date is not saved again,
so repeated refreshes do not duplicate rows.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from .models import SourceSnapshot
from .storage import get_latest_snapshot_by_series, get_snapshot_history, save_snapshot

History = list[tuple[datetime | None, float]]

# Minimum history each computation needs (see the docstrings).
COT_PERCENTILE_MIN_VALUES = 10
COT_PERCENTILE_WINDOW = 52  # weekly values
ETF_FLOW_MIN_AGE_DAYS = 84  # 12 weeks — the "then" snapshot must be this old
ETF_FLOW_TARGET_DAYS = 91  # 13 weeks
CHANGE_TARGET_DAYS = 30
CHANGE_TOLERANCE_DAYS = 5  # the "30 days ago" value must be 25-35 days old


def _latest(history: History) -> tuple[datetime | None, float] | None:
    dated = [(d, v) for d, v in history if d is not None]
    return dated[-1] if dated else None


def _value_near(history: History, target: datetime,
                tolerance: timedelta) -> tuple[datetime, float] | None:
    """The (date, value) closest to ``target`` within ``tolerance``."""
    best: tuple[datetime, float] | None = None
    best_gap: timedelta | None = None
    for date, value in history:
        if date is None:
            continue
        gap = abs(date - target)
        if gap <= tolerance and (best_gap is None or gap < best_gap):
            best = (date, value)
            best_gap = gap
    return best


def cot_percentile_52w(db_path: str | Path) -> tuple[float, datetime] | None:
    """Percentile (0-100) of the latest managed-money net %OI within
    the last 52 weekly values. Needs >= 10 values. The percentile is
    the share of values <= the latest — the same convention the COT
    panel already shows for the 52-week rank."""
    history = get_snapshot_history(db_path, "cot_gold_mm_net_pct_oi",
                                   limit=COT_PERCENTILE_WINDOW)
    values = [v for _, v in history]
    if len(values) < COT_PERCENTILE_MIN_VALUES:
        return None
    latest = _latest(history)
    if latest is None:
        return None
    as_of, latest_value = latest
    window = values[-COT_PERCENTILE_WINDOW:]
    rank = sum(1 for v in window if v <= latest_value) / len(window) * 100.0
    return rank, as_of


def etf_flow_tonnes_13w(db_path: str | Path) -> tuple[float, datetime, str] | None:
    """(GLD + IAU tonnes now) minus (GLD + IAU tonnes ~13w ago).

    For each ETF the "then" value is the snapshot closest to 13 weeks
    before the latest date, among snapshots at least 12 weeks old.
    IAU is stored only via manual CSV import, so when it is missing
    at either end the flow is GLD-only and the formula says so.
    Needs at least one ETF with a snapshot >= 12 weeks old, else None.
    """
    now_total = 0.0
    then_total = 0.0
    used: list[str] = []
    latest_as_of: datetime | None = None
    for series_id, label in (("etf_gld_tonnes", "GLD"),
                             ("etf_iau_tonnes", "IAU")):
        history = get_snapshot_history(db_path, series_id, limit=120)
        latest = _latest(history)
        if latest is None:
            continue
        as_of, now_value = latest
        target = as_of - timedelta(days=ETF_FLOW_TARGET_DAYS)
        then = _value_near(history, target,
                           timedelta(days=ETF_FLOW_TARGET_DAYS - ETF_FLOW_MIN_AGE_DAYS))
        if then is None:
            continue
        # The "then" snapshot must be at least 12 weeks old.
        if (as_of - then[0]).days < ETF_FLOW_MIN_AGE_DAYS:
            continue
        now_total += now_value
        then_total += then[1]
        used.append(label)
        latest_as_of = as_of if latest_as_of is None else max(latest_as_of, as_of)
    if not used or latest_as_of is None:
        return None
    formula = (f"{'+'.join(used)} tonnes now minus "
               f"{'+'.join(used)} tonnes ~13w ago")
    if "IAU" not in used:
        formula += " (IAU not stored — manual CSV import only)"
    return now_total - then_total, latest_as_of, formula


def real_yield_change_30d(db_path: str | Path) -> tuple[float, datetime] | None:
    """Latest 10y real yield (fred_dfii10) minus the value closest to
    30 days ago, in percentage points. Needs a value 25-35 days old."""
    history = get_snapshot_history(db_path, "fred_dfii10", limit=60)
    latest = _latest(history)
    if latest is None:
        return None
    as_of, now_value = latest
    old = _value_near(history, as_of - timedelta(days=CHANGE_TARGET_DAYS),
                      timedelta(days=CHANGE_TOLERANCE_DAYS))
    if old is None:
        return None
    return now_value - old[1], as_of


def dxy_change_30d(db_path: str | Path) -> tuple[float, datetime] | None:
    """30-day change of the broad trade-weighted dollar
    (fred_dtwexbgs — the dollar series the FRED macro connector
    stores), in percent. Needs a value 25-35 days old."""
    history = get_snapshot_history(db_path, "fred_dtwexbgs", limit=60)
    latest = _latest(history)
    if latest is None:
        return None
    as_of, now_value = latest
    old = _value_near(history, as_of - timedelta(days=CHANGE_TARGET_DAYS),
                      timedelta(days=CHANGE_TOLERANCE_DAYS))
    if old is None or old[1] == 0:
        return None
    return (now_value - old[1]) / abs(old[1]) * 100.0, as_of


def comex_registered_share_change_30d(db_path: str | Path) -> tuple[float, datetime] | None:
    """Latest COMEX registered share minus the value closest to 30
    days ago, in percentage points. Needs a value 25-35 days old."""
    history = get_snapshot_history(db_path, "comex_registered_share_pct", limit=60)
    latest = _latest(history)
    if latest is None:
        return None
    as_of, now_value = latest
    old = _value_near(history, as_of - timedelta(days=CHANGE_TARGET_DAYS),
                      timedelta(days=CHANGE_TOLERANCE_DAYS))
    if old is None:
        return None
    return now_value - old[1], as_of


# series_id -> (compute function, unit, proxy_formula)
_DERIVED: dict[str, tuple] = {
    "cot_managed_money_net_pctile_52w": (
        cot_percentile_52w, "percentile",
        "percentile of latest cot_gold_mm_net_pct_oi within last 52 weekly values"),
    "etf_flow_tonnes_13w": (
        etf_flow_tonnes_13w, "tonnes", None),  # formula built per run (GLD/IAU)
    "real_yield_10y_change_pp_30d": (
        real_yield_change_30d, "pp",
        "fred_dfii10 now minus fred_dfii10 ~30d ago"),
    "dxy_change_pct_30d": (
        dxy_change_30d, "pct",
        "(fred_dtwexbgs now / fred_dtwexbgs ~30d ago - 1) * 100"),
    "comex_registered_share_change_pp_30d": (
        comex_registered_share_change_30d, "pp",
        "comex_registered_share_pct now minus comex_registered_share_pct ~30d ago"),
}


def compute_derived_series(db_path: str | Path) -> dict[str, float | None]:
    """Compute and save every derived series that has enough history.

    Returns {series_id: value or None}. A series with too little
    history is None and nothing is saved for it. Never raises for a
    single failed computation — that series is None and the rest
    still run (the caller records the failure in payload["errors"]).
    """
    db_path = str(db_path)
    now = datetime.now(timezone.utc)
    results: dict[str, float | None] = {}
    for series_id, (compute, unit, formula) in _DERIVED.items():
        try:
            out = compute(db_path)
        except Exception:
            results[series_id] = None
            continue
        if out is None:
            results[series_id] = None
            continue
        if len(out) == 3:
            value, as_of, formula = out
        else:
            value, as_of = out
        results[series_id] = value
        # Idempotent: skip when the stored derived value already
        # describes the same data date.
        latest_derived = get_latest_snapshot_by_series(db_path, series_id)
        if (latest_derived is not None
                and latest_derived.source == "derived"
                and latest_derived.as_of is not None
                and as_of is not None
                and latest_derived.as_of == as_of):
            continue
        save_snapshot(db_path, SourceSnapshot(
            source="derived", series_id=series_id, value=float(value),
            unit=unit, as_of=as_of, published_at=as_of, fetched_at=now,
            status="ok", source_url="", is_proxy=True,
            proxy_formula=formula,
        ))
    return results
