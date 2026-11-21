"""refresh_all persistence tests.

v0.1.1 bug: ``refresh_all`` fetched snapshots from every connector
but stored none of them, so ``collect_dashboard_data`` always read an
empty DB and every panel stayed on "no data yet" — the app could never
become functional no matter how well the connectors worked.
"""

import goldboard_data.registry as registry_mod
from goldboard_core.models import SourceSnapshot
from goldboard_core.storage import get_latest_snapshot_by_series
from goldboard_data.base import BaseConnector, ConnectorError
from goldboard_data.registry import refresh_all


class FakeOK(BaseConnector):
    name = "fakeok"

    def fetch(self):
        return [
            SourceSnapshot(
                source="fakeok", series_id="fake_series", value=42.0, unit="x"
            )
        ]

    def parse(self, raw):
        return []


class FakeBroken(BaseConnector):
    name = "fakebroken"

    def fetch(self):
        raise ConnectorError("boom")

    def parse(self, raw):
        return []


def test_refresh_all_persists_snapshots(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    monkeypatch.setattr(
        registry_mod, "REGISTRY", {"fakeok": FakeOK, "fakebroken": FakeBroken}
    )
    report = refresh_all(str(db))
    assert report["fakeok"]["status"] == "ok"
    assert report["fakeok"]["persisted"] == 1
    assert report["fakebroken"]["status"] == "failed"
    assert report["fakebroken"]["persisted"] == 0
    snap = get_latest_snapshot_by_series(str(db), "fake_series")
    assert snap is not None
    assert snap.value == 42.0
    assert snap.source == "fakeok"


def test_refresh_all_without_db_path_persists_nothing(monkeypatch):
    monkeypatch.setattr(registry_mod, "REGISTRY", {"fakeok": FakeOK})
    report = refresh_all(None)
    assert report["fakeok"]["status"] == "ok"
    assert report["fakeok"]["persisted"] == 0


def test_refresh_all_broken_source_blocks_nothing(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    monkeypatch.setattr(
        registry_mod, "REGISTRY", {"fakebroken": FakeBroken, "fakeok": FakeOK}
    )
    report = refresh_all(str(db))
    assert report["fakebroken"]["status"] == "failed"
    assert report["fakeok"]["status"] == "ok"
    assert get_latest_snapshot_by_series(str(db), "fake_series") is not None
