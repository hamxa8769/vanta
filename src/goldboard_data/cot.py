"""CFTC Commitments of Traders (COT) connector — COMEX gold, code 088691.

Official sources (CFTC, free, weekly). Positions are as of Tuesday,
released usually Friday 15:30 ET (3-day lag). Verified live 2026-11-21:
- Legacy futures-only flat file (CSV):
  https://www.cftc.gov/dea/newcot/deafut.txt
- Disaggregated futures-only flat file (CSV):
  https://www.cftc.gov/dea/newcot/f_disagg.txt
Both files are comma-delimited, one row per market:
  "MARKET NAME",YYMMDD,YYYY-MM-DD,CODE,EXCH,...,OI, positions..., ...
Legacy positions after OI: non-commercial long, short, spreading,
commercial long, short, total long, short, non-reportable long, short.
Disaggregated positions after OI: producer long, short, swap-dealer
long, short, spreading, managed-money long, short, spreading,
other-reportable long, short, spreading, total long, short,
non-reportable long, short.

NOTE (2026-11-21): the older names FinFutWk.txt, deacot.txt and
c_disaggrt.txt are dead — FinFutWk.txt returns 200 but contains no
gold row, deacot.txt / c_disaggrt.txt return 404. The URLs above are
the live ones. If the CFTC format changes so the gold row is missing
or has too few numeric fields, this connector fails loudly
(ConnectorError / empty list), never silently mis-parses.
Managed money = the closest public proxy for hedge funds/CTAs as a
group; COT never names a fund and never gives individual SL/TP levels.
"""

from __future__ import annotations

import csv
import io
import re
from datetime import datetime, timezone

from .base import BaseConnector, ConnectorError, utcnow

GOLD_CODE = "088691"
FINFUT_URL = "https://www.cftc.gov/dea/newcot/deafut.txt"
DEACOT_URL = "https://www.cftc.gov/dea/newcot/f_disagg.txt"

_DATE_PATTERNS = [
    re.compile(r"(\d{4})-(\d{2})-(\d{2})"),             # 2026-11-17
    re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})"),          # 11/17/2026
    re.compile(r"([A-Za-z]+)\s+(\d{1,2}),\s+(\d{4})"),   # November 17, 2026
]


def _to_float(tok: str) -> float | None:
    tok = (tok or "").strip().replace(",", "")
    if tok in ("", ".", "-", "n/a"):
        return None
    try:
        return float(tok)
    except ValueError:
        return None


def _find_gold_row(text: str) -> tuple[datetime | None, list[float | None]]:
    """Locate the gold row; return (as-of date, numeric fields after OI).

    Handles the live CSV flat files and, as a fallback, the old
    space-separated text layout.
    """
    # -- CSV flat files (live format) --
    try:
        rows = list(csv.reader(io.StringIO(text)))
    except csv.Error:
        rows = []
    for row in rows:
        if len(row) >= 8 and row[3].strip() == GOLD_CODE:
            as_of = None
            m = re.match(r"(\d{4})-(\d{2})-(\d{2})", (row[2] or "").strip())
            if m:
                as_of = datetime(int(m.group(1)), int(m.group(2)),
                                 int(m.group(3)), tzinfo=timezone.utc)
            return as_of, [_to_float(v) for v in row[7:]]
    # -- old space-separated text layout (fallback) --
    for line in text.splitlines():
        tokens = line.split()
        if GOLD_CODE not in tokens:
            continue
        nums = [_to_float(t) for t in tokens[tokens.index(GOLD_CODE) + 1:]]
        return _report_date(text), nums
    raise ConnectorError(
        "cot: gold code 088691 not found in CFTC file — "
        "CFTC format may have changed; refusing to guess")


def _report_date(text: str) -> datetime | None:
    """Report (as-of) date from a header line, if one is printed."""
    head = "\n".join(text.splitlines()[:15])
    for pat in _DATE_PATTERNS:
        m = pat.search(head)
        if not m:
            continue
        try:
            if pat is _DATE_PATTERNS[2]:
                return datetime.strptime(m.group(0), "%B %d, %Y").replace(
                    tzinfo=timezone.utc)
            if pat is _DATE_PATTERNS[0]:
                y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            else:
                mo, d, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
            return datetime(y, mo, d, tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _pct(part: float, whole: float) -> float:
    return (part / whole * 100.0) if whole else 0.0


def _require(nums: list, n: int, fname: str) -> list[float]:
    vals = nums[:n]
    if len(vals) < n or any(v is None for v in vals):
        raise ConnectorError(
            f"cot: {fname} gold row has too few usable numeric fields "
            f"(got {len([v for v in vals if v is not None])} of {n}) — "
            "CFTC format may have changed; refusing to guess")
    return [float(v) for v in vals]  # type: ignore[arg-type]


class COTConnector(BaseConnector):
    name = "cot"
    source_url = FINFUT_URL
    refresh_minutes = 60 * 24 * 7  # weekly report

    # -- parsing ------------------------------------------------------
    def parse(self, raw: str) -> list:
        """Parse the legacy futures-only file (deafut.txt)."""
        return self.parse_legacy(raw)

    def _status(self, as_of) -> str:
        if as_of is None or (utcnow() - as_of).days > 10:
            return "stale"  # no date printed, or report is over a week old
        return "ok"

    def parse_legacy(self, text: str) -> list:
        as_of, nums = _find_gold_row(text)
        (oi, nc_long, nc_short, nc_spread, c_long, c_short,
         _tot_long, _tot_short, _nr_long, _nr_short) = _require(nums, 10, "deafut.txt")
        nc_net = nc_long - nc_short
        c_net = c_long - c_short
        status = self._status(as_of)
        return [
            self._snapshot("cot_gold_oi", oi, "contracts", as_of, status=status),
            self._snapshot("cot_gold_nc_long", nc_long, "contracts", as_of, status=status),
            self._snapshot("cot_gold_nc_short", nc_short, "contracts", as_of, status=status),
            self._snapshot("cot_gold_nc_net", nc_net, "contracts", as_of, status=status),
            self._snapshot("cot_gold_nc_net_pct_oi", _pct(nc_net, oi), "pct_of_oi", as_of, status=status),
            self._snapshot("cot_gold_commercial_net", c_net, "contracts", as_of, status=status),
            self._snapshot("cot_gold_nc_spreading", nc_spread, "contracts", as_of, status=status),
        ]

    def parse_disaggregated(self, text: str) -> list:
        """Parse the disaggregated file (f_disagg.txt): managed money etc."""
        as_of, nums = _find_gold_row(text)
        (oi, prod_long, prod_short, swap_long, swap_short, _swap_spread,
         mm_long, mm_short, _mm_spread, _oth_long, _oth_short, _oth_spread,
         _tot_long, _tot_short, _nr_long, _nr_short) = _require(nums, 16, "f_disagg.txt")
        mm_net = mm_long - mm_short
        swap_net = swap_long - swap_short
        status = self._status(as_of)
        return [
            self._snapshot("cot_gold_mm_long", mm_long, "contracts", as_of, status=status),
            self._snapshot("cot_gold_mm_short", mm_short, "contracts", as_of, status=status),
            self._snapshot("cot_gold_mm_net", mm_net, "contracts", as_of, status=status),
            self._snapshot("cot_gold_mm_net_pct_oi", _pct(mm_net, oi), "pct_of_oi", as_of, status=status),
            self._snapshot("cot_gold_swap_net", swap_net, "contracts", as_of, status=status),
            self._snapshot("cot_gold_producer_net", prod_long - prod_short, "contracts", as_of, status=status),
        ]

    # -- fetching -----------------------------------------------------
    def fetch(self) -> list:
        snaps: list = []
        try:
            snaps += self.parse_legacy(self._get_text(FINFUT_URL))
        except ConnectorError as exc:
            return self._fail(str(exc))
        try:
            snaps += self.parse_disaggregated(self._get_text(DEACOT_URL))
        except ConnectorError as exc:
            # Disaggregated file is a supplement; legacy data still stands.
            self.log.warning("cot: disaggregated file unusable: %s", exc)
            self.last_error = str(exc)
        return snaps
