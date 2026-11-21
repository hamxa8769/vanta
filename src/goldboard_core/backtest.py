"""Backtest engine (Phase 3) — pure functions over supplied data.

No live fetching, no invented prices: every price and every signal
comes from the caller (e.g. :func:`load_cot_price_history` on a
fixture CSV). Levels are the board's own ATR-based levels from
``signals.generate_signal`` — never institutional stops.

Rules (exact):
    * A signal is evaluated at the NEXT price bar strictly after its
      ``generated_at`` — never the bar it was generated on (no
      look-ahead).
    * Within ``horizon_days`` (default 10, per-signal override wins):
      TP hit first  -> exit at the TP level, +2R.
      SL hit first  -> exit at the SL level, -1R.
      A bar's high/low decides the hit; when only a close is supplied
      the close is used for both. If TP and SL are both inside one
      bar's range, the SL is taken first (conservative — the adverse
      move is assumed to have happened first).
    * Neither hit within the horizon -> expired at the close of the
      last bar inside the horizon window; R = (exit - entry) / risk
      for buys, (entry - exit) / risk for sells, risk = |entry - SL|.
    * R is per unit of initial risk. "Win" means R > 0 (including a
      profitable expiry); "loss" means R < 0.

Sharpe is per-trade only (mean R / population std of R, no time
scaling, no annualisation) — it is labelled as such wherever it is
reported. With fewer than 2 trades, or zero variance, it is 0.0.
"""

from __future__ import annotations

import csv
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

DEFAULT_HORIZON_DAYS = 10


# --------------------------------------------------------------------------
# Result models
# --------------------------------------------------------------------------

@dataclass
class TradeResult:
    """One evaluated signal: what happened, at what exit, for how much R."""

    direction: str
    entry: float
    sl: float | None
    tp: float | None
    generated_at: datetime | None
    outcome: str  # "tp" | "sl" | "expired"
    exit_price: float
    exit_at: datetime | None
    r_multiple: float
    bars_held: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "direction": self.direction,
            "entry": self.entry,
            "sl": self.sl,
            "tp": self.tp,
            "generated_at": self.generated_at.isoformat() if self.generated_at else None,
            "outcome": self.outcome,
            "exit_price": self.exit_price,
            "exit_at": self.exit_at.isoformat() if self.exit_at else None,
            "r_multiple": self.r_multiple,
            "bars_held": self.bars_held,
        }


@dataclass
class BacktestResult:
    """Aggregate outcome of :func:`backtest` over one set of signals."""

    n_trades: int = 0
    wins: int = 0
    losses: int = 0
    breakevens: int = 0
    tp_hits: int = 0
    sl_hits: int = 0
    expired: int = 0
    skipped: int = 0  # signals that could not be evaluated at all
    total_r: float = 0.0
    win_rate: float = 0.0
    expectancy_r: float = 0.0
    profit_factor: float = 0.0
    max_drawdown_r: float = 0.0
    sharpe_per_trade: float = 0.0  # per-trade only; NOT annualised
    trades: list[TradeResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "n_trades": self.n_trades,
            "wins": self.wins,
            "losses": self.losses,
            "breakevens": self.breakevens,
            "tp_hits": self.tp_hits,
            "sl_hits": self.sl_hits,
            "expired": self.expired,
            "skipped": self.skipped,
            "total_r": self.total_r,
            "win_rate": self.win_rate,
            "expectancy_r": self.expectancy_r,
            "profit_factor": self.profit_factor,
            "max_drawdown_r": self.max_drawdown_r,
            "sharpe_per_trade": self.sharpe_per_trade,
            "sharpe_note": "per-trade only (mean R / population std of R); "
                           "no time scaling, not annualised",
            "trades": [t.to_dict() for t in self.trades],
        }


# --------------------------------------------------------------------------
# Normalisation helpers (accept dicts, Signal dataclasses, bar tuples/dicts)
# --------------------------------------------------------------------------

def _as_signal_dict(signal: Any) -> dict[str, Any]:
    if isinstance(signal, dict):
        return dict(signal)
    return {
        "direction": getattr(signal, "direction", None),
        "entry": getattr(signal, "entry", None),
        "sl": getattr(signal, "sl", None),
        "tp": getattr(signal, "tp", None),
        "generated_at": getattr(signal, "generated_at", None),
        "horizon_days": getattr(signal, "horizon_days", None),
    }


def _parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _as_bar_tuple(bar: Any) -> tuple[datetime, float, float, float]:
    """Normalise one price bar to (timestamp, close, high, low)."""
    if isinstance(bar, dict):
        ts = _parse_dt(bar.get("date") or bar.get("timestamp") or bar.get("as_of"))
        close = float(bar["price"] if "price" in bar else bar["close"])
        high = float(bar["high"]) if bar.get("high") is not None else close
        low = float(bar["low"]) if bar.get("low") is not None else close
        return (ts, close, high, low)
    seq = tuple(bar)
    if len(seq) == 2:
        ts, close = seq
        return (_parse_dt(ts), float(close), float(close), float(close))
    if len(seq) == 4:
        ts, close, high, low = seq
        return (_parse_dt(ts), float(close), float(high), float(low))
    raise ValueError(f"unrecognised price bar shape: {bar!r}")


def _max_drawdown_r(r_values: list[float]) -> float:
    peak = 0.0
    running = 0.0
    worst = 0.0
    for r in r_values:
        running += r
        peak = max(peak, running)
        worst = max(worst, peak - running)
    return worst


# --------------------------------------------------------------------------
# Core backtest
# --------------------------------------------------------------------------

def backtest(
    signals: list[Any],
    prices: list[Any],
    horizon_days: int = DEFAULT_HORIZON_DAYS,
) -> BacktestResult:
    """Evaluate signals against later price bars. Pure — no I/O.

    ``signals``: dicts (or ``Signal`` objects) with
    entry/sl/tp/direction/generated_at (optional horizon_days).
    ``prices``: (datetime, close) or (datetime, close, high, low)
    tuples, or dicts with date/price/high/low keys.
    """
    bars = sorted((_as_bar_tuple(b) for b in prices), key=lambda b: b[0])
    result = BacktestResult()

    for raw in signals:
        sig = _as_signal_dict(raw)
        direction = str(sig.get("direction") or "none").lower()
        entry = sig.get("entry")
        sl = sig.get("sl")
        tp = sig.get("tp")
        generated_at = _parse_dt(sig.get("generated_at"))
        horizon = sig.get("horizon_days") or horizon_days
        try:
            horizon = int(horizon)
        except (TypeError, ValueError):
            horizon = horizon_days

        # A "none" signal, or one without levels, is not a trade.
        if direction not in ("buy", "sell") or entry is None or sl is None or tp is None:
            result.skipped += 1
            continue
        entry = float(entry)
        sl = float(sl)
        tp = float(tp)
        risk = abs(entry - sl)
        if risk <= 0 or generated_at is None:
            result.skipped += 1
            continue

        # No look-ahead: first bar STRICTLY after generated_at.
        start = next((i for i, b in enumerate(bars) if b[0] > generated_at), None)
        if start is None:
            result.skipped += 1
            continue
        window_end = generated_at + timedelta(days=horizon)
        window = [b for b in bars[start:] if b[0] <= window_end]
        if not window:
            result.skipped += 1
            continue

        outcome = None
        exit_price = None
        exit_at = None
        bars_held = 0
        for idx, (_ts, _close, high, low) in enumerate(window, start=1):
            if direction == "buy":
                sl_hit = low <= sl
                tp_hit = high >= tp
            else:  # sell
                sl_hit = high >= sl
                tp_hit = low <= tp
            if sl_hit or tp_hit:
                # Both inside one bar: SL first (conservative).
                if sl_hit:
                    outcome, exit_price = "sl", sl
                else:
                    outcome, exit_price = "tp", tp
                exit_at = _ts
                bars_held = idx
                break
        if outcome is None:
            outcome = "expired"
            exit_price = window[-1][1]  # close of last bar in the window
            exit_at = window[-1][0]
            bars_held = len(window)

        if direction == "buy":
            r_multiple = (exit_price - entry) / risk
        else:
            r_multiple = (entry - exit_price) / risk
        result.trades.append(
            TradeResult(
                direction=direction, entry=entry, sl=sl, tp=tp,
                generated_at=generated_at, outcome=outcome,
                exit_price=float(exit_price), exit_at=exit_at,
                r_multiple=float(r_multiple), bars_held=bars_held,
            )
        )

    result.trades.sort(key=lambda t: t.exit_at or t.generated_at or datetime.min.replace(tzinfo=timezone.utc))
    result.n_trades = len(result.trades)
    r_values = [t.r_multiple for t in result.trades]
    result.wins = sum(1 for r in r_values if r > 0)
    result.losses = sum(1 for r in r_values if r < 0)
    result.breakevens = sum(1 for r in r_values if r == 0)
    result.tp_hits = sum(1 for t in result.trades if t.outcome == "tp")
    result.sl_hits = sum(1 for t in result.trades if t.outcome == "sl")
    result.expired = sum(1 for t in result.trades if t.outcome == "expired")
    result.total_r = sum(r_values)
    if result.n_trades:
        result.win_rate = result.wins / result.n_trades
        result.expectancy_r = result.total_r / result.n_trades
        gross_win = sum(r for r in r_values if r > 0)
        gross_loss = abs(sum(r for r in r_values if r < 0))
        if gross_loss == 0:
            result.profit_factor = float("inf") if gross_win > 0 else 0.0
        else:
            result.profit_factor = gross_win / gross_loss
        result.max_drawdown_r = _max_drawdown_r(r_values)
        if result.n_trades >= 2:
            mean = result.expectancy_r
            variance = sum((r - mean) ** 2 for r in r_values) / result.n_trades
            std = math.sqrt(variance)
            result.sharpe_per_trade = mean / std if std > 0 else 0.0
    return result


# --------------------------------------------------------------------------
# Walk-forward
# --------------------------------------------------------------------------

def _unpack_split_args(signals: Any, prices: Any, n_splits: Any) -> tuple[list, list, int]:
    """Accept split_backtest(signals, prices, n) and split_backtest(data, n).

    ``data`` may be a dict {"signals": [...], "prices": [...]} (as
    returned by load_cot_price_history) or a (signals, prices) tuple.
    """
    if isinstance(prices, int) and n_splits == 2:
        # Called as split_backtest(data, n_splits).
        n = prices
        if isinstance(signals, dict):
            return list(signals.get("signals", [])), list(signals.get("prices", [])), n
        if isinstance(signals, (list, tuple)) and len(signals) == 2:
            return list(signals[0]), list(signals[1]), n
    return list(signals), list(prices), int(n_splits)


def split_backtest(signals: Any, prices: Any = None, n_splits: int = 2) -> list[dict[str, Any]]:
    """Walk-forward: split signals chronologically into ``n_splits``
    contiguous folds and backtest each fold on the same prices.

    Returns one dict per fold: fold index, n_trades, total_r,
    expectancy_r (None when the fold has no trades), win_rate.
    """
    sig_list, price_list, n = _unpack_split_args(signals, prices, n_splits)
    if n < 1:
        raise ValueError("n_splits must be >= 1")
    if prices is None and not isinstance(signals, (dict, tuple)):
        raise ValueError("split_backtest needs prices (or a data dict/tuple)")

    ordered = sorted(
        (_as_signal_dict(s) for s in sig_list),
        key=lambda s: _parse_dt(s.get("generated_at")) or datetime.min.replace(tzinfo=timezone.utc),
    )
    folds: list[dict[str, Any]] = []
    total = len(ordered)
    base, extra = divmod(total, n)
    start = 0
    for fold in range(n):
        size = base + (1 if fold < extra else 0)
        chunk = ordered[start:start + size]
        start += size
        res = backtest(chunk, price_list)
        folds.append({
            "fold": fold,
            "n_trades": res.n_trades,
            "total_r": res.total_r,
            "expectancy_r": res.expectancy_r if res.n_trades else None,
            "win_rate": res.win_rate if res.n_trades else None,
        })
    return folds


# --------------------------------------------------------------------------
# Historical dataset loader
# --------------------------------------------------------------------------

def load_cot_price_history(path: str | Path) -> dict[str, list]:
    """Load a historical backtest dataset CSV.

    Expected columns: ``date, price`` (required), ``high, low``
    (optional — default to the close), and signal columns
    ``signal_direction, signal_entry, signal_sl, signal_tp,
    signal_horizon_days`` (rows without a signal carry empty fields).
    A row with an empty ``price`` but signal fields is a signal-only
    row (a second frozen signal generated on the same day): it adds a
    signal but no price bar.
    Lines starting with ``#`` are fixture header comments and are
    skipped. Dates are ``YYYY-MM-DD`` or ISO datetimes (naive = UTC).

    Returns {"prices": [(datetime, close, high, low), ...],
             "signals": [{"direction", "entry", "sl", "tp",
                          "generated_at", "horizon_days"}, ...]}.

    Raises FileNotFoundError when the path does not exist — never
    silently substitutes other data.
    """
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(
            f"historical dataset not found: {path} — supply a CSV with "
            "columns date, price, high, low, signal_* (see "
            "data/fixtures/backtest_sample.csv)"
        )
    prices: list[tuple[datetime, float, float, float]] = []
    signals: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8", newline="") as fh:
        lines = [line for line in fh if not line.lstrip().startswith("#")]
    reader = csv.DictReader(lines)
    if reader.fieldnames is None or "date" not in reader.fieldnames or "price" not in reader.fieldnames:
        raise ValueError(
            f"{path}: expected a CSV with at least 'date' and 'price' columns, "
            f"got {reader.fieldnames!r}"
        )
    for row in reader:
        date_text = (row.get("date") or "").strip()
        if not date_text:
            continue
        ts = _parse_dt(date_text)
        price_text = (row.get("price") or "").strip()
        if price_text:
            close = float(price_text)
            high_text = (row.get("high") or "").strip()
            low_text = (row.get("low") or "").strip()
            high = float(high_text) if high_text else close
            low = float(low_text) if low_text else close
            prices.append((ts, close, high, low))
        # A row with an empty price but signal fields is a signal-only
        # row (a second frozen signal generated on the same day): it
        # adds a signal but no price bar.
        direction = (row.get("signal_direction") or "").strip().lower()
        if direction in ("buy", "sell"):
            entry = (row.get("signal_entry") or "").strip()
            sl = (row.get("signal_sl") or "").strip()
            tp = (row.get("signal_tp") or "").strip()
            horizon = (row.get("signal_horizon_days") or "").strip()
            signals.append({
                "direction": direction,
                "entry": float(entry) if entry else None,
                "sl": float(sl) if sl else None,
                "tp": float(tp) if tp else None,
                "generated_at": ts,
                "horizon_days": int(horizon) if horizon else DEFAULT_HORIZON_DAYS,
            })
    prices.sort(key=lambda b: b[0])
    signals.sort(key=lambda s: s["generated_at"])
    return {"prices": prices, "signals": signals}
