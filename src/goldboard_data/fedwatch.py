"""CME FedWatch connector + FOMC calendar.

FedWatch is market pricing of the Fed (from 30-Day Fed Funds futures),
NOT Fed guidance. The free web tool is not an API and is never scraped
or faked. The supported route is the CME FedWatch End-of-Day API
($25/month, REST/JSON, history back to 2015):
  https://www.cmegroup.com/market-data/market-data-api/fedwatch-api.html
It needs a key (env GOLD_BOARD_FEDWATCH_KEY, or the Settings page which
saves it to the config file) and the endpoint URL (env
GOLD_BOARD_FEDWATCH_URL, or the Settings page — the exact endpoint path
is set from the CME API page; no endpoint is guessed here). Without a
key, fetch() raises ConnectorError: "FedWatch API key required
($25/mo EOD API)".

FOMC calendar: NOTHING is hard-coded as live. ``next_meetings()`` reads
``data/fomc_calendar.json`` — a small, manually-maintained calendar file
whose snapshot date is recorded inside it. Seeded 2026-11-21: next
meeting 8–9 Dec 2026 (with SEP/dot plot); target range 3.75–4.00%
after the 28 Oct 2026 hold.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from .base import BaseConnector, ConnectorError

FEDWATCH_API_PAGE = "https://www.cmegroup.com/market-data/market-data-api/fedwatch-api.html"
KEY_REQUIRED_MSG = ("FedWatch API key required ($25/mo EOD API) — set "
                    "GOLD_BOARD_FEDWATCH_KEY; see " + FEDWATCH_API_PAGE)


def _calendar_paths() -> list[Path]:
    here = Path(__file__).resolve()
    paths: list[Path] = []
    # Frozen app (PyInstaller): the calendar is bundled as data/ next
    # to the exe (vanta.spec datas); _MEIPASS is the bundle dir.
    # Without this, the frozen app never finds the file (v0.1.1 bug:
    # "FOMC calendar unavailable: data/fomc_calendar.json not found").
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        # PyInstaller 6 onedir: datas live under _internal/ and
        # _MEIPASS is the _internal dir itself; older/onefile layouts
        # put them directly under _MEIPASS — try both, plus the
        # bundle-root _internal variant, before the dev paths.
        paths.append(Path(meipass) / "data" / "fomc_calendar.json")
        paths.append(Path(meipass) / "_internal" / "data" / "fomc_calendar.json")
    return paths + [
        here.parents[2] / "data" / "fomc_calendar.json",   # gold-board/data/
        Path.cwd() / "data" / "fomc_calendar.json",
        here.parents[1] / "data" / "fomc_calendar.json",
    ]


def load_fomc_calendar() -> dict:
    """Load the manually-maintained FOMC calendar JSON."""
    for path in _calendar_paths():
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise ConnectorError(f"fedwatch: bad fomc_calendar.json "
                                     f"at {path}: {exc}")
    raise ConnectorError("fedwatch: data/fomc_calendar.json not found")


def next_meetings() -> list[dict]:
    """Upcoming FOMC meetings from the manual calendar (not live data)."""
    cal = load_fomc_calendar()
    today = datetime.now(timezone.utc).date()
    out = []
    for m in cal.get("meetings", []):
        try:
            start = datetime.strptime(m["start"], "%Y-%m-%d").date()
        except (KeyError, ValueError):
            continue
        if start >= today:
            out.append(m)
    return out


class FedWatchConnector(BaseConnector):
    name = "fedwatch"
    source_url = FEDWATCH_API_PAGE
    refresh_minutes = 60 * 24  # end-of-day API

    def __init__(self, api_key: str | None = None,
                 endpoint: str | None = None, **kw) -> None:
        super().__init__(**kw)
        # Env first, then the config file (Settings page), then nothing.
        self.api_key = api_key or os.environ.get("GOLD_BOARD_FEDWATCH_KEY")
        self.endpoint = endpoint or os.environ.get("GOLD_BOARD_FEDWATCH_URL")
        if not self.api_key or not self.endpoint:
            try:
                from goldboard_core.config import load_config
                cfg = load_config()
                if not self.api_key:
                    self.api_key = cfg.fedwatch_api_key or None
                if not self.endpoint:
                    self.endpoint = cfg.fedwatch_api_url or None
            except Exception:
                pass

    def parse(self, raw) -> list:
        """Parse FedWatch EOD JSON into per-meeting probability snapshots.

        Expected shape: a dict with a 'probabilities' (or 'meetings')
        list of {meeting_date, outcome, probability}. Any other shape
        fails loudly — probabilities are never guessed.
        """
        if not isinstance(raw, dict):
            raise ConnectorError("fedwatch: EOD API did not return a JSON object")
        rows = raw.get("probabilities") or raw.get("meetings") or raw.get("data")
        if not isinstance(rows, list) or not rows:
            raise ConnectorError(
                "fedwatch: EOD API JSON has no probabilities/meetings list — "
                "unexpected shape; refusing to guess")
        snaps = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            meeting = str(row.get("meeting_date") or row.get("meeting") or "")
            outcome = str(row.get("outcome") or row.get("scenario") or "")
            prob = row.get("probability", row.get("prob"))
            if not meeting or prob is None:
                continue
            try:
                p = float(prob)
            except (TypeError, ValueError):
                continue
            as_of = None
            try:
                as_of = datetime.strptime(meeting[:10], "%Y-%m-%d").replace(
                    tzinfo=timezone.utc)
            except ValueError:
                pass
            label = (meeting[:10] + "_" + outcome).lower()
            label = "".join(c if c.isalnum() or c == "_" else "_" for c in label)
            snaps.append(self._snapshot(
                f"fedwatch_prob_{label}", p, "pct", as_of=as_of,
                source_url=self.endpoint or FEDWATCH_API_PAGE,
                vintage="CME FedWatch EOD API — market pricing, not Fed guidance"))
        if not snaps:
            raise ConnectorError(
                "fedwatch: no usable probabilities in EOD API response")
        return snaps

    def fetch(self) -> list:
        if not self.api_key:
            raise ConnectorError(KEY_REQUIRED_MSG)
        if not self.endpoint:
            raise ConnectorError(
                "fedwatch: EOD API endpoint not configured — set "
                "GOLD_BOARD_FEDWATCH_URL to the endpoint from "
                + FEDWATCH_API_PAGE)
        data = self._get_json(self.endpoint, params={"api_key": self.api_key},
                              )
        return self.parse(data)
