"""World Gold Council / central-bank gold connector.

There is NO public WGC API. Live data therefore comes from two routes:
1. Manual import (the reliable route): ``parse_wgc_csv(path)`` reads a
   user-downloaded WGC Gold Demand Trends / Central Bank Gold Statistics
   file (CSV). Store EVERY vintage — WGC revises: in July 2026 Metals
   Focus revised Q1 2026 central-bank demand from 244t to 57t, the 187t
   difference reclassified as OTC/other demand. Never treat a first print
   as final.
2. Best-effort page scrape of the public WGC Gold Focus page
   (https://www.gold.org/goldhub/gold-focus) for a monthly net-buying
   figure; if the page layout yields nothing, fetch() logs it and still
   returns the IMF reference snapshot below — it never invents a number.

Reference constants (NOT flows — never score these as buying/selling):
- IMF holds 2,814.1t, unchanged since early 2011. The IMF may sell gold
  only with 85% Executive Board approval; it has NO authority to buy
  gold, lend, lease, swap or use gold as collateral, and does not trade
  it. Source: https://www.imf.org/en/about/factsheets/sheets/2022/gold-in-the-imf
- The World Bank holds/trades NO gold; it only publishes reserve data
  sourced from IMF IFS and research. It is deliberately NOT a source here.
"""

from __future__ import annotations

import csv
import re
from datetime import datetime, timezone

from .base import BaseConnector, ConnectorError

WGC_GOLD_FOCUS_URL = "https://www.gold.org/goldhub/gold-focus"
IMF_SOURCE_URL = "https://www.imf.org/en/about/factsheets/sheets/2022/gold-in-the-imf"

# Static reference holding, NOT a flow. Labelled as such everywhere it is used.
IMF_HOLDINGS_TONNES = 2814.1

_NET_RE = re.compile(
    r"net (?:purchases|buying)[^0-9]{0,120}?([\d,]+(?:\.\d+)?)\s*(?:tonnes|t)\b",
    re.IGNORECASE)


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.strip().lower()).strip("_")


def parse_date_loose(s: str) -> datetime | None:
    """Parse YYYY-MM-DD, YYYY-MM, YYYY-Qn, or 'Month YYYY'."""
    s = (s or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m", "%Y/%m/%d", "%B %Y", "%b %Y",
                "%d-%b-%Y", "%d %b %Y"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    m = re.match(r"(\d{4})-Q([1-4])$", s)
    if m:  # quarter end month
        return datetime(int(m.group(1)), int(m.group(2)) * 3, 1,
                        tzinfo=timezone.utc)
    return None


def parse_wgc_csv(path: str) -> list:
    """Parse a user-downloaded WGC CSV (central-bank stats or GDT).

    Recognised columns (case-insensitive): a date/month/quarter column,
    an optional country/category/component column, and a value column
    named tonnes / net purchases / net purchases (tonnes) / value.
    Returns one snapshot per row; the caller wraps it in a connector
    (see WGCConnector.parse_wgc_csv) — this function is importable
    standalone for the supply connector too.
    """
    return WGCConnector().parse_wgc_csv_file(path)


class WGCConnector(BaseConnector):
    name = "wgc"
    source_url = WGC_GOLD_FOCUS_URL
    refresh_minutes = 60 * 24 * 30  # monthly

    # -- manual import -------------------------------------------------
    def parse_wgc_csv_file(self, path: str) -> list:
        try:
            with open(path, newline="", encoding="utf-8-sig") as fh:
                rows = list(csv.DictReader(fh))
        except OSError as exc:
            raise ConnectorError(f"wgc: cannot read {path}: {exc}")
        if not rows:
            raise ConnectorError(f"wgc: {path} has no data rows")
        headers = { (k or "").strip().lower(): k for k in rows[0].keys() if k }
        date_key = next((headers[h] for h in headers
                         if h in ("date", "month", "quarter", "period")), None)
        country_key = next((headers[h] for h in headers
                            if h in ("country", "category", "component",
                                     "central bank", "bank")), None)
        value_key = next((headers[h] for h in headers
                          if "tonne" in h or "net purchase" in h
                          or h in ("value", "demand", "supply")), None)
        if value_key is None:
            raise ConnectorError(
                f"wgc: {path} has no tonnes/value column "
                f"(headers: {sorted(headers)})")
        snaps = []
        for row in rows:
            raw_val = (row.get(value_key) or "").strip().replace(",", "")
            if raw_val in ("", "-", "n/a"):
                continue
            try:
                value = float(raw_val)
            except ValueError:
                continue
            as_of = parse_date_loose(row.get(date_key, "")) if date_key else None
            label = (row.get(country_key) or "").strip() if country_key else ""
            series = ("wgc_cb_net_tonnes_" + _slug(label)) if label \
                else "wgc_cb_net_tonnes"
            snaps.append(self._snapshot(
                series, value, "tonnes", as_of=as_of,
                vintage=(row.get(date_key) or "").strip() or None,
                source_url="manual import: user-downloaded WGC file"))
        if not snaps:
            raise ConnectorError(f"wgc: no parseable rows in {path}")
        return snaps

    # -- live (best-effort scrape + IMF reference) --------------------
    def parse(self, raw: str) -> list:
        """Parse Gold Focus page HTML/text for a monthly net-buying figure.

        The figure comes from the latest Gold Focus blog post (e.g.
        "reported net buying totalling 39t" for August 2026); the month
        is taken from the article context when it can be found, and the
        vintage says which article it came from.
        """
        text = re.sub(r"<[^>]+>", " ", str(raw))
        text = re.sub(r"\s+", " ", text)
        m = _NET_RE.search(text)
        if not m:
            raise ConnectorError(
                "wgc: no net-purchases figure found on Gold Focus page — "
                "use manual import (parse_wgc_csv)")
        as_of = None
        months = ("January", "February", "March", "April", "May", "June",
                  "July", "August", "September", "October", "November",
                  "December")
        before = text[max(0, m.start() - 500):m.start()]
        month = next((mo for mo in months if mo in before), None)
        if month:
            year_m = re.search(r"(20\d\d)", before)
            year = int(year_m.group(1)) if year_m else datetime.now(timezone.utc).year
            try:
                as_of = datetime.strptime(f"{month} {year}", "%B %Y").replace(
                    tzinfo=timezone.utc)
            except ValueError:
                as_of = None
        return [self._snapshot("wgc_cb_net_tonnes", float(m.group(1).replace(",", "")),
                               "tonnes", as_of=as_of,
                               source_url=WGC_GOLD_FOCUS_URL,
                               vintage="WGC Gold Focus blog, latest article "
                                       "(monthly reported net buying)")]

    def fetch(self) -> list:
        snaps = [self._snapshot(
            "imf_holdings_tonnes", IMF_HOLDINGS_TONNES, "tonnes",
            vintage="IMF factsheet 2022; holdings unchanged since early 2011",
            source_url=IMF_SOURCE_URL)]
        try:
            snaps += self.parse(self._get_text(WGC_GOLD_FOCUS_URL))
        except ConnectorError as exc:
            # WGC has no API; the manual-import path is the documented route.
            self.log.info("wgc: live scrape unavailable (%s); "
                          "use parse_wgc_csv", exc)
            self.last_error = str(exc)
        return snaps
