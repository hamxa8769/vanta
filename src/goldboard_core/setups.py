"""Setups — Vanta's own transparent rule engine (the "auto bot"
layer, built in the open).

Real institutional bots are secret; Vanta does not pretend to copy
one. Instead it runs a small set of explicit, labelled rules over
two independent views — the local technical summary and the
institutional score — and reports each setup with its reasons, so
the user can see exactly why it fired. A setup is NEVER a trade
order: it is an advisory watchlist entry for the board's shadow
mode, and every rule's hit/miss is meant to be measured before
anything is trusted.

Rules (all need a complete technical summary unless noted):
  * Trend continuation — technical signal + ADX>25 + score agrees.
  * Oversold / overbought bounce — RSI<30 / >70 + score not opposed.
  * Range fade (Muuk-style) — ADX<20 (ranging) + price near SMA20.
    Suppressed when the regime filter says TRENDING: fading a range
    in a trending market is the trap the regime filter exists to
    stop (the spec's "martingale trap-in-range").
  * Breakout watch — close within 0.5% of the 20-day high/low.
  * Event caution — FOMC within 2 days: stand down / size down.
  * Conflict — technical and score strongly disagree: no setup.
"""

from __future__ import annotations


def _setup(name: str, direction: str, confidence: str,
           reasons: list[str], kind: str) -> dict:
    return {"name": name, "direction": direction,
            "confidence": confidence, "reasons": reasons, "kind": kind}


def generate_setups(technical: dict, score_total: float | None,
                    cot_pctile: float | None, etf_gld_change: float | None,
                    fomc_days_away: int | None,
                    daily_bars: list[dict] | None = None,
                    next_event: dict | None = None,
                    regime_filter: dict | None = None) -> list[dict]:
    """Evaluate the setup rules. Pure function — no I/O, no trading."""
    setups: list[dict] = []
    if not technical.get("complete"):
        return [_setup("Not enough history",
                       "none", "low",
                       ["Need 50+ daily bars for the technical read; "
                        "history is still fetching."],
                       "waiting")]
    signal = technical.get("signal", "neutral")
    adx_v = technical.get("adx")
    rsi_v = technical.get("rsi14")
    sma20_v = technical.get("sma20")
    last_close = technical.get("last_close")
    score = score_total if score_total is not None else 0.0
    score_dir = "long" if score >= 20 else "short" if score <= -20 else "none"

    # Event caution always fires first — it overrides nothing, it
    # warns alongside any other setup. Any macro event within 2 days
    # counts (FOMC, PCE, jobs…), not just FOMC.
    event_name = None
    event_days = None
    if next_event is not None and next_event.get("days_away") is not None \
            and next_event["days_away"] <= 2:
        event_name = str(next_event.get("name") or "macro event")
        event_days = int(next_event["days_away"])
    elif fomc_days_away is not None and fomc_days_away <= 2:
        event_name = "FOMC"
        event_days = fomc_days_away
    if event_name is not None and event_days is not None:
        reasons = [f"{event_name} within 2 days: ranges compress before "
                   "the release and break after it — stand down or size "
                   "down."]
        if next_event is not None and next_event.get("impact"):
            reasons.append(str(next_event["impact"]))
        setups.append(_setup(
            f"Event caution — {event_name} in "
            f"{event_days} day{'s' if event_days != 1 else ''}",
            "none", "high", reasons, "caution"))

    # Conflict: technical and institutional score strongly disagree.
    tech_dir = ("long" if signal in ("buy", "strong buy") else
                "short" if signal in ("sell", "strong sell") else "none")
    if tech_dir != "none" and score_dir != "none" and tech_dir != score_dir:
        setups.append(_setup(
            "Conflict — technical vs institutions disagree",
            "none", "high",
            [f"Technical summary says {signal} but the institutional "
             f"score is {score:+.1f} ({score_dir}). No setup until they "
             "agree — trading the disagreement is guessing."],
            "conflict"))
        return setups

    # Trend continuation.
    if signal in ("buy", "strong buy", "sell", "strong sell") \
            and adx_v is not None and adx_v > 25:
        direction = "long" if signal in ("buy", "strong buy") else "short"
        reasons = [f"Technical summary: {signal} "
                   f"({technical.get('buy')} buy / "
                   f"{technical.get('sell')} sell signals).",
                   f"ADX {adx_v:.0f} — market is trending, not ranging."]
        if score_dir == direction:
            reasons.append(f"Institutional score {score:+.1f} agrees "
                           f"({direction}).")
        if cot_pctile is not None:
            reasons.append("COT managed-money net at "
                           f"{cot_pctile:.0f}th percentile (52w).")
        if etf_gld_change is not None and etf_gld_change != 0:
            reasons.append("GLD " + ("inflow" if etf_gld_change > 0
                                     else "outflow") +
                           f" {etf_gld_change:+.2f}t today.")
        confidence = ("high" if score_dir == direction and
                      abs(score) >= 40 else "medium")
        setups.append(_setup(f"Trend continuation ({direction})",
                             direction, confidence, reasons, "trend"))

    # Oversold / overbought bounce.
    if rsi_v is not None and (rsi_v < 30 or rsi_v > 70):
        direction = "long" if rsi_v < 30 else "short"
        if score_dir in ("none", direction):
            setups.append(_setup(
                f"{'Oversold' if rsi_v < 30 else 'Overbought'} bounce "
                f"({direction})",
                direction, "medium",
                [f"RSI(14) {rsi_v:.1f} — "
                 f"{'oversold' if rsi_v < 30 else 'overbought'}.",
                 "Bounce setup only: needs a confirming close back "
                 "inside the range — never catch the falling knife blind."],
                "bounce"))

    # Range fade (Muuk-style): ranging market + price hugging SMA20.
    # Suppressed when the regime filter says TRENDING — fading a range
    # while three methods agree the market is trending is exactly the
    # trap the filter exists to stop. When the filter says CHOPPY,
    # the fade gets a supporting reason instead.
    regime_label = (regime_filter or {}).get("label")
    if adx_v is not None and adx_v < 20 and sma20_v and last_close:
        dist_pct = abs(last_close - sma20_v) / sma20_v * 100.0
        if dist_pct < 1.0 and regime_label != "trending":
            reasons = [f"ADX {adx_v:.0f} — market is ranging, not trending.",
                       f"Price {dist_pct:.2f}% from SMA20 — fade the edge of "
                       "the range back toward the middle, the way Muuk's "
                       "pending-order ranges do."]
            if regime_label == "choppy":
                reasons.append("Regime filter agrees: CHOPPY "
                               "(ER < 0.3, CHOP > 61.8).")
            setups.append(_setup(
                "Range fade (Muuk-style)",
                "long" if last_close < sma20_v else "short", "medium",
                reasons, "range"))

    # Breakout watch: close within 0.5% of the 20-day high/low.
    if daily_bars and len(daily_bars) >= 20 and last_close:
        window = daily_bars[-20:]
        high_20 = max(float(b["high"]) for b in window)
        low_20 = min(float(b["low"]) for b in window)
        if abs(last_close - high_20) / high_20 * 100.0 < 0.5:
            setups.append(_setup(
                "Breakout watch (long)",
                "long", "low",
                [f"Close {last_close:,.2f} is within 0.5% of the 20-day "
                 f"high {high_20:,.2f}. Watch for a close ABOVE it — "
                 "no trade until the break confirms."],
                "breakout"))
        elif abs(last_close - low_20) / low_20 * 100.0 < 0.5:
            setups.append(_setup(
                "Breakout watch (short)",
                "short", "low",
                [f"Close {last_close:,.2f} is within 0.5% of the 20-day "
                 f"low {low_20:,.2f}. Watch for a close BELOW it — "
                 "no trade until the break confirms."],
                "breakout"))

    if not setups:
        setups.append(_setup("No setup right now", "none", "low",
                             ["Technical summary is neutral and no rule "
                              "fired. Waiting is a position."],
                             "waiting"))
    return setups
