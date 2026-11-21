"""Local technical indicators — computed from stored OHLC bars, free.

Finnhub's Technical Indicator / Aggregate Indicator endpoints are
Premium-only (checked 2026-11-21: both pages say "Premium Access
Required"), so a free Finnhub key cannot use them. Vanta does not
need them: Yahoo history (already stored) is enough to compute the
same indicators locally — SMA, EMA, RSI, MACD, ADX, Bollinger, ATR.

Pure functions over close/high/low lists (oldest first). Wilder's
smoothing is used for RSI/ADX/ATR, matching the replay rule's ATR.
Nothing here is a trade order: the technical summary is advisory
only, separate from the frozen institutional score.
"""

from __future__ import annotations


def sma(values: list[float], period: int) -> list[float | None]:
    """Simple moving average; None until ``period`` values exist."""
    out: list[float | None] = []
    window = 0.0
    for i, value in enumerate(values):
        window += float(value)
        if i >= period:
            window -= float(values[i - period])
        out.append(window / period if i >= period - 1 else None)
    return out


def ema(values: list[float], period: int) -> list[float | None]:
    """Exponential moving average (seeded with the first SMA)."""
    out: list[float | None] = []
    k = 2.0 / (period + 1)
    prev: float | None = None
    for i, value in enumerate(values):
        value = float(value)
        if i < period - 1:
            out.append(None)
        elif i == period - 1:
            prev = sum(float(v) for v in values[:period]) / period
            out.append(prev)
        else:
            prev = value * k + prev * (1.0 - k)  # type: ignore[operator]
            out.append(prev)
    return out


def rsi(closes: list[float], period: int = 14) -> list[float | None]:
    """Wilder's RSI; None until ``period`` gains/losses exist."""
    out: list[float | None] = [None] * len(closes)
    if len(closes) <= period:
        return out
    gains = [max(0.0, closes[i] - closes[i - 1]) for i in range(1, len(closes))]
    losses = [max(0.0, closes[i - 1] - closes[i]) for i in range(1, len(closes))]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(closes)):
        if i > period:
            avg_gain = (avg_gain * (period - 1) + gains[i - 1]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i - 1]) / period
        if avg_loss == 0 and avg_gain == 0:
            out[i] = 50.0  # flat market: neutral, not "overbought"
        elif avg_loss == 0:
            out[i] = 100.0
        elif avg_gain == 0:
            out[i] = 0.0
        else:
            rs = avg_gain / avg_loss
            out[i] = 100.0 - 100.0 / (1.0 + rs)
    return out


def macd(closes: list[float], fast: int = 12, slow: int = 26,
         signal: int = 9) -> tuple[list[float | None], list[float | None],
                                   list[float | None]]:
    """MACD line, signal line, histogram (all None until seeded)."""
    fast_ema = ema(closes, fast)
    slow_ema = ema(closes, slow)
    line: list[float | None] = [
        (f - s) if (f is not None and s is not None) else None
        for f, s in zip(fast_ema, slow_ema)]
    # Signal = EMA of the MACD line over its seeded values.
    seeded = [v for v in line if v is not None]
    sig_seeded = ema(seeded, signal) if seeded else []
    sig: list[float | None] = []
    j = 0
    for value in line:
        if value is None:
            sig.append(None)
        else:
            sig.append(sig_seeded[j] if j < len(sig_seeded) else None)
            j += 1
    hist: list[float | None] = [
        (m - s) if (m is not None and s is not None) else None
        for m, s in zip(line, sig)]
    return line, sig, hist


def atr(bars: list[dict], period: int = 14) -> list[float | None]:
    """Wilder's ATR — None until ``period`` true ranges are available.

    Same math as the replay rule used to use (moved here so every
    consumer shares one implementation).
    """
    trs: list[float] = []
    for i, bar in enumerate(bars):
        high, low, close = (float(bar["high"]), float(bar["low"]),
                            float(bar["close"]))
        if i == 0:
            trs.append(high - low)
        else:
            prev_close = float(bars[i - 1]["close"])
            trs.append(max(high - low, abs(high - prev_close),
                           abs(low - prev_close)))
    out: list[float | None] = []
    for i in range(len(bars)):
        if i < period - 1:
            out.append(None)
        elif i == period - 1:
            out.append(sum(trs[:period]) / period)
        else:
            out.append((out[-1] * (period - 1) + trs[i]) / period)  # type: ignore[operator]
    return out


def bollinger(closes: list[float], period: int = 20,
              mult: float = 2.0) -> tuple[list[float | None],
                                         list[float | None],
                                         list[float | None]]:
    """Bollinger middle/upper/lower bands."""
    mid = sma(closes, period)
    upper: list[float | None] = []
    lower: list[float | None] = []
    for i, value in enumerate(closes):
        if i < period - 1 or mid[i] is None:
            upper.append(None)
            lower.append(None)
            continue
        window = [float(v) for v in closes[i - period + 1:i + 1]]
        mean = sum(window) / period
        var = sum((v - mean) ** 2 for v in window) / period
        sd = var ** 0.5
        upper.append(mid[i] + mult * sd)  # type: ignore[operator]
        lower.append(mid[i] - mult * sd)  # type: ignore[operator]
    return mid, upper, lower


def adx(bars: list[dict], period: int = 14) -> tuple[list[float | None],
                                                    list[float | None],
                                                    list[float | None]]:
    """Wilder's ADX (+DI, -DI). ADX > 25 = trending, < 20 = ranging."""
    highs = [float(b["high"]) for b in bars]
    lows = [float(b["low"]) for b in bars]
    closes = [float(b["close"]) for b in bars]
    n = len(bars)
    plus_dm: list[float] = [0.0] * n
    minus_dm: list[float] = [0.0] * n
    for i in range(1, n):
        up = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        plus_dm[i] = up if up > down and up > 0 else 0.0
        minus_dm[i] = down if down > up and down > 0 else 0.0
    adx_out: list[float | None] = [None] * n
    plus_di: list[float | None] = [None] * n
    minus_di: list[float | None] = [None] * n
    if n <= 2 * period:
        return adx_out, plus_di, minus_di
    # Wilder smoothing of TR / +DM / -DM, seeded at index period.
    # Raw TR list for the seed (atr() returns smoothed values).
    raw_tr: list[float] = []
    for i in range(n):
        if i == 0:
            raw_tr.append(highs[0] - lows[0])
        else:
            raw_tr.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]),
                              abs(lows[i] - closes[i - 1])))
    s_tr = sum(raw_tr[:period])
    s_plus = sum(plus_dm[:period])
    s_minus = sum(minus_dm[:period])
    dx: list[float | None] = [None] * n
    for i in range(period, n):
        if i > period:
            s_tr = s_tr - s_tr / period + raw_tr[i]
            s_plus = s_plus - s_plus / period + plus_dm[i]
            s_minus = s_minus - s_minus / period + minus_dm[i]
        if s_tr == 0:
            continue
        p_di = 100.0 * s_plus / s_tr
        m_di = 100.0 * s_minus / s_tr
        plus_di[i] = p_di
        minus_di[i] = m_di
        denom = p_di + m_di
        dx[i] = (100.0 * abs(p_di - m_di) / denom) if denom else 0.0
    # ADX = Wilder average of DX, seeded after 2*period bars.
    seed = [v for v in dx[period:2 * period] if v is not None]
    if len(seed) == period:
        prev = sum(seed) / period
        adx_out[2 * period - 1] = prev
        for i in range(2 * period, n):
            if dx[i] is None:
                continue
            prev = (prev * (period - 1) + dx[i]) / period
            adx_out[i] = prev
    return adx_out, plus_di, minus_di
