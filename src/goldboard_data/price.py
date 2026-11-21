"""XAUUSD spot price connector — free Yahoo Finance quote (delayed/proxy).

Free feed: Yahoo Finance public chart API
  https://query1.finance.yahoo.com/v8/finance/chart/GC=F
(front-month COMEX gold futures as the spot proxy). XAUUSD=X was
checked live 2026-11-21 and 404s on Yahoo, so it is NOT used.
Labelled delayed/proxy on every snapshot — there is no paid real-time
feed in this board. No price is ever invented: on failure fetch()
returns [] and logs.
"""

from __future__ import annotations

from datetime import datetime, timezone

from .base import BaseConnector, ConnectorError

YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
SYMBOLS = ("GC=F",)  # XAUUSD=X 404s on Yahoo (checked 2026-11-21)


class PriceConnector(BaseConnector):
    name = "price"
    source_url = YAHOO_CHART_URL.format(symbol="GC=F")
    refresh_minutes = 15

    def parse(self, raw) -> list:
        """Parse a Yahoo chart JSON dict into a price snapshot."""
        if not isinstance(raw, dict):
            raise ConnectorError("price: expected Yahoo chart JSON")
        try:
            meta = raw["chart"]["result"][0]["meta"]
            price = float(meta["regularMarketPrice"])
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ConnectorError(f"price: no price in Yahoo chart JSON: {exc}")
        ts = meta.get("regularMarketTime")
        as_of = (datetime.fromtimestamp(ts, tz=timezone.utc)
                 if isinstance(ts, (int, float)) else None)
        symbol = str(meta.get("symbol") or "GC=F")
        return [self._snapshot(
            "price_xauusd", price, "USD/oz", as_of=as_of, is_proxy=True,
            proxy_formula=f"Yahoo Finance delayed quote ({symbol}) as proxy "
                          "for live XAUUSD spot",
            source_url=YAHOO_CHART_URL.format(symbol=symbol))]

    def fetch(self) -> list:
        errors: list[str] = []
        for symbol in SYMBOLS:
            try:
                data = self._get_json(YAHOO_CHART_URL.format(symbol=symbol),
                                      params={"range": "1d", "interval": "1d"})
                return self.parse(data)
            except ConnectorError as exc:
                errors.append(f"{symbol}: {exc}")
                self.log.warning("price: %s", exc)
        return self._fail("price: " + "; ".join(errors))
