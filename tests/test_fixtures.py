"""Tests that load the Q3 2026 fixture (test data only, never live data)."""

import json
from datetime import datetime
from pathlib import Path

from goldboard_core.api import get_features
from goldboard_core.models import SourceSnapshot
from goldboard_core.storage import init_db, save_snapshot

FIXTURE = Path(__file__).resolve().parents[1] / "data" / "fixtures" / "q3_2026_snapshot.json"


def test_fixture_values_match_research():
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert "TEST FIXTURE ONLY" in data["label"]
    values = data["values"]
    assert values["total_demand_t_q3_2026"] == 1369
    assert values["mine_production_t_q3_2026"] == 1071
    assert values["jewellery_demand_t_q3_2026"] == 313
    assert values["aisc_usd_per_oz_q2_2026"] == 1930
    assert values["gld_holdings_t_2026_11_20"] == 1034
    assert values["managed_money_net_contracts_2026_11_17"] == 102942
    assert values["fed_funds_target_pct"] == "3.75-4.00"


def test_fixture_snapshots_seed_and_map(tmp_path):
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    db = tmp_path / "f.db"
    init_db(db)
    for raw in data["snapshots"]:
        save_snapshot(
            db,
            SourceSnapshot(
                source=raw["source"],
                series_id=raw["series_id"],
                value=float(raw["value"]),
                unit=raw["unit"],
                as_of=datetime.fromisoformat(raw["as_of"]),
                fetched_at=datetime.fromisoformat(raw["as_of"]),
                source_url=raw["source_url"],
            ),
        )
    features = get_features(db)
    # WGC 321t / 3 months maps to the top of the scale (321/3 = 107 -> 100).
    assert features["central_bank_etf"] == 100.0
    # Raw COT contract count has no honest scale mapping -> stays None.
    assert features["cot"] is None
