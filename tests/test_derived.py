"""Derived series (v0.6.0) — the COT alias + history-derived pillars.

The score contract asks for derived quantities (52w percentile, 13w
flow, 30d changes) but the connectors store raw latest values. These
tests seed a DB the way the connectors do and check that the derived
step fills the pillars — and that it returns None (saving nothing)
when there is not enough history. Never invented, never extrapolated.
"""

from datetime import datetime, timedelta, timezone

from goldboard_app.worker import collect_dashboard_data
from goldboard_core.api import get_features, get_score
from goldboard_core.derived import (
    comex_registered_share_change_30d,
    compute_derived_series,
    cot_percentile_52w,
    dxy_change_30d,
    etf_flow_tonnes_13w,
    real_yield_change_30d,
)
from goldboard_core.models import SourceSnapshot
from goldboard_core.storage import (
    get_latest_snapshot_by_series,
    get_snapshot_history,
    init_db,
    save_snapshot,
)

NOW = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)


def _seed(db, source, series_id, dated_values):
    """dated_values: list of (days_ago, value) — as_of and fetched_at
    both set to that date, oldest first."""
    for days_ago, value in dated_values:
        when = NOW - timedelta(days=days_ago)
        save_snapshot(db, SourceSnapshot(
            source=source, series_id=series_id, value=float(value),
            unit="", as_of=when, published_at=when, fetched_at=when,
            status="ok", source_url=f"https://example.invalid/{source}"))


def _db(tmp_path):
    path = tmp_path / "test.db"
    init_db(path)
    return path


def _count_series(db, series_id):
    import sqlite3
    with sqlite3.connect(str(db)) as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM snapshots WHERE series_id = ?",
            (series_id,)).fetchone()[0]


# ------------------------------------------------------------------ alias

def test_cot_alias_maps_live_value(tmp_path):
    """The e2e proof case: 25.24% of OI -> clamp(25.24 * 2) = 50.48."""
    db = _db(tmp_path)
    _seed(db, "cftc", "cot_gold_mm_net_pct_oi", [(0, 25.24)])
    features = get_features(db)
    assert features["cot"] == 50.48
    assert features["central_bank_etf"] is None
    assert features["macro"] is None
    assert features["news"] is None
    assert features["microstructure"] is None


def test_cot_alias_does_not_map_noncommercial(tmp_path):
    """Only managed money is aliased — the non-commercial id must
    stay unmapped (different classification, pillar stays faithful)."""
    db = _db(tmp_path)
    _seed(db, "cftc", "cot_gold_nc_net_pct_oi", [(0, 25.24)])
    assert get_features(db)["cot"] is None


def test_worker_refresh_false_fills_score_from_cot_alias(tmp_path):
    db = _db(tmp_path)
    _seed(db, "cftc", "cot_gold_mm_net_pct_oi", [(0, 25.24)])
    payload = collect_dashboard_data(db, refresh=False)
    assert payload["score"] is not None
    assert payload["score"].total == 50.48
    assert len(payload["score"].components) == 1
    assert payload["score"].components[0].name == "cot"


# ------------------------------------------------------- history helper

def test_get_snapshot_history_oldest_first_excludes_failed(tmp_path):
    db = _db(tmp_path)
    _seed(db, "fred", "fred_dfii10", [(30, 3.10), (10, 3.0), (0, 2.88)])
    save_snapshot(db, SourceSnapshot(
        source="fred", series_id="fred_dfii10", value=99.0, unit="%",
        as_of=NOW - timedelta(days=5),
        fetched_at=NOW - timedelta(days=5), status="failed"))
    history = get_snapshot_history(db, "fred_dfii10")
    assert [v for _, v in history] == [3.10, 3.0, 2.88]
    assert history[0][0] == NOW - timedelta(days=30)


# --------------------------------------------------------- COT percentile

def test_cot_percentile_latest_is_max(tmp_path):
    db = _db(tmp_path)
    # 52 weekly values, 1..52, latest (this week) is the highest.
    _seed(db, "cftc", "cot_gold_mm_net_pct_oi",
          [(7 * (51 - i), float(i + 1)) for i in range(52)])
    out = cot_percentile_52w(db)
    assert out is not None
    assert out[0] == 100.0
    assert out[1] == NOW


def test_cot_percentile_mid_value(tmp_path):
    db = _db(tmp_path)
    # 51 weekly values 1..51, latest (this week) 25.5: exactly 26 of
    # the 52 window values are <= 25.5, so the percentile is 50.0.
    _seed(db, "cftc", "cot_gold_mm_net_pct_oi",
          [(7 * (51 - i), float(i + 1)) for i in range(51)])
    _seed(db, "cftc", "cot_gold_mm_net_pct_oi", [(0, 25.5)])
    out = cot_percentile_52w(db)
    assert out is not None
    assert out[0] == 50.0


def test_cot_percentile_needs_ten_values(tmp_path):
    db = _db(tmp_path)
    _seed(db, "cftc", "cot_gold_mm_net_pct_oi",
          [(7 * (8 - i), float(i + 1)) for i in range(9)])
    assert cot_percentile_52w(db) is None
    assert _count_series(db, "cot_managed_money_net_pctile_52w") == 0


def test_derived_percentile_feeds_get_features(tmp_path):
    db = _db(tmp_path)
    _seed(db, "cftc", "cot_gold_mm_net_pct_oi",
          [(7 * (51 - i), float(i + 1)) for i in range(52)])
    results = compute_derived_series(db)
    assert results["cot_managed_money_net_pctile_52w"] == 100.0
    # The percentile outranks the raw %OI in COMPONENT_SERIES.
    assert get_features(db)["cot"] == 100.0
    snap = get_latest_snapshot_by_series(
        db, "cot_managed_money_net_pctile_52w")
    assert snap is not None
    assert snap.source == "derived"
    assert snap.is_proxy is True
    assert "52 weekly" in (snap.proxy_formula or "")


def test_compute_derived_is_idempotent(tmp_path):
    db = _db(tmp_path)
    _seed(db, "cftc", "cot_gold_mm_net_pct_oi",
          [(7 * (51 - i), float(i + 1)) for i in range(52)])
    compute_derived_series(db)
    compute_derived_series(db)
    assert _count_series(db, "cot_managed_money_net_pctile_52w") == 1


# -------------------------------------------------------------- ETF flow

def test_etf_flow_gld_plus_iau(tmp_path):
    db = _db(tmp_path)
    _seed(db, "etf", "etf_gld_tonnes", [(91, 1000.0), (0, 1038.14)])
    _seed(db, "etf", "etf_iau_tonnes", [(91, 500.0), (0, 510.0)])
    out = etf_flow_tonnes_13w(db)
    assert out is not None
    assert abs(out[0] - 48.14) < 0.01
    assert "GLD+IAU" in out[2]


def test_etf_flow_gld_only_when_iau_missing(tmp_path):
    db = _db(tmp_path)
    _seed(db, "etf", "etf_gld_tonnes", [(91, 1000.0), (0, 1038.14)])
    out = etf_flow_tonnes_13w(db)
    assert out is not None
    assert abs(out[0] - 38.14) < 0.01
    assert "IAU not stored" in out[2]


def test_etf_flow_needs_twelve_week_old_snapshot(tmp_path):
    db = _db(tmp_path)
    _seed(db, "etf", "etf_gld_tonnes", [(30, 1000.0), (0, 1038.14)])
    assert etf_flow_tonnes_13w(db) is None
    assert compute_derived_series(db)["etf_flow_tonnes_13w"] is None


# ------------------------------------------------------- 30-day changes

def test_real_yield_change_30d(tmp_path):
    db = _db(tmp_path)
    _seed(db, "fred", "fred_dfii10", [(30, 3.10), (0, 2.88)])
    out = real_yield_change_30d(db)
    assert out is not None
    assert abs(out[0] - (-0.22)) < 0.001


def test_real_yield_change_needs_25_to_35_day_old_value(tmp_path):
    db = _db(tmp_path)
    _seed(db, "fred", "fred_dfii10", [(10, 3.0), (0, 2.88)])
    assert real_yield_change_30d(db) is None


def test_dxy_change_30d(tmp_path):
    db = _db(tmp_path)
    _seed(db, "fred", "fred_dtwexbgs", [(30, 98.0), (0, 100.0)])
    out = dxy_change_30d(db)
    assert out is not None
    assert abs(out[0] - 2.0408) < 0.01


def test_comex_share_change_30d(tmp_path):
    db = _db(tmp_path)
    _seed(db, "comex", "comex_registered_share_pct", [(30, 60.0), (0, 62.1)])
    out = comex_registered_share_change_30d(db)
    assert out is not None
    assert abs(out[0] - 2.1) < 0.001


def test_all_derived_fill_score_pillars(tmp_path):
    """A DB with enough history fills four of the five pillars
    (news stays None — no GDELT fetch in the test)."""
    db = _db(tmp_path)
    _seed(db, "cftc", "cot_gold_mm_net_pct_oi",
          [(7 * (51 - i), float(i + 1)) for i in range(52)])
    _seed(db, "etf", "etf_gld_tonnes", [(91, 1000.0), (0, 1038.14)])
    _seed(db, "fred", "fred_dfii10", [(30, 3.10), (0, 2.88)])
    _seed(db, "fred", "fred_dtwexbgs", [(30, 98.0), (0, 100.0)])
    _seed(db, "comex", "comex_registered_share_pct", [(30, 60.0), (0, 62.1)])
    results = compute_derived_series(db)
    assert results["etf_flow_tonnes_13w"] is not None
    assert results["real_yield_10y_change_pp_30d"] is not None
    assert results["dxy_change_pct_30d"] is not None
    assert results["comex_registered_share_change_pp_30d"] is not None
    features = get_features(db)
    assert all(features[p] is not None for p in
               ("cot", "central_bank_etf", "macro", "microstructure"))
    assert features["news"] is None
    score = get_score(db)
    assert score is not None
    assert len(score.components) == 4
