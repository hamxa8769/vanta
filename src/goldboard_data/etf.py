"""Gold ETF holdings connector — SPDR GLD and iShares IAU (daily).

GLD: sponsor State Street publishes daily holdings (tonnes/ounces, NAV,
shares outstanding) in the Historical Archive XLSX served by the
sponsor's own API (found in the GLD product page's download link,
verified live 2026-11-21):
  https://api.spdrgoldshares.com/api/v1/historical-archive?product=gld&exchange=NYSE&lang=en
fetch_gld() downloads and parses that file with stdlib only (zipfile +
XML, no openpyxl); the file has a Disclaimer sheet first, then the
"US GLD Historical Archive" data sheet (Date / Tonnes of Gold /
Total Ounces of Gold in the Trust). Fallback: parse the GLD product
page (https://www.spdrgoldshares.com/hong-kong/9840/ — note the page
shows 'AWAITING' placeholders until its data loads, so the page route
often fails; that failure is reported, never faked). The old direct
file URL .../media/GLD/GLD_holdings.xlsx returned 404 on 2026-11-21
and is not used. parse_gld_xlsx() is also the manual-import route for
a user-downloaded archive file.
IAU: BlackRock publishes daily price/NAV/holdings on iShares.com
  (product page https://www.ishares.com/us/products/239561/ishares-gold-trust).
  No verbatim holdings-file URL was returned from a primary source, so
  IAU is fetched by parsing that product page and, failing that, by
  manual import (parse_iau_csv). No URL is guessed for IAU.

Daily change in tonnes is the ETF flow proxy. GLD custodian: HSBC +
JPMorgan; IAU custodian: JPMorgan Chase Bank London.
"""

from __future__ import annotations

import csv
import io
import re
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

from .base import BaseConnector, ConnectorError
from .wgc import parse_date_loose

GLD_PAGE_URL = "https://www.spdrgoldshares.com/hong-kong/9840/"
# Verified live 2026-11-21: the sponsor's own Historical Archive API —
# returns the full GLD holdings XLSX (200, 544 KB). Found in the GLD
# product page's own download link.
GLD_HOLDINGS_XLSX_URL = ("https://api.spdrgoldshares.com/api/v1/"
                         "historical-archive?product=gld&exchange=NYSE&lang=en")
# Old direct file URL — 404 on 2026-11-21; do not use.
GLD_HOLDINGS_XLSX_URL_OLD = "https://www.spdrgoldshares.com/media/GLD/GLD_holdings.xlsx"
IAU_PRODUCT_URL = "https://www.ishares.com/us/products/239561/ishares-gold-trust"


def _parse_sheet(xml_bytes: bytes, shared: list[str]) -> list[list[str]]:
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    root = ET.fromstring(xml_bytes)
    rows: list[list[str]] = []
    for r in root.findall(".//m:row", ns):
        cells: list[str] = []
        for c in r.findall("m:c", ns):
            t = c.get("t")
            if t == "inlineStr":
                t_el = c.find(".//m:t", ns)
                cells.append(t_el.text or "" if t_el is not None else "")
                continue
            v = c.find("m:v", ns)
            if v is None or v.text is None:
                cells.append("")
            elif t == "s":
                try:
                    cells.append(shared[int(v.text)])
                except (ValueError, IndexError):
                    cells.append("")
            else:
                cells.append(v.text)
        rows.append(cells)
    return rows


def _read_xlsx_rows(data: bytes) -> list[list[str]]:
    """Minimal stdlib XLSX reader.

    Returns the rows of the first sheet whose header contains both a
    Date column and a Tonnes column (the real GLD archive has a
    Disclaimer sheet first, then the data sheet)."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ConnectorError(f"etf: holdings file is not a valid XLSX: {exc}")
    shared: list[str] = []
    if "xl/sharedStrings.xml" in zf.namelist():
        root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
        ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
        for si in root.findall("m:si", ns):
            shared.append("".join(t.text or "" for t in si.iter(
                "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t")))
    sheet_files = sorted(
        n for n in zf.namelist()
        if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n))
    if not sheet_files:
        raise ConnectorError("etf: XLSX has no worksheets")
    for sf in sheet_files:
        try:
            rows = _parse_sheet(zf.read(sf), shared)
        except ET.ParseError:
            continue
        for row in rows:
            low = [c.strip().lower() for c in row]
            if any("date" in c for c in low) and any("tonne" in c for c in low):
                return rows
    raise ConnectorError(
        "etf: no worksheet with a Date/Tonnes header found in the XLSX")


def parse_gld_xlsx(data: bytes) -> list[dict]:
    """Extract dated holdings rows from the GLD XLSX.

    Returns [{date, tonnes, ounces}] sorted by date. Raises
    ConnectorError if the layout is unrecognised (fail loudly).
    """
    rows = _read_xlsx_rows(data)
    header_idx = None
    date_col = tonnes_col = ounces_col = None
    for i, row in enumerate(rows):
        low = [c.strip().lower() for c in row]
        if any("date" in c for c in low) and any("tonne" in c for c in low):
            header_idx = i
            date_col = next(j for j, c in enumerate(low) if "date" in c)
            tonnes_col = next(j for j, c in enumerate(low) if "tonne" in c)
            ounces_col = next((j for j, c in enumerate(low) if "ounce" in c), None)
            break
    if header_idx is None:
        raise ConnectorError(
            "etf: GLD holdings XLSX layout unrecognised "
            "(no Date/Tonnes header row) — sponsor file may have changed")
    out: list[dict] = []
    for row in rows[header_idx + 1:]:
        if date_col is None or tonnes_col is None or len(row) <= max(date_col, tonnes_col):
            continue
        d = parse_date_loose(row[date_col])
        try:
            tonnes = float(row[tonnes_col].replace(",", ""))
        except (ValueError, AttributeError):
            continue
        ounces = None
        if ounces_col is not None and len(row) > ounces_col:
            try:
                ounces = float(row[ounces_col].replace(",", ""))
            except (ValueError, AttributeError):
                ounces = None
        out.append({"date": d, "tonnes": tonnes, "ounces": ounces})
    if not out:
        raise ConnectorError("etf: GLD holdings XLSX has no data rows")
    out.sort(key=lambda r: r["date"] or datetime.min.replace(tzinfo=timezone.utc))
    return out


def _parse_holdings_csv(path: str, fund: str) -> list[dict]:
    try:
        with open(path, newline="", encoding="utf-8-sig") as fh:
            rows = list(csv.DictReader(fh))
    except OSError as exc:
        raise ConnectorError(f"etf: cannot read {path}: {exc}")
    if not rows:
        raise ConnectorError(f"etf: {path} has no data rows")
    headers = {(k or "").strip().lower(): k for k in rows[0].keys() if k}
    date_key = next((headers[h] for h in headers
                     if h in ("date", "month", "as of", "asof")), None)
    tonnes_key = next((headers[h] for h in headers if "tonne" in h), None)
    ounces_key = next((headers[h] for h in headers if "ounce" in h), None)
    if tonnes_key is None and ounces_key is None:
        raise ConnectorError(f"etf: {path} has no tonnes/ounces column")
    out = []
    for row in rows:
        raw = (row.get(tonnes_key) or "") if tonnes_key else ""
        tonnes = None
        if raw:
            try:
                tonnes = float(raw.replace(",", ""))
            except ValueError:
                tonnes = None
        if tonnes is None and ounces_key:
            try:
                tonnes = float((row.get(ounces_key) or "").replace(",", "")) * 0.0311034768
            except ValueError:
                tonnes = None
        if tonnes is None:
            continue
        out.append({"date": parse_date_loose(row.get(date_key, "")) if date_key else None,
                    "tonnes": tonnes, "ounces": None})
    if not out:
        raise ConnectorError(f"etf: no parseable rows in {path}")
    return out


class ETFConnector(BaseConnector):
    name = "etf"
    source_url = GLD_HOLDINGS_XLSX_URL
    refresh_minutes = 60 * 24  # daily, after US close

    def parse(self, raw) -> list:
        """Parse GLD holdings XLSX bytes, a holdings CSV string, or the
        sponsor's GLD product page HTML."""
        if isinstance(raw, (bytes, bytearray)):
            return self._snapshots_from_holdings("gld", parse_gld_xlsx(bytes(raw)))
        text = str(raw)
        if "Tonnes" in text and ("Trust Information" in text or "spdrgoldshares" in text.lower()):
            return self.parse_gld_page(text)
        return self._snapshots_from_holdings(
            "gld", _parse_holdings_csv_text(text, "gld"))

    def parse_gld_page(self, html: str) -> list:
        """Parse the sponsor GLD page ('Ounces, …, Tonnes, …, <date>')."""
        text = re.sub(r"<[^>]+>", " ", html)
        text = re.sub(r"\s+", " ", text)
        m = re.search(
            r"Tonnes,\s*([\d,]+\.?\d*),\s*([A-Za-z]+\s+\d{1,2},\s+\d{4})", text)
        if not m:
            raise ConnectorError(
                "etf: GLD tonnes not found on the sponsor page — "
                "page layout may have changed")
        tonnes = float(m.group(1).replace(",", ""))
        try:
            as_of = datetime.strptime(m.group(2).strip(), "%B %d, %Y").replace(
                tzinfo=timezone.utc)
        except ValueError:
            as_of = None
        snaps = [self._snapshot("etf_gld_tonnes", tonnes, "tonnes",
                                as_of=as_of, source_url=GLD_PAGE_URL)]
        om = re.search(r"Ounces,\s*([\d,]+\.?\d*)", text)
        if om:
            snaps.append(self._snapshot(
                "etf_gld_ounces", float(om.group(1).replace(",", "")),
                "troy oz", as_of=as_of, source_url=GLD_PAGE_URL))
        return snaps

    def _snapshots_from_holdings(self, fund: str, rows: list[dict]) -> list:
        latest = rows[-1]
        snaps = [self._snapshot(
            f"etf_{fund}_tonnes", latest["tonnes"], "tonnes",
            as_of=latest["date"],
            source_url=GLD_HOLDINGS_XLSX_URL if fund == "gld" else IAU_PRODUCT_URL)]
        if len(rows) >= 2:
            prev = rows[-2]
            snaps.append(self._snapshot(
                f"etf_{fund}_daily_change_tonnes",
                latest["tonnes"] - prev["tonnes"], "tonnes",
                as_of=latest["date"],
                source_url=GLD_HOLDINGS_XLSX_URL if fund == "gld" else IAU_PRODUCT_URL))
        return snaps

    # -- manual import -------------------------------------------------
    def parse_gld_csv(self, path: str) -> list:
        return self._snapshots_from_holdings("gld", _parse_holdings_csv(path, "gld"))

    def parse_iau_csv(self, path: str) -> list:
        return self._snapshots_from_holdings("iau", _parse_holdings_csv(path, "iau"))

    # -- live ----------------------------------------------------------
    def fetch_gld(self) -> list:
        """Live GLD holdings from the sponsor's Historical Archive XLSX.

        Falls back to parsing the sponsor product page (which shows
        'AWAITING' placeholders until the data loads, so it often fails
        — that failure is reported, never faked).
        """
        try:
            data = self._get(GLD_HOLDINGS_XLSX_URL).content
            return self._snapshots_from_holdings("gld", parse_gld_xlsx(data))
        except ConnectorError as exc:
            self.log.warning("etf: GLD archive XLSX failed: %s", exc)
            try:
                return self.parse_gld_page(self._get_text(GLD_PAGE_URL))
            except ConnectorError as exc2:
                raise ConnectorError(
                    f"etf: GLD holdings unavailable ({exc}; page: {exc2})")

    def fetch_gld_xlsx(self) -> list:
        """Manual-archive route: parse a downloaded GLD holdings XLSX."""
        data = self._get(GLD_HOLDINGS_XLSX_URL).content
        return self._snapshots_from_holdings("gld", parse_gld_xlsx(data))

    def fetch_iau(self) -> list:
        html = self._get_text(IAU_PRODUCT_URL)
        text = re.sub(r"<[^>]+>", " ", html)
        text = re.sub(r"\s+", " ", text)
        # Real figure reads "Tonnes in Trust 460.11 as of Nov 20, 2026";
        # a bare "… 2026 Tonnes" match would catch the year instead.
        m = re.search(r"Tonnes in Trust\s+([\d,]+\.?\d*)", text, re.IGNORECASE)
        if not m:
            raise ConnectorError(
                "etf: IAU 'Tonnes in Trust' not found on the iShares "
                "product page — page layout may have changed; use manual "
                "import (parse_iau_csv)")
        as_of = None
        dm = re.search(r"as of\s+([A-Za-z]+\s+\d{1,2},\s+\d{4})",
                       text[m.end():m.end() + 60], re.IGNORECASE)
        if dm:
            try:
                as_of = datetime.strptime(dm.group(1).strip(), "%b %d, %Y").replace(
                    tzinfo=timezone.utc)
            except ValueError:
                try:
                    as_of = datetime.strptime(dm.group(1).strip(), "%B %d, %Y").replace(
                        tzinfo=timezone.utc)
                except ValueError:
                    as_of = None
        return [self._snapshot("etf_iau_tonnes",
                               float(m.group(1).replace(",", "")), "tonnes",
                               as_of=as_of, source_url=IAU_PRODUCT_URL)]

    def fetch(self) -> list:
        snaps: list = []
        errors: list[str] = []
        for fn in (self.fetch_gld, self.fetch_iau):
            try:
                snaps += fn()
            except ConnectorError as exc:
                errors.append(str(exc))
                self.log.warning("etf: %s", exc)
        if not snaps:
            return self._fail("etf: " + "; ".join(errors))
        self.last_error = "; ".join(errors) if errors else None
        return snaps


def _parse_holdings_csv_text(text: str, fund: str) -> list[dict]:
    import tempfile, os
    with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False,
                                     encoding="utf-8") as fh:
        fh.write(text)
        path = fh.name
    try:
        return _parse_holdings_csv(path, fund)
    finally:
        os.unlink(path)
