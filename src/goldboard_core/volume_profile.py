"""Volume Profile — where the trading actually happened.

Built free from the stored intraday bars (15m, 5d): every bar's
volume is dropped into a price bin, so the profile shows which
prices attracted the most trading. No tick data is needed for this
version — true footprint (bid vs ask volume per price) needs CME
tick data, which is paid, and this module never pretends otherwise.

  * POC (point of control): the price bin with the most volume —
    the market's "fair value" for the session window.
  * Value area: the smallest set of contiguous bins around the POC
    covering 70% of total volume — where 70% of trading happened.
  * High/low volume nodes: bins at the extremes of the profile.

Pure function over OHLCV bars (oldest first). Bars without volume
are skipped; if none carry volume, the profile is empty with a
reason instead of invented numbers.
"""

from __future__ import annotations


def volume_profile(bars: list[dict], bins: int = 24) -> dict:
    """Build the volume profile from OHLCV bars.

    Returns {bins, poc, value_area_high, value_area_low,
    total_volume, complete, reason}. Each bin: {price_low,
    price_high, mid, volume}. ``complete`` is False when there is
    not enough volume data to draw a profile.
    """
    usable = [b for b in bars
              if b.get("volume") is not None and float(b["volume"]) > 0]
    out: dict = {"bins": [], "poc": None, "value_area_high": None,
                 "value_area_low": None, "total_volume": 0.0,
                 "complete": False, "reason": ""}
    if len(usable) < 10:
        out["reason"] = ("Not enough bars with volume yet — refresh to "
                         "fetch intraday history.")
        return out
    lows = [float(b["low"]) for b in usable]
    highs = [float(b["high"]) for b in usable]
    lo, hi = min(lows), max(highs)
    if hi <= lo:
        out["reason"] = "Price range is zero — cannot build a profile."
        return out
    width = (hi - lo) / bins
    volumes = [0.0] * bins
    total = 0.0
    for bar in usable:
        vol = float(bar["volume"])
        # A bar's volume sits at its close price (the traded price).
        idx = min(bins - 1, max(0, int((float(bar["close"]) - lo) / width)))
        volumes[idx] += vol
        total += vol
    if total <= 0:
        out["reason"] = "No volume in the stored bars."
        return out
    profile_bins = []
    for i, vol in enumerate(volumes):
        profile_bins.append({
            "price_low": lo + i * width,
            "price_high": lo + (i + 1) * width,
            "mid": lo + (i + 0.5) * width,
            "volume": vol,
        })
    poc_idx = max(range(bins), key=lambda i: volumes[i])
    # Value area: grow outwards from the POC until 70% of volume.
    target = total * 0.70
    covered = volumes[poc_idx]
    top = bottom = poc_idx
    while covered < target and (top < bins - 1 or bottom > 0):
        up = volumes[top + 1] if top < bins - 1 else -1.0
        down = volumes[bottom - 1] if bottom > 0 else -1.0
        if up >= down:
            top += 1
            covered += volumes[top]
        else:
            bottom -= 1
            covered += volumes[bottom]
    out.update({
        "bins": profile_bins,
        "poc": profile_bins[poc_idx]["mid"],
        "poc_volume": volumes[poc_idx],
        "value_area_high": profile_bins[top]["price_high"],
        "value_area_low": profile_bins[bottom]["price_low"],
        "total_volume": total,
        "complete": True,
        "reason": "",
    })
    return out
