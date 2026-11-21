"""Shadow validation runner (Phase 3) — advisory only, never a trade.

A :class:`ShadowRunner` freezes advisory signals into shadow trades
in the DB and tracks them to close/expiry. All writes go through
``goldboard_core.storage``; nothing here places an order, touches a
broker, or moves real money. SHADOW ONLY.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from .models import ShadowTrade, Signal
from .storage import (
    close_shadow_trade,
    close_shadow_trade_expired,
    get_meta,
    get_shadow_trade,
    init_db,
    list_shadow_trades,
    log_shadow_trade,
    save_signal,
    set_meta,
)

DEFAULT_HORIZON_DAYS = 10
UNREALISED_R_META_PREFIX = "shadow_unrealised_r"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _to_aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _unrealised_r(trade: ShadowTrade, price: float) -> float | None:
    """Mark-to-market R for an open trade at ``price`` (not a close)."""
    if trade.direction == "buy" and trade.sl is not None:
        risk = trade.entry - trade.sl
        return (price - trade.entry) / risk if risk else None
    if trade.direction == "sell" and trade.sl is not None:
        risk = trade.sl - trade.entry
        return (trade.entry - price) / risk if risk else None
    return None


class ShadowRunner:
    """Open / close / expire / mark shadow trades in one DB."""

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        init_db(db_path)

    # ---------------------------------------------------------------- open

    def open_signal(self, signal: Signal, price: float | None = None,
                    atr: float | None = None) -> int:
        """Freeze a signal into an open shadow trade; returns the trade id.

        The signal's own frozen levels (entry/SL/TP) are authoritative —
        ``price``/``atr`` are validated (price must be > 0 when given)
        and recorded in the trade reason, never used to rewrite the
        signal. Signals with direction "none" (or no SL/TP) have
        nothing to track and raise ValueError.
        """
        if signal.direction not in ("buy", "sell"):
            raise ValueError(
                f"cannot open a shadow trade for direction "
                f"{signal.direction!r} — only buy/sell signals are tracked"
            )
        if signal.sl is None or signal.tp is None:
            raise ValueError("cannot open a shadow trade without SL/TP levels")
        if price is not None and price <= 0:
            raise ValueError("price must be a positive number when given")
        if atr is not None and atr <= 0:
            raise ValueError("atr must be a positive number when given")

        signal_id = save_signal(self.db_path, signal)
        trade = ShadowTrade.from_signal(signal, signal_id=signal_id)
        if price is not None:
            trade.reason = (
                f"{trade.reason} [shadow open at market {price:.2f}"
                + (f", ATR {atr:.2f}" if atr is not None else "")
                + "; SHADOW ONLY — no order placed]"
            )
        return log_shadow_trade(self.db_path, trade)

    # --------------------------------------------------------------- close

    def _resolve_open_trade(self, signal_id: int) -> ShadowTrade | None:
        """Find the trade to close: first by trade id, then by signal id."""
        trade = get_shadow_trade(self.db_path, signal_id)
        if trade is not None:
            return trade
        for candidate in list_shadow_trades(self.db_path, status="open"):
            if candidate.signal_id == signal_id:
                return candidate
        return None

    def close_signal(self, signal_id: int, exit_price: float,
                     exit_at: datetime | None = None,
                     reason: str = "") -> ShadowTrade | None:
        """Close the shadow trade for ``signal_id`` at ``exit_price``.

        ``signal_id`` may be the shadow-trade id returned by
        :meth:`open_signal` or the signals-table id of the frozen
        signal. R is computed by storage against initial risk.
        Returns the closed trade, or None when nothing matches.
        Already-closed trades are returned unchanged.
        """
        trade = self._resolve_open_trade(signal_id)
        if trade is None:
            return None
        # Note: storage.close_shadow_trade stores no close reason; the
        # ``reason`` argument is accepted for the caller's own log and
        # is not persisted. The returned trade is exactly what is in
        # the DB.
        return close_shadow_trade(
            self.db_path, trade.id, exit_price,
            closed_at=_to_aware(exit_at) or _now(),
        )

    # -------------------------------------------------------------- expire

    def expire_stale(self, horizon_days: int = DEFAULT_HORIZON_DAYS,
                     price: float | None = None,
                     now: datetime | None = None) -> list[ShadowTrade]:
        """Close open trades older than ``horizon_days`` (or each trade's
        own horizon when ``horizon_days`` is None) at ``price``.

        When no current ``price`` is supplied the trade is closed at its
        entry (R = 0 by construction, never a market exit) and the
        reason says so — no price is ever invented. Expired trades get
        outcome "expired" and still count as closed trades for the
        gates. Returns the expired trades.
        """
        moment = _to_aware(now) or _now()
        expired: list[ShadowTrade] = []
        for trade in list_shadow_trades(self.db_path, status="open"):
            horizon = trade.horizon_days if horizon_days is None else horizon_days
            generated = _to_aware(trade.generated_at)
            if generated is None:
                continue
            if (moment - generated).days > horizon:
                exit_price = price if price is not None else trade.entry
                reason = (
                    "expired: no TP/SL hit within horizon"
                    if price is not None
                    else "expired: no TP/SL hit within horizon; no current "
                         "price supplied — closed at entry (R=0, not a market exit)"
                )
                closed = close_shadow_trade_expired(
                    self.db_path, trade.id, exit_price,
                    closed_at=moment, reason=reason,
                )
                if closed is not None:
                    expired.append(closed)
        return expired

    # ---------------------------------------------------------- daily mark

    def daily_mark(self, price: float, now: datetime | None = None) -> dict[int, float]:
        """Update unrealised R for every open trade at ``price``.

        Stored per trade in meta (``shadow_unrealised_r:<trade id>``)
        with the mark time; open trades stay open — a mark is never
        counted as a close. Returns {trade id: unrealised R}.
        """
        if price <= 0:
            raise ValueError("price must be a positive number")
        moment = _to_aware(now) or _now()
        marks: dict[int, float] = {}
        for trade in list_shadow_trades(self.db_path, status="open"):
            r_value = _unrealised_r(trade, price)
            if r_value is None or trade.id is None:
                continue
            marks[trade.id] = r_value
            set_meta(self.db_path, f"{UNREALISED_R_META_PREFIX}:{trade.id}",
                     f"{r_value:.6f}")
        set_meta(self.db_path, "shadow_last_mark_at", moment.isoformat())
        return marks

    def get_unrealised_r(self, trade_id: int) -> float | None:
        """Last stored unrealised R for a trade (None if never marked)."""
        raw = get_meta(self.db_path, f"{UNREALISED_R_META_PREFIX}:{trade_id}")
        if raw is None:
            return None
        try:
            return float(raw)
        except ValueError:
            return None

    def list_open(self) -> list[ShadowTrade]:
        """All currently open shadow trades, oldest first."""
        return list_shadow_trades(self.db_path, status="open")
