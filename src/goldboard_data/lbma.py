"""LBMA connector — market makers, vault holdings, clearing totals.

Sources (LBMA, free web, no API):
- Market makers: https://www.lbma.org.uk/membership/market-making-membership
  The live list is fetched and parsed — the count is NEVER hard-coded:
  research found a discrepancy (LBMA page listed 11 banks / 6 full
  market makers on 21 Nov 2026; secondary sources listed 12 / 7
  including Deutsche Bank, reclassified 20 Mar 2026). Only aggregates
  are published; NO per-bank positions exist — London is OTC, bilateral,
  principal-to-principal, with no central order book.
- Vault holdings (monthly, ~4-week lag):
  https://www.lbma.org.uk/prices-and-data/london-vault-data
  (7 custodians incl. Bank of England; no owner/custodian breakdown)
- Clearing totals (monthly):
  https://www.lbma.org.uk/prices-and-data/clearing-data
  (net settlement among clearing banks, not total OTC turnover)
"""

from __future__ import annotations

import html as html_lib
import re

from .base import BaseConnector, ConnectorError

MEMBERSHIP_URL = "https://www.lbma.org.uk/membership/market-making-membership"
VAULT_URL = "https://www.lbma.org.uk/prices-and-data/london-vault-data"
CLEARING_URL = "https://www.lbma.org.uk/prices-and-data/clearing-data"

_NAV_JUNK = ("cookie", "privacy", "menu", "search", "home", "contact",
             "login", "sign in", "subscribe", "sitemap", "terms")


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.strip().lower()).strip("_")


def parse_market_makers(html: str) -> list[str]:
    """Extract the market-maker names from the LBMA membership page.

    Takes list/table items in the members section (from the 'Market
    Maker' heading to the next heading); filters quoting minimums
    (contain digits/':'), navigation and other junk. Raises
    ConnectorError if the section cannot be found (page layout
    changed) — never guesses. The count is never hard-coded.
    """
    m = re.search(r"market maker", html, re.IGNORECASE)
    if not m:
        raise ConnectorError(
            "lbma: 'Market Maker' section not found on membership page — "
            "page layout may have changed")
    section = html[m.end():m.end() + 20000]
    nxt = re.search(r"<h[123][^>]*>", section, re.IGNORECASE)
    if nxt:
        section = section[:nxt.start()]
    items: list[str] = []
    for raw in re.findall(r"<(?:li|td)[^>]*>(.*?)</(?:li|td)>",
                          section, re.IGNORECASE | re.DOTALL):
        name = html_lib.unescape(re.sub(r"<[^>]+>", "", raw))
        name = re.sub(r"\s+", " ", name).strip(" -\u2022")
        if len(name) < 4 or len(name) > 80:
            continue
        if re.search(r"\d", name) or ":" in name:
            continue  # quoting minimums ("Spot : 5,000 oz") etc.
        low = name.lower()
        if any(j in low for j in _NAV_JUNK):
            continue
        if not re.search(r"[A-Za-z]{3,}", name):
            continue
        items.append(name)
    # de-duplicate, keep order
    seen: list[str] = []
    for n in items:
        if n not in seen:
            seen.append(n)
    if not seen:
        raise ConnectorError(
            "lbma: no market-maker names parsed from membership page — "
            "page layout may have changed")
    return seen


def _first_number_near(text: str, keyword: str, window: int = 300) -> float | None:
    m = re.search(re.escape(keyword), text, re.IGNORECASE)
    if not m:
        return None
    seg = text[m.end():m.end() + window]
    nm = re.search(r"([\d,]+(?:\.\d+)?)", seg)
    if not nm:
        return None
    try:
        return float(nm.group(1).replace(",", ""))
    except ValueError:
        return None


def _number_before(text: str, keyword: str, window: int = 300) -> float | None:
    """Number immediately BEFORE a keyword ('9,850 tonnes', '7,438 transfers')."""
    m = re.search(re.escape(keyword), text, re.IGNORECASE)
    if not m:
        return None
    seg = text[max(0, m.start() - window):m.start()]
    nums = re.findall(r"([\d,]+(?:\.\d+)?)", seg)
    if not nums:
        return None
    try:
        return float(nums[-1].replace(",", ""))
    except ValueError:
        return None


class LBMAConnector(BaseConnector):
    name = "lbma"
    source_url = MEMBERSHIP_URL
    refresh_minutes = 60 * 24 * 30  # monthly for vault/clearing

    def parse(self, raw: str) -> list:
        """Parse the membership page HTML into per-bank + count snapshots."""
        banks = parse_market_makers(str(raw))
        snaps = [self._snapshot(f"lbma_market_maker_{_slug(b)}", 1.0, "member",
                                source_url=MEMBERSHIP_URL) for b in banks]
        snaps.append(self._snapshot("lbma_market_maker_count", float(len(banks)),
                                    "banks", source_url=MEMBERSHIP_URL,
                                    vintage="live LBMA membership page; "
                                            "count is fetched, never hard-coded"))
        return snaps

    def fetch_membership(self) -> list:
        return self.parse(self._get_text(MEMBERSHIP_URL))

    def fetch_vault(self) -> list:
        html = self._get_text(VAULT_URL)
        text = re.sub(r"<[^>]+>", " ", html)
        text = re.sub(r"\s+", " ", text)
        tonnes = _number_before(text, "tonnes")
        if tonnes is None:
            ounces = _number_before(text, "ounces")
            if ounces is None:
                raise ConnectorError(
                    "lbma: vault gold total not found on LBMA vault page — "
                    "page layout may have changed")
            return [self._snapshot("lbma_vault_gold_oz", ounces, "troy oz",
                                   source_url=VAULT_URL)]
        return [self._snapshot("lbma_vault_gold_tonnes", tonnes, "tonnes",
                               source_url=VAULT_URL)]

    def fetch_clearing(self) -> list:
        """Parse the 'Most Recent Figures' prose on the clearing page.

        Gold section reads e.g. "Most Recent Figures September 2026
        Gold The volume of gold transferred in September ... to 18.3mn
        ounces ... There were 7,438 transfers ...". Values are monthly
        net settlement among clearing banks, not total OTC turnover.
        """
        from datetime import datetime, timezone
        html = self._get_text(CLEARING_URL)
        text = re.sub(r"<[^>]+>", " ", html)
        text = re.sub(r"\s+", " ", text)
        m = re.search(
            r"Most Recent Figures\s+([A-Za-z]+)\s+(\d{4})\s+Gold\s+"
            r"The volume of gold transferred.*?to\s+([\d.]+)mn ounces",
            text, re.IGNORECASE | re.DOTALL)
        if not m:
            raise ConnectorError(
                "lbma: clearing figures not found on LBMA clearing page — "
                "page layout may have changed")
        try:
            as_of = datetime.strptime(f"{m.group(1)} {m.group(2)}",
                                      "%B %Y").replace(tzinfo=timezone.utc)
        except ValueError:
            as_of = None
        ounces = float(m.group(3)) * 1_000_000
        snaps = [self._snapshot("lbma_clearing_gold_oz", ounces,
                                "troy oz", as_of=as_of,
                                source_url=CLEARING_URL)]
        tm = re.search(r"There were\s+([\d,]+)\s+transfers",
                       text[m.end():m.end() + 500], re.IGNORECASE)
        if tm:
            snaps.append(self._snapshot(
                "lbma_clearing_transfers",
                float(tm.group(1).replace(",", "")), "transfers",
                as_of=as_of, source_url=CLEARING_URL))
        return snaps

    def fetch(self) -> list:
        snaps: list = []
        errors: list[str] = []
        for fn in (self.fetch_membership, self.fetch_vault, self.fetch_clearing):
            try:
                snaps += fn()
            except ConnectorError as exc:
                errors.append(str(exc))
                self.log.warning("lbma: %s", exc)
        if not snaps:
            return self._fail("lbma: " + "; ".join(errors))
        self.last_error = "; ".join(errors) if errors else None
        return snaps
