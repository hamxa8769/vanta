"""GDELT 2.0 Doc API connector — free news volume + tone, no key.

Endpoint: https://api.gdeltproject.org/api/v2/doc/doc
Modes used: timelinevol (daily article volume) and timelinetone
(average tone, -10 to +10), plus ArtList for real headlines (title,
URL, source domain, time — no full text). Query: gold.
Rules: ~15 min lag (labelled), polite throttle 1 request / 5s per IP
(enforced in-process), research-grade and noisy — no full text, and
tone/volume are slow regime variables, never intraday triggers.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

from .base import BaseConnector, ConnectorError

GDELT_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
THROTTLE_SECONDS = 5.0
QUERY = "gold"


class GDELTConnector(BaseConnector):
    name = "gdelt"
    source_url = GDELT_URL
    refresh_minutes = 60  # ~15 min lag; hourly polling is plenty
    _last_call: float = 0.0  # class-level: throttle across instances

    def _throttle(self) -> None:
        wait = THROTTLE_SECONDS - (time.monotonic() - type(self)._last_call)
        if wait > 0:
            time.sleep(wait)
        type(self)._last_call = time.monotonic()

    def _query(self, mode: str, timespan: str = "7d") -> dict:
        self._throttle()
        return self._get_json(GDELT_URL, params={
            "query": QUERY, "mode": mode, "format": "json",
            "timespan": timespan})

    def parse(self, raw) -> list:
        """Parse one GDELT timeline response (vol or tone) into snapshots."""
        if not isinstance(raw, dict):
            raise ConnectorError("gdelt: Doc API did not return a JSON object")
        timelines = raw.get("timeline") or []
        if not timelines:
            raise ConnectorError("gdelt: Doc API JSON has no 'timeline'")
        series = timelines[0].get("data") or []
        if not series:
            raise ConnectorError("gdelt: timeline has no data points")
        # Mode is encoded by the caller via _mode_hint; default to volume.
        mode = getattr(self, "_mode_hint", "timelinevol")
        values = []
        for pt in series:
            try:
                values.append(float(pt.get("value")))
            except (TypeError, ValueError):
                continue
        if not values:
            raise ConnectorError("gdelt: timeline has no numeric values")
        if mode == "timelinetone":
            return [
                self._snapshot("gdelt_gold_tone_latest", values[-1],
                               "tone (-10..10)",
                               vintage="GDELT 2.0 Doc API, ~15 min lag"),
                self._snapshot("gdelt_gold_tone_7d_avg",
                               sum(values) / len(values), "tone (-10..10)",
                               vintage="GDELT 2.0 Doc API, ~15 min lag"),
            ]
        return [
            self._snapshot("gdelt_gold_mentions_latest", values[-1],
                           "articles/day",
                           vintage="GDELT 2.0 Doc API, ~15 min lag"),
            self._snapshot("gdelt_gold_mentions_7d_avg",
                           sum(values) / len(values), "articles/day",
                           vintage="GDELT 2.0 Doc API, ~15 min lag"),
        ]

    def fetch(self) -> list:
        snaps: list = []
        errors: list[str] = []
        for mode in ("timelinevol", "timelinetone"):
            try:
                self._mode_hint = mode
                snaps += self.parse(self._query(mode))
            except ConnectorError as exc:
                errors.append(str(exc))
                self.log.warning("gdelt: %s failed: %s", mode, exc)
        if not snaps:
            return self._fail("gdelt: " + "; ".join(errors))
        self.last_error = "; ".join(errors) if errors else None
        return snaps

    # -- headlines ------------------------------------------------------
    def parse_headlines(self, raw) -> list[dict]:
        """Parse a GDELT ArtList response into headline dicts.

        Returns [{title, url, source, published_at, provider}] newest
        first. Articles without a title are skipped; nothing is
        invented — an empty/malformed response raises ConnectorError.
        """
        if not isinstance(raw, dict):
            raise ConnectorError("gdelt: ArtList did not return a JSON object")
        articles = raw.get("articles") or []
        if not articles:
            raise ConnectorError("gdelt: ArtList JSON has no 'articles'")
        out: list[dict] = []
        for art in articles:
            if not isinstance(art, dict):
                continue
            title = str(art.get("title") or "").strip()
            if not title:
                continue
            published = None
            seen = str(art.get("seendate") or "").strip()
            if seen:
                try:
                    published = datetime.strptime(
                        seen, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
                except ValueError:
                    published = None
            out.append({
                "title": title,
                "url": str(art.get("url") or ""),
                "source": str(art.get("domain") or ""),
                "published_at": published,
                "provider": "gdelt",
            })
        if not out:
            raise ConnectorError("gdelt: ArtList has no usable articles")
        return out

    def fetch_headlines(self, max_records: int = 15) -> list[dict]:
        """Fetch real gold headlines (last 24h, newest first). Keyless."""
        self._throttle()
        raw = self._get_json(GDELT_URL, params={
            "query": QUERY, "mode": "ArtList", "format": "json",
            "timespan": "24h", "maxrecords": max_records,
            "sort": "datedesc"})
        return self.parse_headlines(raw)
