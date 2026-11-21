"""Finnhub connector — news + news sentiment (free tier: 60 calls/min).

Endpoints (https://finnhub.io/api/v1):
- GET /news?category=general&token=<key>  (general market news)
- GET /news-sentiment?symbol=GLD&token=<key>  (buzz + bullish %)
Key from env GOLD_BOARD_FINNHUB_KEY. Without a key, fetch() raises
ConnectorError("Finnhub API key required ...") — never returns fake
sentiment. Gold coverage here is general news filtered for "gold";
there is no gold-specific Finnhub sentiment score.
"""

from __future__ import annotations

import os

from .base import BaseConnector, ConnectorError

FINNHUB_BASE = "https://finnhub.io/api/v1"
KEY_REQUIRED_MSG = ("Finnhub API key required (free tier: 60 calls/min) — "
                    "set GOLD_BOARD_FINNHUB_KEY")


class FinnhubConnector(BaseConnector):
    name = "finnhub"
    source_url = "https://finnhub.io/docs/api"
    refresh_minutes = 60

    def __init__(self, api_key: str | None = None, **kw) -> None:
        super().__init__(**kw)
        self.api_key = api_key or os.environ.get("GOLD_BOARD_FINNHUB_KEY")

    def parse(self, raw) -> list:
        """Parse a news list (filters gold headlines) or a sentiment dict."""
        if isinstance(raw, dict):
            return self.parse_sentiment(raw)
        return self.parse_news(raw)

    def parse_news(self, items) -> list:
        if not isinstance(items, list):
            raise ConnectorError("finnhub: /news did not return a list")
        gold = [a for a in items if isinstance(a, dict)
                and "gold" in ((a.get("headline") or "")
                               + " " + (a.get("summary") or "")).lower()]
        return [self._snapshot("finnhub_gold_news_count", float(len(gold)),
                               "articles",
                               vintage="Finnhub general news, gold-filtered")]

    def parse_sentiment(self, data: dict) -> list:
        if not isinstance(data, dict):
            raise ConnectorError("finnhub: sentiment did not return an object")
        sent = data.get("sentiment") or {}
        if "bullishPercent" not in sent:
            raise ConnectorError(
                "finnhub: /news-sentiment JSON has no bullishPercent")
        # Finnhub returns bullishPercent as a 0-1 fraction; normalise to %.
        bullish = float(sent["bullishPercent"])
        if bullish <= 1.0:
            bullish *= 100.0
        return [self._snapshot("finnhub_gld_bullish_pct",
                               bullish, "pct",
                               vintage="Finnhub news sentiment, symbol GLD")]

    def fetch(self) -> list:
        if not self.api_key:
            raise ConnectorError(KEY_REQUIRED_MSG)
        snaps: list = []
        errors: list[str] = []
        try:
            snaps += self.parse_news(self._get_json(
                f"{FINNHUB_BASE}/news",
                params={"category": "general", "token": self.api_key}))
        except ConnectorError as exc:
            errors.append(str(exc))
            self.log.warning("finnhub: news failed: %s", exc)
        try:
            snaps += self.parse_sentiment(self._get_json(
                f"{FINNHUB_BASE}/news-sentiment",
                params={"symbol": "GLD", "token": self.api_key}))
        except ConnectorError as exc:
            errors.append(str(exc))
            self.log.warning("finnhub: sentiment failed: %s", exc)
        if not snaps:
            return self._fail("finnhub: " + "; ".join(errors))
        self.last_error = "; ".join(errors) if errors else None
        return snaps
