"""Tests for live headlines: GDELT ArtList + Finnhub /news parsing and
news storage (v0.2.0). No network: provider JSON is faked."""

from datetime import datetime, timezone

from goldboard_core.storage import get_latest_news, init_db, save_news
from goldboard_data.finnhub import FinnhubConnector
from goldboard_data.gdelt import GDELTConnector

GDELT_ARTLIST = {
    "articles": [
        {"title": "Gold hits record as central banks buy",
         "url": "https://example.invalid/gold-record",
         "domain": "example.invalid",
         "seendate": "20261121T203000Z"},
        {"title": "", "url": "https://example.invalid/empty",
         "domain": "example.invalid", "seendate": "20261121T200000Z"},
        {"title": "Gold miners rally", "url": "https://example.invalid/miners",
         "domain": "news.invalid", "seendate": "not-a-date"},
    ]
}

FINNHUB_NEWS = [
    {"headline": "Gold steady as dollar slips", "source": "Reuters",
     "url": "https://example.invalid/gold-steady", "datetime": 1763750000,
     "summary": "Spot gold held firm."},
    {"headline": "Stocks end higher", "source": "Reuters",
     "url": "https://example.invalid/stocks", "datetime": 1763750000,
     "summary": "Equities rose on strong earnings."},
    {"headline": "", "source": "Reuters",
     "url": "https://example.invalid/empty", "datetime": 1763750000},
]


def test_gdelt_artlist_parse():
    items = GDELTConnector().parse_headlines(GDELT_ARTLIST)
    assert len(items) == 2  # empty title skipped
    assert items[0]["title"] == "Gold hits record as central banks buy"
    assert items[0]["url"] == "https://example.invalid/gold-record"
    assert items[0]["source"] == "example.invalid"
    assert items[0]["provider"] == "gdelt"
    assert items[0]["published_at"] == datetime(
        2026, 11, 21, 20, 30, tzinfo=timezone.utc)
    assert items[1]["published_at"] is None  # bad seendate -> None, kept


def test_gdelt_artlist_rejects_empty():
    import pytest
    with pytest.raises(Exception):
        GDELTConnector().parse_headlines({"articles": []})


def test_finnhub_headlines_gold_filtered():
    items = FinnhubConnector(api_key="k").parse_headlines(FINNHUB_NEWS)
    assert len(items) == 1  # non-gold + empty headline skipped
    assert items[0]["title"] == "Gold steady as dollar slips"
    assert items[0]["source"] == "Reuters"
    assert items[0]["provider"] == "finnhub"
    assert items[0]["published_at"] == datetime.fromtimestamp(
        1763750000, tz=timezone.utc)


def test_news_storage_round_trip_and_dedupe(tmp_path):
    db = str(tmp_path / "test.db")
    init_db(db)
    items = [
        {"title": "Gold up", "url": "https://example.invalid/1",
         "source": "a.invalid",
         "published_at": datetime(2026, 11, 21, 20, 0, tzinfo=timezone.utc),
         "provider": "gdelt"},
        {"title": "Gold down", "url": "https://example.invalid/2",
         "source": "b.invalid",
         "published_at": datetime(2026, 11, 21, 19, 0, tzinfo=timezone.utc),
         "provider": "finnhub"},
    ]
    assert save_news(db, items) == 2
    assert save_news(db, items) == 0  # same title+url -> deduped
    stored = get_latest_news(db)
    assert len(stored) == 2
    assert stored[0]["title"] == "Gold up"  # newest first
    assert stored[1]["provider"] == "finnhub"
    assert get_latest_news(db, limit=1)[0]["title"] == "Gold up"
