"""COMEX warehouse stocks connector.

Origin: CME Group daily warehouse stock reports (cmegroup.com).
Access point: MetalCharts republishes the CME report daily at
  https://metalcharts.org/comex/inventory
(the research pass could not verify a stable verbatim CME file URL, so
the aggregator is the access point and CME is cited as the origin —
labelled clearly on every snapshot).

Live layout (verified 2026-11-21): the page shows gold registered
stocks in a "Registered COMEX stocks by metal" table (e.g. "14.2M oz"
with the report date) and the gold total in prose (e.g. "22.86M oz of
gold, combining registered and eligible stocks"). Values are rounded
by the source to 0.01M oz. Eligible is DERIVED as total − registered
and labelled as such. Manual import: ``parse_comex_csv(path)`` with
date/registered/eligible/total columns (oz, exact).

Registered = warranted / deliverable; eligible = meets standards but
not warranted. A falling registered share with rising open interest
is a tightness flag, not a price forecast.
"""

from __future__ import annotations

import csv
import re
from datetime import datetime, timezone

from .base import BaseConnector, ConnectorError
from .wgc import parse_date_loose

METALCHARTS_URL = "https://metalcharts.org/comex/inventory"
CME_ORIGIN_NOTE = "CME Group daily warehouse stock reports (cmegroup.com)"


class COMEXConnector(BaseConnector):
    name = "comex"
    source_url = METALCHARTS_URL
    refresh_minutes = 60 * 24  # daily

    def parse(self, raw: str) -> list:
        """Parse the MetalCharts COMEX inventory page (HTML).

        Gold registered comes from the per-metal summary table
        (millions of oz, 2dp, with report date); gold total comes from
        the page prose; eligible is derived as total - registered.
        """
        html = re.sub(r"<!--.*?-->", "", str(raw), flags=re.S)
        m = re.search(
            r"Gold\s*<span[^>]*>\(\s*(\d{4}-\d{2}-\d{2})\s*\)</span>\s*</th>\s*"
            r"<td[^>]*>\s*([\d.]+)M oz</td>",
            html, re.IGNORECASE)
        if not m:
            raise ConnectorError(
                "comex: gold registered stocks not found on "
                "metalcharts.org/comex/inventory — page layout may have "
                "changed; CME remains the cited origin")
        as_of = None
        try:
            as_of = datetime.strptime(m.group(1), "%Y-%m-%d").replace(
                tzinfo=timezone.utc)
        except ValueError:
            pass
        registered = float(m.group(2)) * 1_000_000
        tm = re.search(r"([\d.]+)M oz</strong>\s*of gold", html, re.IGNORECASE)
        if not tm:
            raise ConnectorError(
                "comex: gold total stocks not found on "
                "metalcharts.org/comex/inventory — page layout may have "
                "changed; CME remains the cited origin")
        total = float(tm.group(1)) * 1_000_000
        eligible = total - registered
        return self._build(registered, eligible, total, as_of,
                           derived_eligible=True)

    def _build(self, registered: float, eligible: float, total: float,
               as_of, derived_eligible: bool = False) -> list:
        share = (registered / total * 100.0) if total else 0.0
        note = CME_ORIGIN_NOTE
        if derived_eligible:
            note += "; eligible derived as total - registered; source " \
                    "values rounded to 0.01M oz"
        return [
            self._snapshot("comex_registered_oz", registered, "troy oz", as_of,
                           vintage=note),
            self._snapshot("comex_eligible_oz", eligible, "troy oz", as_of,
                           vintage=note,
                           is_proxy=derived_eligible,
                           proxy_formula="eligible = total - registered" if derived_eligible else None),
            self._snapshot("comex_total_oz", total, "troy oz", as_of,
                           vintage=note),
            self._snapshot("comex_registered_share_pct", share, "pct", as_of,
                           vintage=note),
        ]

    def parse_comex_csv(self, path: str) -> list:
        """Manual import: CSV with date, registered, eligible, total (oz)."""
        try:
            with open(path, newline="", encoding="utf-8-sig") as fh:
                rows = list(csv.DictReader(fh))
        except OSError as exc:
            raise ConnectorError(f"comex: cannot read {path}: {exc}")
        if not rows:
            raise ConnectorError(f"comex: {path} has no data rows")
        headers = {(k or "").strip().lower(): k for k in rows[0].keys() if k}
        def col(*names):
            return next((headers[h] for h in headers if h in names), None)
        date_key = col("date", "month")
        reg_key = col("registered", "registered_oz", "registered (oz)")
        eli_key = col("eligible", "eligible_oz", "eligible (oz)")
        tot_key = col("total", "total_oz", "total (oz)")
        if reg_key is None or eli_key is None:
            raise ConnectorError(
                f"comex: {path} needs registered/eligible columns "
                f"(headers: {sorted(headers)})")
        last = rows[-1]
        try:
            registered = float((last.get(reg_key) or "").replace(",", ""))
            eligible = float((last.get(eli_key) or "").replace(",", ""))
        except ValueError as exc:
            raise ConnectorError(f"comex: bad numbers in {path}: {exc}")
        total = None
        if tot_key:
            try:
                total = float((last.get(tot_key) or "").replace(",", ""))
            except ValueError:
                total = None
        total = total if total is not None else registered + eligible
        as_of = parse_date_loose(last.get(date_key, "")) if date_key else None
        return self._build(registered, eligible, total, as_of)

    def fetch(self) -> list:
        try:
            return self.parse(self._get_text(METALCHARTS_URL))
        except ConnectorError as exc:
            return self._fail(str(exc))
