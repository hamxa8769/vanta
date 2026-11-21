"""Shared base for Vanta data connectors (Phase 2).

Every connector:
- uses a real HTTP endpoint or documented CSV/JSON feed (no guessed URLs;
  where an exact file URL could not be verified the connector says so in
  its docstring and fails loudly instead of inventing a URL),
- sets a real User-Agent, 15s timeout, 2 retries with backoff,
- returns a list of ``SourceSnapshot`` (imported from ``goldboard_core``
  when Phase 1 has created it, else the local identical dataclass below),
- never invents a value on failure: ``fetch()`` returns ``[]`` and logs,
  with ``self.last_error`` holding the reason; deterministic config
  failures (missing API key) raise ``ConnectorError`` so the registry can
  report them as failed with the reason.
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import requests

try:
    from goldboard_core.models import SourceSnapshot  # Phase 1 core
except Exception:  # pragma: no cover - core not present yet
    @dataclass
    class SourceSnapshot:
        """Local fallback, field-identical to goldboard_core.models.SourceSnapshot."""

        source: str
        series_id: str
        value: float
        unit: str = ""
        as_of: datetime | None = None
        published_at: datetime | None = None
        fetched_at: datetime | None = None
        status: str = "ok"
        source_url: str = ""
        is_proxy: bool = False
        proxy_formula: str | None = None
        vintage: str | None = None


USER_AGENT = "Vanta/0.2 (XAUUSD institutional analysis board; research use)"
DEFAULT_TIMEOUT = 15
MAX_RETRIES = 2  # retries after the first attempt

logger = logging.getLogger("vanta.data")


class ConnectorError(Exception):
    """Raised when a connector cannot produce any trustworthy data."""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class BaseConnector(ABC):
    """Base class for one public data source."""

    name: str = "base"
    source_url: str = ""
    refresh_minutes: int = 60

    def __init__(
        self,
        name: str | None = None,
        source_url: str | None = None,
        refresh_minutes: int | None = None,
    ) -> None:
        self.name = name or self.name
        self.source_url = source_url or self.source_url
        self.refresh_minutes = refresh_minutes or self.refresh_minutes
        self.last_error: str | None = None
        self.log = logging.getLogger(f"vanta.data.{self.name}")

    @abstractmethod
    def fetch(self) -> list[SourceSnapshot]:
        """Fetch + parse. Returns [] (never an invented value) on failure."""

    @abstractmethod
    def parse(self, raw: Any) -> list[SourceSnapshot]:
        """Parse already-fetched raw data into snapshots."""

    # -- HTTP ---------------------------------------------------------
    def _get(self, url: str, params: dict | None = None,
             headers: dict | None = None,
             timeout: int | None = None,
             retries: int | None = None) -> requests.Response:
        """GET with real User-Agent, timeout and retries + backoff.

        Defaults: 15s timeout, 2 retries. Callers that poll fast (the
        live price ticker) pass a short timeout and 0 retries so one
        slow attempt never stalls the loop.
        Returns the response on HTTP 2xx, else raises ConnectorError.
        """
        hdrs = {"User-Agent": USER_AGENT, "Accept": "*/*"}
        if headers:
            hdrs.update(headers)
        timeout = DEFAULT_TIMEOUT if timeout is None else timeout
        max_retries = MAX_RETRIES if retries is None else retries
        last_exc: Exception | None = None
        for attempt in range(max_retries + 1):
            try:
                resp = requests.get(url, params=params, headers=hdrs,
                                    timeout=timeout)
                resp.raise_for_status()
                return resp
            except requests.RequestException as exc:  # timeout / HTTP / DNS
                last_exc = exc
                self.log.warning("%s: GET %s attempt %d failed: %s",
                                 self.name, url, attempt + 1, exc)
                if attempt < max_retries:
                    time.sleep(2 ** attempt)  # 1s, then 2s
        raise ConnectorError(f"{self.name}: GET {url} failed after "
                             f"{max_retries + 1} attempts: {last_exc}")

    def _get_text(self, url: str, params: dict | None = None,
                  timeout: int | None = None,
                  retries: int | None = None) -> str:
        return self._get(url, params=params, timeout=timeout,
                         retries=retries).text

    def _get_json(self, url: str, params: dict | None = None,
                  timeout: int | None = None,
                  retries: int | None = None) -> Any:
        try:
            return self._get(url, params=params, timeout=timeout,
                             retries=retries).json()
        except ValueError as exc:
            raise ConnectorError(f"{self.name}: {url} did not return JSON: {exc}")

    # -- snapshot helper ----------------------------------------------
    def _snapshot(self, series_id: str, value: float, unit: str,
                  as_of: datetime | None = None,
                  published_at: datetime | None = None,
                  status: str = "ok", is_proxy: bool = False,
                  proxy_formula: str | None = None,
                  vintage: str | None = None,
                  source_url: str | None = None) -> SourceSnapshot:
        return SourceSnapshot(
            source=self.name,
            series_id=series_id,
            value=float(value),
            unit=unit,
            as_of=as_of,
            published_at=published_at,
            fetched_at=utcnow(),
            status=status,
            source_url=source_url or self.source_url,
            is_proxy=is_proxy,
            proxy_formula=proxy_formula,
            vintage=vintage,
        )

    def _fail(self, reason: str) -> list[SourceSnapshot]:
        """Record a failure, log it, return an empty list (no invented value)."""
        self.last_error = reason
        self.log.error("%s: %s", self.name, reason)
        return []
