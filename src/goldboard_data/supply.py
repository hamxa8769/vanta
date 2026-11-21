"""Supply / demand connector — WGC quarterly balance, USGS mine output, AISC.

- WGC quarterly supply/demand: reuse the WGC manual-import parser
  (``wgc.parse_wgc_csv``) via ``parse_supply_csv(path)`` — the WGC
  balance (mine production, recycling, jewellery, bar & coin, ETFs,
  central banks, technology, net producer hedging) has no public API and
  its underlying Metals Focus / GFMS data are paid; the free summary is
  a user-downloaded file. Store every vintage (revisions happen).
- USGS annual mine production: public statistics page
  https://www.usgs.gov/centers/national-minerals-information-center/gold-statistics-and-information
  (annual Mineral Commodity Summaries; world 2025 output 3,300t on the
  USGS basis — do NOT mix with the WGC/Metals Focus 3,815t figure, the
  gap is methodology). NOTE (2026-11-21): the USGS page is JS-rendered
  — a static fetch returns an almost empty shell, so fetch() fails
  loudly and the manual-import path (parse_supply_csv) is the working
  route for USGS figures until a stable USGS file URL is verified.
- AISC (all-in sustaining cost, WGC definition): parse public WGC/Metals
  Focus cost data where available, else manual import through the same
  ``parse_supply_csv`` (rows with unit USD/oz). AISC is a floor/margin
  indicator (spot − AISC), not a price target.
"""

from __future__ import annotations

import csv
import re

from .base import BaseConnector, ConnectorError
from .wgc import parse_date_loose

USGS_PAGE_URL = ("https://www.usgs.gov/centers/national-minerals-information-center/"
                 "gold-statistics-and-information")


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.strip().lower()).strip("_")


class SupplyConnector(BaseConnector):
    name = "supply"
    source_url = USGS_PAGE_URL
    refresh_minutes = 60 * 24 * 30  # quarterly / annual data

    def parse(self, raw) -> list:
        """Parse USGS statistics page text for a world mine-production figure."""
        text = re.sub(r"<[^>]+>", " ", str(raw))
        text = re.sub(r"\s+", " ", text)
        m = re.search(r"mine production[^0-9]{0,200}?([\d,]+(?:\.\d+)?)\s*(?:t|tons|tonnes)?",
                      text, re.IGNORECASE)
        if not m:
            raise ConnectorError(
                "supply: world mine production not found on the USGS gold "
                "statistics page — use manual import (parse_supply_csv)")
        return [self._snapshot("supply_usgs_world_mine_production_tonnes",
                               float(m.group(1).replace(",", "")), "tonnes",
                               source_url=USGS_PAGE_URL,
                               vintage="USGS Mineral Commodity Summaries basis; "
                                       "do not mix with WGC/Metals Focus totals")]

    def parse_supply_csv(self, path: str) -> list:
        """Manual import: quarterly supply/demand (or AISC) CSV.

        Columns (case-insensitive): date/quarter/month, category/component,
        value/tonnes, optional unit (tonnes default; 'USD/oz' or 'usd'
        gives an AISC-style $/oz series). Series: supply_<category>_tonnes
        or supply_<category>_usd_per_oz.
        """
        try:
            with open(path, newline="", encoding="utf-8-sig") as fh:
                rows = list(csv.DictReader(fh))
        except OSError as exc:
            raise ConnectorError(f"supply: cannot read {path}: {exc}")
        if not rows:
            raise ConnectorError(f"supply: {path} has no data rows")
        headers = {(k or "").strip().lower(): k for k in rows[0].keys() if k}
        date_key = next((headers[h] for h in headers
                         if h in ("date", "month", "quarter", "period")), None)
        cat_key = next((headers[h] for h in headers
                        if h in ("category", "component", "item", "series")), None)
        val_key = next((headers[h] for h in headers
                        if h in ("value", "tonnes", "tons", "amount")), None)
        unit_key = next((headers[h] for h in headers if h == "unit"), None)
        if val_key is None:
            raise ConnectorError(
                f"supply: {path} has no value/tonnes column "
                f"(headers: {sorted(headers)})")
        snaps = []
        for row in rows:
            raw_val = (row.get(val_key) or "").strip().replace(",", "")
            if raw_val in ("", "-", "n/a"):
                continue
            try:
                value = float(raw_val)
            except ValueError:
                continue
            unit = ((row.get(unit_key) or "").strip().lower()
                    if unit_key else "")
            is_usd = ("usd" in unit or "$" in unit or "oz" in unit)
            label = (row.get(cat_key) or "").strip() if cat_key else "total"
            series = ("supply_" + _slug(label) + "_usd_per_oz") if is_usd \
                else ("supply_" + _slug(label) + "_tonnes")
            snaps.append(self._snapshot(
                series, value, "USD/oz" if is_usd else "tonnes",
                as_of=parse_date_loose(row.get(date_key, "")) if date_key else None,
                source_url="manual import: user-downloaded supply/demand file"))
        if not snaps:
            raise ConnectorError(f"supply: no parseable rows in {path}")
        return snaps

    def fetch(self) -> list:
        try:
            return self.parse(self._get_text(USGS_PAGE_URL))
        except ConnectorError as exc:
            return self._fail(str(exc))
