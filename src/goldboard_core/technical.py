"""Technical summary — the free local version of Finnhub's premium
Aggregate Indicator endpoint.

Finnhub's /scan/technical-indicator returns buy/neutral/sell counts
across MACD/RSI/MA plus an ADX trend reading, but only on a paid
plan. Vanta computes the same summary locally from its stored Yahoo
history (daily bars; intraday bars when available), so it costs
nothing and needs no key.

The summary is ADVISORY ONLY and deliberately separate from the
frozen institutional score (RULE_VERSION v0.1.0): it describes what
price itself is doing, while the score describes what institutions
are doing. Both are shown; neither overrides the other.
"""

from __future__ import annotations

from .indicators import (adx, atr, atr_percentile, bollinger,
                         choppiness, efficiency_ratio, macd, rsi, sma)

MIN_DAILY_BARS = 50  # enough to seed SMA200? No — 50 seeds RSI/MACD/ADX


def _last(values: list[float | None]) -> float | None:
    for value in reversed(values):
        if value is not None:
            return value
    return None


def technical_summary(daily_bars: list[dict],
                      intraday_bars: list[dict] | None = None) -> dict:
    """Build the technical summary from stored OHLC bars.

    ``daily_bars``: oldest-first daily bars (needs >= 50 for a full
    read; fewer returns a partial summary with ``complete: False``).
    Returns a dict with indicator values, per-indicator signals,
    aggregate counts + overall signal, and the trend regime.
    """
    closes = [float(b["close"]) for b in daily_bars]
    out: dict = {"complete": False, "bars_daily": len(daily_bars),
                 "signals": [], "buy": 0, "neutral": 0, "sell": 0,
                 "signal": "neutral", "trending": None, "adx": None,
                 "rsi14": None, "macd": None, "macd_signal": None,
                 "macd_hist": None, "sma20": None, "sma50": None,
                 "sma200": None, "atr14": None, "bb_position": None,
                 "regime": "no data"}
    if len(closes) < 5:
        return out
    last_close = closes[-1]
    rsi_s = rsi(closes, 14)
    macd_line, macd_sig, macd_hist = macd(closes)
    sma20 = sma(closes, 20)
    sma50 = sma(closes, 50)
    sma200 = sma(closes, 200)
    atr_s = atr(daily_bars, 14)
    _, upper, lower = bollinger(closes, 20, 2.0)
    adx_s, plus_di, minus_di = adx(daily_bars, 14)

    rsi_v = _last(rsi_s)
    macd_v = _last(macd_line)
    macd_sig_v = _last(macd_sig)
    macd_hist_v = _last(macd_hist)
    sma20_v = _last(sma20)
    sma50_v = _last(sma50)
    sma200_v = _last(sma200)
    atr_v = _last(atr_s)
    adx_v = _last(adx_s)
    upper_v = _last(upper)
    lower_v = _last(lower)
    plus_di_v = _last(plus_di)
    minus_di_v = _last(minus_di)

    out.update({"rsi14": rsi_v, "macd": macd_v, "macd_signal": macd_sig_v,
                "macd_hist": macd_hist_v, "sma20": sma20_v, "sma50": sma50_v,
                "sma200": sma200_v, "atr14": atr_v, "adx": adx_v,
                "plus_di": plus_di_v, "minus_di": minus_di_v,
                "last_close": last_close})

    # Bollinger %B: where the close sits inside the bands (0=lower, 1=upper).
    if upper_v is not None and lower_v is not None and upper_v > lower_v:
        out["bb_position"] = (last_close - lower_v) / (upper_v - lower_v)

    # Per-indicator signals (same vocabulary as Finnhub's aggregate).
    signals: list[dict] = []

    def _add(name: str, signal: str, value: float | None,
             detail: str = "") -> None:
        signals.append({"name": name, "signal": signal,
                        "value": value, "detail": detail})

    if rsi_v is not None:
        _add("RSI(14)",
             "buy" if rsi_v < 30 else "sell" if rsi_v > 70 else "neutral",
             rsi_v, "oversold <30 = buy, overbought >70 = sell")
    else:
        _add("RSI(14)", "neutral", None, "")
    if macd_v is not None and macd_sig_v is not None:
        _add("MACD", "buy" if macd_v > macd_sig_v else "sell", macd_hist_v,
             "MACD above signal = buy")
    if sma20_v is not None:
        _add("Price vs SMA20", "buy" if last_close > sma20_v else "sell",
             last_close - sma20_v, "close above SMA20 = buy")
    if sma50_v is not None:
        _add("Price vs SMA50", "buy" if last_close > sma50_v else "sell",
             last_close - sma50_v, "close above SMA50 = buy")
    if sma50_v is not None and sma200_v is not None:
        _add("SMA50 vs SMA200",
             "buy" if sma50_v > sma200_v else "sell",
             sma50_v - sma200_v, "golden cross = buy")
    if plus_di_v is not None and minus_di_v is not None:
        _add("+DI vs -DI", "buy" if plus_di_v > minus_di_v else "sell",
             plus_di_v - minus_di_v, "+DI above -DI = buy")
    if out["bb_position"] is not None:
        pos = out["bb_position"]
        _add("Bollinger %B",
             "buy" if pos < 0.1 else "sell" if pos > 0.9 else "neutral",
             pos, "near lower band = buy, near upper = sell")

    # Intraday momentum (15m bars): same close vs its SMA20.
    if intraday_bars and len(intraday_bars) >= 25:
        intra_closes = [float(b["close"]) for b in intraday_bars]
        intra_sma20 = _last(sma(intra_closes, 20))
        if intra_sma20 is not None:
            _add("Intraday (15m) vs SMA20",
                 "buy" if intra_closes[-1] > intra_sma20 else "sell",
                 intra_closes[-1] - intra_sma20,
                 "short-term trend agrees or not")

    for sig in signals:
        out[sig["signal"]] += 1
    out["signals"] = signals
    total = out["buy"] + out["sell"]
    if out["buy"] >= 5 and out["sell"] == 0:
        out["signal"] = "strong buy"
    elif out["sell"] >= 5 and out["buy"] == 0:
        out["signal"] = "strong sell"
    elif out["buy"] > out["sell"]:
        out["signal"] = "buy"
    elif out["sell"] > out["buy"]:
        out["signal"] = "sell"
    else:
        out["signal"] = "neutral"

    # Regime: trending (ADX>25) vs ranging (ADX<20), with direction.
    if adx_v is not None:
        out["trending"] = adx_v > 25
        if adx_v > 25:
            direction = ("up" if (plus_di_v or 0) > (minus_di_v or 0)
                         else "down")
            out["regime"] = f"trending {direction} (ADX {adx_v:.0f})"
        elif adx_v < 20:
            out["regime"] = f"ranging (ADX {adx_v:.0f})"
        else:
            out["regime"] = f"transition (ADX {adx_v:.0f})"
    # Regime filter (v0.5.0) — the spec's 4-state classification from
    # three independent methods, so no single indicator decides:
    #   TRENDING: ER > 0.5 AND CHOP < 38.2 AND ADX > 25
    #   CHOPPY:   ER < 0.3 AND CHOP > 61.8
    #   HIGH-RISK: ATR percentile > 90 (volatility extreme for gold)
    #   UNCLEAR:  everything else
    # The spec's spread rule (spread > 2x median) is NOT measurable
    # from Yahoo bars — there is no spread in them — so the panel says
    # so instead of inventing one. The spec's news window is covered
    # by the setups engine's event caution (any macro event ≤ 2 days).
    er_v = _last(efficiency_ratio(closes, 20))
    chop_v = _last(choppiness(daily_bars, 14))
    atr_pct_v = atr_percentile(atr_s, 250)
    regime_filter: dict = {"label": "unknown", "er": er_v,
                          "chop": chop_v, "atr_percentile": atr_pct_v,
                          "reasons": []}
    if er_v is not None and chop_v is not None and adx_v is not None:
        if atr_pct_v is not None and atr_pct_v > 90:
            regime_filter["label"] = "high-risk"
            regime_filter["reasons"].append(
                f"ATR percentile {atr_pct_v:.0f} — volatility is extreme "
                "for gold (above 90).")
        elif er_v > 0.5 and chop_v < 38.2 and adx_v > 25:
            regime_filter["label"] = "trending"
            regime_filter["reasons"].append(
                f"ER {er_v:.2f} > 0.5, CHOP {chop_v:.1f} < 38.2, "
                f"ADX {adx_v:.0f} > 25 — three methods agree: trending.")
        elif er_v < 0.3 and chop_v > 61.8:
            regime_filter["label"] = "choppy"
            regime_filter["reasons"].append(
                f"ER {er_v:.2f} < 0.3, CHOP {chop_v:.1f} > 61.8 — "
                "price is wandering, not marching.")
        else:
            regime_filter["label"] = "unclear"
            regime_filter["reasons"].append(
                f"ER {er_v:.2f}, CHOP {chop_v:.1f}, ADX {adx_v:.0f} — "
                "the methods do not agree; no new read.")
    else:
        regime_filter["reasons"].append(
            "Not enough history yet for ER / CHOP / ADX.")
    out["regime_filter"] = regime_filter
    out["complete"] = len(closes) >= MIN_DAILY_BARS
    return out
