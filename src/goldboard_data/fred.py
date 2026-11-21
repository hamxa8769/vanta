"""FRED macro connector (Federal Reserve Bank of St. Louis).

Free FRED API key (env GOLD_BOARD_FRED_KEY, 120 req/min) via
  https://api.stlouisfed.org/fred/series/observations
Without a key, the keyless CSV fallback is used for single series:
  https://fred.stlouisfed.org/graph/fredgraph.csv?id=<series>
FRED republishes BLS / BEA / Treasury / Fed data — it is the aggregator,
not the originator; series definitions belong to the originator.

Series tracked (from research, 2026-11-21):
  DFII10 10y TIPS real yield, DGS10/DGS2 Treasury yields,
  DTWEXBGS broad trade-weighted dollar, CPIAUCSL/CPILFESL CPI,
  PPIACO PPI, PAYEMS payrolls, UNRATE unemployment,
  DCOILWTICO WTI, SOFR, USEPUINDXD policy uncertainty (daily, revises
  for 30 days — store the vintage with every print).
"""

from __future__ import annotations

import csv
import io
import os
from datetime import datetime, timezone

from .base import BaseConnector, ConnectorError

FRED_API_URL = "https://api.stlouisfed.org/fred/series/observations"
FRED_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"

SERIES_UNITS = {
    "DFII10": "%",
    "DGS10": "%",
    "DGS2": "%",
    "DTWEXBGS": "index",
    "CPIAUCSL": "index",
    "CPILFESL": "index",
    "PPIACO": "index",
    "PAYEMS": "thousands",
    "UNRATE": "%",
    "DCOILWTICO": "USD/bbl",
    "SOFR": "%",
    "USEPUINDXD": "index",
}
SERIES_IDS = list(SERIES_UNITS)


def _parse_date(s: str) -> datetime | None:
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(s.strip(), fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


class FREDConnector(BaseConnector):
    name = "fred"
    source_url = "https://fred.stlouisfed.org/"
    refresh_minutes = 60 * 24  # daily

    def __init__(self, api_key: str | None = None, **kw) -> None:
        super().__init__(**kw)
        # Env first, then the config file (Settings page), then keyless
        # CSV fallback.
        self.api_key = api_key or os.environ.get("GOLD_BOARD_FRED_KEY")
        if not self.api_key:
            try:
                from goldboard_core.config import load_config
                self.api_key = load_config().fred_api_key or None
            except Exception:
                pass

    # -- parsing ------------------------------------------------------
    def parse(self, raw) -> list:
        """Parse one series' observations (FRED API JSON or fredgraph CSV)."""
        if isinstance(raw, dict):
            return self.parse_api_json(raw)
        return self.parse_csv(str(raw))

    def parse_api_json(self, data: dict, series_id: str | None = None) -> list:
        obs = data.get("observations") if isinstance(data, dict) else None
        if not obs:
            raise ConnectorError("fred: API JSON has no 'observations'")
        sid = series_id or getattr(self, "_current_series", None) or "unknown"
        return [self._latest_from_pairs(sid, [
            (_parse_date(o.get("date", "")), o.get("value"))
            for o in obs])]

    def parse_csv(self, text: str, series_id: str | None = None) -> list:
        rows = list(csv.reader(io.StringIO(text)))
        if len(rows) < 2 or len(rows[0]) < 2:
            raise ConnectorError("fred: fredgraph.csv has no data rows")
        sid = series_id or rows[0][1].strip()
        pairs = [(_parse_date(r[0]), r[1] if len(r) > 1 else ".")
                 for r in rows[1:] if r]
        return [self._latest_from_pairs(sid, pairs)]

    def _latest_from_pairs(self, series_id: str, pairs) -> object:
        """Latest non-missing observation ('.' = missing in FRED)."""
        vintage = None
        for date, val in reversed(pairs):
            if val is None or str(val).strip() in (".", ""):
                continue
            try:
                value = float(val)
            except (TypeError, ValueError):
                continue
            vintage = date.strftime("%Y-%m-%d") if date else None
            status = "ok"
            if date and (datetime.now(timezone.utc) - date).days > 45:
                status = "stale"
            return self._snapshot(
                f"fred_{series_id.lower()}", value,
                SERIES_UNITS.get(series_id, ""),
                as_of=date, status=status, vintage=vintage,
                source_url=f"https://fred.stlouisfed.org/series/{series_id}")
        raise ConnectorError(f"fred: no usable observation for {series_id}")

    # -- fetching -----------------------------------------------------
    def fetch_series(self, series_id: str) -> list:
        self._current_series = series_id
        if self.api_key:
            data = self._get_json(FRED_API_URL, params={
                "series_id": series_id, "api_key": self.api_key,
                "file_type": "json", "sort_order": "desc", "limit": 5})
            return self.parse_api_json(data, series_id)
        text = self._get_text(FRED_CSV_URL, params={"id": series_id})
        return self.parse_csv(text, series_id)

    def fetch(self) -> list:
        return self.fetch_macro()

    def fetch_macro(self) -> list:
        # The 12 series fetch concurrently: one stalled fredgraph.csv
        # (3 attempts x 15s) must not hold up the other 11 series.
        from concurrent.futures import ThreadPoolExecutor, as_completed

        snaps: list = []
        errors: list[str] = []
        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = {pool.submit(self.fetch_series, sid): sid
                       for sid in SERIES_IDS}
            for fut in as_completed(futures):
                sid = futures[fut]
                try:
                    snaps += fut.result()
                except ConnectorError as exc:
                    errors.append(str(exc))
                    self.log.warning("fred: %s failed: %s", sid, exc)
        if not snaps:
            return self._fail("fred: all series failed: " + "; ".join(errors))
        if errors:
            self.last_error = "; ".join(errors)
        return snaps
