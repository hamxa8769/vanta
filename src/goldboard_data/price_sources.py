"""Live gold price from every free source, fastest + most trusted
first — the ticker's price feed (v0.3.0).

Priority (probed live 2026-11-21/22 from this VM; the user's Windows
machine may reach sources this VM cannot, so every source is tried
with a short timeout and the failures are reported, never hidden):
  1. gold-api.com — live XAU spot, no key, HTTP 200 in ~1.5s,
     "updated a few seconds ago". The most live free spot feed found.
  2. Yahoo GC=F — front-month COMEX futures as the spot proxy,
     delayed ~15 min; reliable but 429s when hammered (seen live).
  3. metals.live — live XAU spot, no key; unreachable from this VM
     (connection fails), kept as a fallback for networks that reach it.
  4. Stooq gc.f — free daily close (a day delayed); stalls from
     this VM (12s timeout), last resort only.

Rules: try in order, 5s timeout, no retries (one slow source must
never stall the ticker). If two or more live-spot quotes agree
within 0.5%, use their median ("spot consensus"). Otherwise use the
best-priority quote available and label its source + kind (spot /
futures / daily close) on screen. If spot and futures disagree by
more than 1.5% — wider than normal contango + delay — flag
``diverged`` so the log says so instead of silently picking one.
Nothing is ever invented: if every source fails, raise
ConnectorError and the ticker keeps the last price.
"""

from __future__ import annotations

import statistics
from datetime import datetime, timezone

from .base import BaseConnector, ConnectorError
from .price import YAHOO_CHART_URL, PriceConnector

GOLD_API_URL = "https://api.gold-api.com/price/XAU"
METALS_LIVE_URL = "https://api.metals.live/v1/spot/gold"
STOOQ_URL = "https://stooq.com/q/l/?s=gc.f&i=d"

SPOT_AGREE_PCT = 0.5
DIVERGE_PCT = 1.5


def _parse_iso(value) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class GoldAPIConnector(BaseConnector):
    """Live XAU spot from gold-api.com (free, no key)."""

    name = "goldapi"
    source_url = GOLD_API_URL
    refresh_minutes = 1

    def parse(self, raw) -> list:
        try:
            price = float(raw["price"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ConnectorError(f"gold-api: no price in response: {exc}")
        return [self._snapshot(
            "price_xauusd", price, "USD/oz",
            as_of=_parse_iso(raw.get("updatedAt")),
            is_proxy=False, source_url=GOLD_API_URL)]

    def fetch(self) -> list:
        return self.parse(self._get_json(GOLD_API_URL, timeout=5, retries=0))

    def fetch_quote(self) -> dict:
        snap = self.parse(
            self._get_json(GOLD_API_URL, timeout=5, retries=0))[0]
        return {"value": snap.value, "as_of": snap.as_of,
                "source": "gold-api.com", "kind": "spot"}


class MetalsLiveConnector(BaseConnector):
    """Live XAU spot from metals.live (free, no key)."""

    name = "metalslive"
    source_url = METALS_LIVE_URL
    refresh_minutes = 1

    def parse(self, raw) -> list:
        try:
            price = float(raw["price"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ConnectorError(f"metals.live: no price in response: {exc}")
        return [self._snapshot(
            "price_xauusd", price, "USD/oz",
            as_of=_parse_iso(raw.get("updatedAt")),
            is_proxy=False, source_url=METALS_LIVE_URL)]

    def fetch(self) -> list:
        return self.parse(self._get_json(METALS_LIVE_URL, timeout=5,
                                         retries=0))

    def fetch_quote(self) -> dict:
        snap = self.parse(
            self._get_json(METALS_LIVE_URL, timeout=5, retries=0))[0]
        return {"value": snap.value, "as_of": snap.as_of,
                "source": "metals.live", "kind": "spot"}


class StooqConnector(BaseConnector):
    """Daily gold futures close from Stooq (free CSV, a day delayed)."""

    name = "stooq"
    source_url = STOOQ_URL
    refresh_minutes = 60

    def parse(self, raw) -> list:
        lines = [ln.strip() for ln in str(raw).strip().splitlines()
                 if ln.strip()]
        if len(lines) < 2:
            raise ConnectorError("stooq: empty CSV response")
        fields = lines[1].split(",")
        try:
            price = float(fields[6])  # Close column
        except (IndexError, ValueError) as exc:
            raise ConnectorError(f"stooq: no close in CSV: {exc}")
        return [self._snapshot(
            "price_xauusd", price, "USD/oz", as_of=None,
            is_proxy=True,
            proxy_formula="Stooq daily gold futures close (a day delayed) "
                          "as proxy for live XAUUSD spot",
            source_url=STOOQ_URL)]

    def fetch(self) -> list:
        return self.parse(self._get_text(STOOQ_URL, timeout=5, retries=0))

    def fetch_quote(self) -> dict:
        snap = self.parse(self._get_text(STOOQ_URL, timeout=5,
                                         retries=0))[0]
        return {"value": snap.value, "as_of": snap.as_of,
                "source": "stooq.com", "kind": "daily close"}


def _yahoo_quote() -> dict:
    """Yahoo GC=F via the existing PriceConnector (futures proxy)."""
    conn = PriceConnector()
    data = conn._get_json(YAHOO_CHART_URL.format(symbol="GC=F"),
                          params={"range": "1d", "interval": "1d"},
                          timeout=5, retries=0)
    snap = conn.parse(data)[0]
    return {"value": snap.value, "as_of": snap.as_of,
            "source": "Yahoo Finance (GC=F)", "kind": "futures"}


# Priority order: fastest + most trusted first.
SOURCES = [
    ("gold-api.com", lambda: GoldAPIConnector().fetch_quote()),
    ("Yahoo Finance (GC=F)", _yahoo_quote),
    ("metals.live", lambda: MetalsLiveConnector().fetch_quote()),
    ("stooq.com", lambda: StooqConnector().fetch_quote()),
]


def fetch_best_price() -> dict:
    """Best live gold price across the free sources.

    Returns {value, as_of, source, kind, tried, diverged}: ``tried``
    lists every source attempted with ok/error, so the log shows
    exactly what was used and what failed.
    """
    tried: list[dict] = []
    quotes: list[dict] = []
    for name, fetch in SOURCES:
        try:
            quote = fetch()
            tried.append({"source": name, "ok": True, "error": None})
            quotes.append(quote)
        except Exception as exc:  # one dead source never kills the price
            tried.append({"source": name, "ok": False, "error": str(exc)})
    if not quotes:
        raise ConnectorError("price: all free sources failed: " +
                             "; ".join(t["error"] or "?"
                                       for t in tried))
    spots = [q for q in quotes if q["kind"] == "spot"]
    best = quotes[0]
    if len(spots) >= 2:
        values = sorted(q["value"] for q in spots)
        spread_pct = (values[-1] - values[0]) / values[0] * 100.0
        if spread_pct <= SPOT_AGREE_PCT:
            best = {"value": statistics.median(values),
                    "as_of": max((q["as_of"] for q in spots
                                 if q["as_of"] is not None), default=None),
                    "source": "spot consensus (" +
                              ", ".join(q["source"] for q in spots) + ")",
                    "kind": "spot"}
    diverged = False
    if best["kind"] == "spot":
        futures = [q for q in quotes if q["kind"] == "futures"]
        if futures and abs(best["value"] - futures[0]["value"]) \
                / futures[0]["value"] * 100.0 > DIVERGE_PCT:
            diverged = True
    return {"value": best["value"], "as_of": best["as_of"],
            "source": best["source"], "kind": best["kind"],
            "tried": tried, "diverged": diverged,
            "quotes": quotes}
