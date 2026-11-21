"""Core data models for GoldBoard.

All models are plain dataclasses — no I/O, no invented data. A snapshot
records exactly what a source published (with vintage, source URL and a
proxy flag); a score/signal is always stamped with the rule version that
produced it so old outputs stay explainable after the rules change.

Honesty rules baked into the design (from research, 2026-11-21):
- Individual institutions' SL/TP levels are NOT public and are never
  represented here. Any level the board outputs comes from its own
  frozen rule (ATR-based, see signals.py) and is labelled as such.
- The World Bank holds/trades no gold; the IMF holds 2,814.1t but does
  not trade it — neither appears as a "flow" source.
- GOFO / gold lease rates: LBMA stopped publishing GOFO in Jan 2015.
  There is no public lease-rate benchmark; any lease figure is a
  futures-carry proxy and must carry is_proxy=True + proxy_formula.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

SnapshotStatus = Literal["ok", "stale", "failed"]
Direction = Literal["buy", "sell", "none"]
TradeStatus = Literal["open", "closed", "expired"]
TradeOutcome = Literal["win", "loss", "breakeven", "expired"]


@dataclass
class SourceSnapshot:
    """One observed value from one public source, with full provenance."""

    source: str  # e.g. "cftc", "wgc", "fred", "etf", "comex", "lbma", "gdelt"
    series_id: str  # e.g. "cot_managed_money_net_pctile_52w"
    value: float
    unit: str = ""
    as_of: datetime | None = None  # reference period the value describes
    published_at: datetime | None = None  # when the source published it
    fetched_at: datetime | None = None  # when this board fetched it
    status: SnapshotStatus = "ok"
    source_url: str = ""
    is_proxy: bool = False  # True for derived/proxy series (e.g. implied lease)
    proxy_formula: str | None = None  # required when is_proxy=True
    vintage: str | None = None  # source vintage / revision label, if any


@dataclass
class ComponentScore:
    """One scored pillar of the total score, -100 (bearish) .. +100 (bullish)."""

    name: str  # one of: cot, central_bank_etf, macro, news, microstructure
    score: float  # -100..100
    weight: float  # effective (renormalised) weight, sums to 1 over present
    reason: str
    source: str = ""
    as_of: datetime | None = None


@dataclass
class ScoreResult:
    """Weighted total score plus its per-component breakdown."""

    total: float  # -100..100
    components: list[ComponentScore] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    as_of: datetime | None = None
    rule_version: str = "v0.1.0"


@dataclass
class Signal:
    """A frozen advisory signal (shadow only — never a live order)."""

    direction: Direction
    entry: float
    sl: float | None  # ATR-based invalidation; None when direction == "none"
    tp: float | None  # 2R target; None when direction == "none"
    confidence: float  # 0..1 (0 when direction == "none")
    reason: str
    generated_at: datetime | None = None
    rule_version: str = "v0.1.0"
    horizon_days: int = 10
    status: str = "open"  # open | closed | expired


@dataclass
class GateResult:
    """One validation gate (G0–G8); gates never fake a pass."""

    name: str  # e.g. "G0"
    passed: bool
    detail: str
    threshold: str | None = None


@dataclass
class ShadowTrade:
    """A signal tracked in shadow mode until it closes or expires."""

    direction: Direction
    entry: float
    sl: float | None
    tp: float | None
    generated_at: datetime | None = None
    rule_version: str = "v0.1.0"
    horizon_days: int = 10
    status: TradeStatus = "open"
    outcome: TradeOutcome | None = None
    exit_price: float | None = None
    closed_at: datetime | None = None
    r_multiple: float | None = None  # profit/loss in units of initial risk
    reason: str = ""
    signal_id: int | None = None
    id: int | None = None

    @classmethod
    def from_signal(cls, signal: Signal, signal_id: int | None = None) -> "ShadowTrade":
        """Freeze a Signal into a shadow trade for outcome tracking."""
        return cls(
            direction=signal.direction,
            entry=signal.entry,
            sl=signal.sl,
            tp=signal.tp,
            generated_at=signal.generated_at,
            rule_version=signal.rule_version,
            horizon_days=signal.horizon_days,
            status="open",
            reason=signal.reason,
            signal_id=signal_id,
        )
