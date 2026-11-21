"""Vanta data connectors (Phase 2) — one module per public source.

All connectors return lists of ``SourceSnapshot`` (from
``goldboard_core.models`` when present, else the identical local
dataclass in ``base``), never invent a value on failure, and label
proxies (implied lease, Yahoo quotes) with ``is_proxy=True`` + formula.
Individual institutions' SL/TP levels are NOT public and are never
represented; the World Bank holds/trades no gold; the IMF holds
2,814.1t but does not trade it; GOFO ended Jan 2015.
"""

from .base import BaseConnector, ConnectorError, SourceSnapshot
from .comex import COMEXConnector
from .cot import COTConnector
from .etf import ETFConnector
from .fedwatch import FedWatchConnector, next_meetings
from .finnhub import FinnhubConnector
from .fred import FREDConnector
from .gdelt import GDELTConnector
from .lbma import LBMAConnector
from .microstructure import MicrostructureConnector
from .price import PriceConnector
from .registry import REGISTRY, get_connector, list_connectors, refresh_all
from .supply import SupplyConnector
from .wgc import IMF_HOLDINGS_TONNES, WGCConnector

__all__ = [
    "BaseConnector", "ConnectorError", "SourceSnapshot",
    "COTConnector", "FREDConnector", "WGCConnector", "ETFConnector",
    "COMEXConnector", "LBMAConnector", "FedWatchConnector",
    "GDELTConnector", "FinnhubConnector", "SupplyConnector",
    "MicrostructureConnector", "PriceConnector",
    "REGISTRY", "get_connector", "list_connectors", "refresh_all",
    "next_meetings", "IMF_HOLDINGS_TONNES",
]
