"""Market microstructure connector — futures curve, implied lease proxy,
GVZ, CME gold options OI.

- Futures curve: front-month GC=F and a dated COMEX contract ~12 months
  out, via the public Yahoo Finance chart API
  (https://query1.finance.yahoo.com/v8/finance/chart/<symbol>).
  Dated contracts use Yahoo's ".CMX" suffix (e.g. GCZ27.CMX for
  Dec 2027); bare symbols (GCZ27) 404 on Yahoo. The far leg tries
  Dec, then Aug / Jun / Apr / Feb of the target year, and annualises
  the carry by actual days to the delivery month. Gold is almost
  always in contango (carry > 0); backwardation is a rare regime
  event. These are Yahoo delayed quotes, NOT CME settlements (no
  stable verbatim CME settlements URL was verifiable); the docstring
  and snapshots say so.
- Implied lease proxy: GOFO was discontinued by LBMA in Jan 2015 —
  there is NO public lease-rate benchmark. The only honest figure is a
  futures-derived proxy, ALWAYS labelled:
      implied_lease = SOFR - futures_carry_12m
  with is_proxy=True and the formula on the snapshot. SOFR comes from
  FRED (keyless fredgraph.csv).
- GVZ (Cboe Gold ETF Volatility Index, 30-day IV from GLD options):
  Yahoo public quote ^GVZ, labelled as a proxy source.
- CME gold options OI (QuikStrike heatmap, pid=23):
  https://www.cmegroup.com/tools-information/quikstrike/open-interest-heatmap.html?pid=23
  Put/call OI ratio is returned only if parseable from the page;
  otherwise ``fetch_options_oi()`` raises ConnectorError with the reason
  (the page is JS-rendered; no number is guessed).
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from .base import BaseConnector, ConnectorError

YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
QUIKSTRIKE_URL = ("https://www.cmegroup.com/tools-information/quikstrike/"
                  "open-interest-heatmap.html?pid=23")
LEASE_FORMULA = "implied_lease = SOFR - futures_carry_12m"


# COMEX month codes, tried in order of closeness to 12 months out.
_MONTH_CODES = (("Z", 12), ("Q", 8), ("M", 6), ("J", 4), ("G", 2))


def _far_contract_candidates(months_ahead: int = 12) -> list[tuple[str, datetime]]:
    """Dated COMEX symbols near ``months_ahead`` out, with delivery dates.

    Yahoo serves dated COMEX gold only with the ".CMX" suffix
    (GCZ27.CMX); bare symbols 404.
    """
    target = datetime.now(timezone.utc) + timedelta(days=30 * months_ahead)
    year = target.year
    out = []
    for code, month in _MONTH_CODES:
        out.append((f"GC{code}{year % 100:02d}.CMX",
                    datetime(year, month, 15, tzinfo=timezone.utc)))
    return out


class MicrostructureConnector(BaseConnector):
    name = "microstructure"
    source_url = YAHOO_CHART_URL.format(symbol="GC=F")
    refresh_minutes = 60 * 24  # daily

    # -- Yahoo ---------------------------------------------------------
    def _yahoo_price(self, symbol: str) -> tuple[float, datetime | None]:
        data = self._get_json(YAHOO_CHART_URL.format(symbol=symbol),
                              params={"range": "5d", "interval": "1d"})
        try:
            meta = data["chart"]["result"][0]["meta"]
            price = float(meta["regularMarketPrice"])
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ConnectorError(
                f"microstructure: Yahoo chart for {symbol} has no price: {exc}")
        ts = meta.get("regularMarketTime")
        as_of = (datetime.fromtimestamp(ts, tz=timezone.utc)
                 if isinstance(ts, (int, float)) else None)
        return price, as_of

    def _sofr(self) -> float:
        """SOFR (%) via keyless FRED fredgraph.csv."""
        from .fred import FREDConnector
        snaps = FREDConnector().fetch_series("SOFR")
        if not snaps:
            raise ConnectorError("microstructure: SOFR unavailable from FRED")
        return snaps[0].value

    # -- parsing -------------------------------------------------------
    def parse(self, raw) -> list:
        """Parse a Yahoo chart JSON dict into a price snapshot (front month)."""
        if not isinstance(raw, dict):
            raise ConnectorError("microstructure: expected Yahoo chart JSON")
        try:
            meta = raw["chart"]["result"][0]["meta"]
            price = float(meta["regularMarketPrice"])
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ConnectorError(f"microstructure: no price in chart JSON: {exc}")
        return [self._snapshot("futures_front_price", price, "USD/oz",
                               is_proxy=True,
                               proxy_formula="Yahoo Finance delayed quote "
                                             "(GC=F) — proxy, not CME settlement")]

    def _far_contract(self) -> tuple[str, float, int]:
        """First resolvable dated COMEX contract near 12M out.

        Returns (symbol, price, days to delivery month). Raises
        ConnectorError if no dated contract resolves on Yahoo.
        """
        errors: list[str] = []
        for symbol, delivery in _far_contract_candidates():
            try:
                price, _ = self._yahoo_price(symbol)
            except ConnectorError as exc:
                errors.append(f"{symbol}: {exc}")
                continue
            days = max(1, (delivery - datetime.now(timezone.utc)).days)
            return symbol, price, days
        raise ConnectorError(
            "microstructure: no dated COMEX contract quote available "
            "from Yahoo (tried " +
            ", ".join(s for s, _ in _far_contract_candidates()) +
            "); CME settlements need a licensed feed — carry unavailable")

    def fetch_carry(self) -> list:
        front, as_of = self._yahoo_price("GC=F")
        far_symbol, far, days = self._far_contract()
        if front <= 0:
            raise ConnectorError("microstructure: front-month price <= 0")
        # Annualised by actual days to the far delivery month.
        carry_pct = (far / front - 1.0) * (365.0 / days) * 100.0
        regime = "contango" if carry_pct >= 0 else "backwardation"
        snaps = [
            self._snapshot("futures_carry_12m_pct", carry_pct, "pct",
                           as_of=as_of, is_proxy=True,
                           proxy_formula=f"carry_12m = (({far_symbol} / GC=F) - 1) "
                                         f"* 365/{days}d, from Yahoo delayed quotes — "
                                         "proxy, not CME settlements",
                           vintage=f"curve regime: {regime}"),
            self._snapshot("futures_front_price", front, "USD/oz", as_of=as_of,
                           is_proxy=True,
                           proxy_formula="Yahoo Finance delayed quote (GC=F)"),
            self._snapshot("futures_12m_price", far, "USD/oz", as_of=as_of,
                           is_proxy=True,
                           proxy_formula=f"Yahoo Finance delayed quote ({far_symbol})"),
        ]
        try:
            sofr = self._sofr()
            snaps.append(self._snapshot(
                "implied_lease_proxy_pct", sofr - carry_pct, "pct",
                as_of=as_of, is_proxy=True, proxy_formula=LEASE_FORMULA,
                vintage="GOFO discontinued Jan 2015 — no public lease "
                        "benchmark; futures-derived proxy only"))
        except ConnectorError as exc:
            self.log.warning("microstructure: implied lease unavailable: %s", exc)
        return snaps

    def fetch_gvz(self) -> list:
        price, as_of = self._yahoo_price("^GVZ")
        return [self._snapshot("gvz", price, "index points", as_of=as_of,
                               is_proxy=True,
                               proxy_formula="Yahoo Finance ^GVZ quote "
                                             "(proxy for Cboe GVZ)")]

    def fetch_options_oi(self) -> list:
        """CME gold options put/call OI ratio — only if parseable."""
        html = self._get_text(QUIKSTRIKE_URL)
        text = re.sub(r"<[^>]+>", " ", html)
        text = re.sub(r"\s+", " ", text)
        puts = re.search(r"puts?[^0-9]{0,80}?([\d,]+)", text, re.IGNORECASE)
        calls = re.search(r"calls?[^0-9]{0,80}?([\d,]+)", text, re.IGNORECASE)
        if not puts or not calls:
            raise ConnectorError(
                "microstructure: CME QuikStrike OI not parseable from the "
                "static page (JS-rendered heatmap) — put/call OI ratio "
                "unavailable, not guessed")
        put_oi = float(puts.group(1).replace(",", ""))
        call_oi = float(calls.group(1).replace(",", ""))
        if call_oi == 0:
            raise ConnectorError("microstructure: call OI is zero in parsed data")
        return [self._snapshot("cme_gold_options_put_call_oi_ratio",
                               put_oi / call_oi, "ratio",
                               source_url=QUIKSTRIKE_URL)]

    def fetch(self) -> list:
        snaps: list = []
        errors: list[str] = []
        for fn in (self.fetch_carry, self.fetch_gvz):
            try:
                snaps += fn()
            except ConnectorError as exc:
                errors.append(str(exc))
                self.log.warning("microstructure: %s", exc)
        try:
            snaps += self.fetch_options_oi()
        except ConnectorError as exc:
            # Options OI is a supplement; carry/GVZ still stand.
            self.log.info("microstructure: options OI unavailable: %s", exc)
            errors.append(str(exc))
        if not snaps:
            return self._fail("microstructure: " + "; ".join(errors))
        self.last_error = "; ".join(errors) if errors else None
        return snaps
