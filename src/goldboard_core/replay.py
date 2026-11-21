"""History replay — price-only backtest signals from stored OHLC bars.

This is NOT the board's institutional score. The score needs COT,
ETF, central-bank and macro features, which exist only for the
present — they cannot be reconstructed for past dates. The replay
therefore uses a transparent, price-only rule, labelled as such
everywhere it is shown:

    * 20-day simple moving average of daily closes.
    * Buy when the close crosses ABOVE the SMA20; sell when it
      crosses BELOW it (evaluated at the bar close — no look-ahead).
    * SL = entry -/+ 1.5 x ATR(14), TP = entry +/- 3.0 x ATR(14)
      (the same 2R risk levels as the frozen signal rule).
    * Horizon 10 days; the backtest engine decides TP/SL/expiry.

Its purpose is to prove the plumbing (history fetch -> store ->
backtest) and to give a baseline on real history — not to claim the
institutional strategy works. Live board signals are evaluated
separately by the same engine (see worker.run_history_backtest).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

SMA_PERIOD = 20
ATR_PERIOD = 14
SL_ATR_MULTIPLE = 1.5
TP_ATR_MULTIPLE = 3.0
HORIZON_DAYS = 10

REPLAY_RULE_TEXT = (
    "Price-only replay: buy on close crossing above SMA20, sell on "
    "crossing below; SL 1.5xATR(14), TP 3.0xATR(14), 10-day horizon. "
    "NOT the institutional score — COT/ETF/macro history is not "
    "available for past dates."
)


def _parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _atr_series(bars: list[dict]) -> list[float | None]:
    """Wilder's ATR(14) — None until 14 true ranges are available."""
    trs: list[float] = []
    for i, bar in enumerate(bars):
        high, low, close = float(bar["high"]), float(bar["low"]), float(bar["close"])
        if i == 0:
            trs.append(high - low)
        else:
            prev_close = float(bars[i - 1]["close"])
            trs.append(max(high - low, abs(high - prev_close),
                           abs(low - prev_close)))
    out: list[float | None] = []
    for i in range(len(bars)):
        if i < ATR_PERIOD - 1:
            out.append(None)
        elif i == ATR_PERIOD - 1:
            out.append(sum(trs[:ATR_PERIOD]) / ATR_PERIOD)
        else:
            out.append((out[-1] * (ATR_PERIOD - 1) + trs[i]) / ATR_PERIOD)
    return out


def generate_replay_signals(bars: list[dict]) -> list[dict]:
    """Generate price-only replay signals from daily OHLC bars.

    ``bars``: dicts with ts/open/high/low/close (oldest first).
    Returns signal dicts for :func:`goldboard_core.backtest.backtest`
    (direction, entry, sl, tp, generated_at, horizon_days).
    Fewer than SMA_PERIOD + 1 bars -> no signals.
    """
    ordered = sorted(bars, key=lambda b: _parse_dt(b.get("ts")) or datetime.min.replace(tzinfo=timezone.utc))
    if len(ordered) < SMA_PERIOD + 1:
        return []
    closes = [float(b["close"]) for b in ordered]
    atrs = _atr_series(ordered)
    signals: list[dict] = []
    prev_above: bool | None = None
    for i in range(SMA_PERIOD - 1, len(ordered)):
        sma = sum(closes[i - SMA_PERIOD + 1:i + 1]) / SMA_PERIOD
        above = closes[i] > sma
        atr = atrs[i]
        if prev_above is not None and above != prev_above and atr and atr > 0:
            entry = closes[i]
            direction = "buy" if above else "sell"
            if direction == "buy":
                sl = entry - SL_ATR_MULTIPLE * atr
                tp = entry + TP_ATR_MULTIPLE * atr
            else:
                sl = entry + SL_ATR_MULTIPLE * atr
                tp = entry - TP_ATR_MULTIPLE * atr
            signals.append({
                "direction": direction,
                "entry": entry,
                "sl": sl,
                "tp": tp,
                "generated_at": _parse_dt(ordered[i].get("ts")),
                "horizon_days": HORIZON_DAYS,
            })
        prev_above = above
    return signals
