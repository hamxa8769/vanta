"""Pure scoring functions — no I/O, no hardcoded live values.

Inputs are five pillars, each already normalised to -100 (bearish for
gold) .. +100 (bullish for gold), or None when the data is missing.
Missing pillars are excluded and the weights renormalised over the
pillars that are present (recorded in the result, never silently
treated as zero).

Weights (fixed, v0.1.0):
    cot                25%
    central_bank_etf   20%
    macro              25%
    news               15%
    microstructure     15%
"""

from __future__ import annotations

from datetime import datetime, timezone

from .models import ComponentScore, ScoreResult

RULE_VERSION = "v0.1.0"

WEIGHTS: dict[str, float] = {
    "cot": 0.25,
    "central_bank_etf": 0.20,
    "macro": 0.25,
    "news": 0.15,
    "microstructure": 0.15,
}

COMPONENT_ORDER = ["cot", "central_bank_etf", "macro", "news", "microstructure"]

DISPLAY_NAMES = {
    "cot": "COT positioning",
    "central_bank_etf": "Central-bank + ETF flows",
    "macro": "Macro (rates / USD / inflation)",
    "news": "News & sentiment",
    "microstructure": "Microstructure (COMEX / options / physical)",
}


def clamp_score(value: float) -> float:
    """Clamp a score to the -100..100 range."""
    return max(-100.0, min(100.0, float(value)))


def _label(value: float) -> str:
    if value >= 50:
        return "strongly bullish"
    if value >= 20:
        return "bullish"
    if value > -20:
        return "neutral"
    if value > -50:
        return "bearish"
    return "strongly bearish"


def score_features(features: dict[str, float | None]) -> ScoreResult:
    """Score the five pillars into a single -100..100 total.

    ``features`` maps pillar name -> score (-100..100) or None (missing).
    Unknown keys are ignored. Total is the weight-renormalised mean,
    clamped to -100..100.
    """
    now = datetime.now(timezone.utc)
    present: dict[str, float] = {}
    for name in COMPONENT_ORDER:
        value = features.get(name)
        if value is None:
            continue
        present[name] = clamp_score(value)

    if not present:
        return ScoreResult(
            total=0.0,
            components=[],
            reasons=["No component data available — all five pillars missing."],
            as_of=now,
            rule_version=RULE_VERSION,
        )

    weight_sum = sum(WEIGHTS[name] for name in present)
    components: list[ComponentScore] = []
    reasons: list[str] = []
    total = 0.0
    for name, value in present.items():
        effective_weight = WEIGHTS[name] / weight_sum
        total += value * effective_weight
        reason = (
            f"{DISPLAY_NAMES[name]}: {value:+.0f} ({_label(value)}), "
            f"weight {effective_weight:.0%}"
            + (
                f" (renormalised from {WEIGHTS[name]:.0%} — "
                f"{len(COMPONENT_ORDER) - len(present)} pillar(s) missing)"
                if len(present) < len(COMPONENT_ORDER)
                else ""
            )
        )
        components.append(
            ComponentScore(
                name=name,
                score=value,
                weight=effective_weight,
                reason=reason,
                source="features",
                as_of=now,
            )
        )
        reasons.append(reason)

    return ScoreResult(
        total=clamp_score(total),
        components=components,
        reasons=reasons,
        as_of=now,
        rule_version=RULE_VERSION,
    )


# ---------------------------------------------------------------------------
# Raw-snapshot -> -100..100 normalisation (used by api.get_features).
#
# Phase 2 connectors store snapshots in ONE of these documented forms:
#   * series id ending in ``_pctile`` / ``_percentile``: 0..100 percentile
#     -> 2*p - 100
#   * series id ending in ``_z``: z-score -> clamp(z, -3, 3) / 3 * 100
#   * series id ending in ``_score``: already normalised -100..100
#   * series id ending in ``_pct_change`` / ``_change_pct``: % change
#     -> clamp(change * 20)  (a 5% move maps to the full scale)
#   * a series id in EXPLICIT_SERIES below, with its documented formula
#   * anything else: treated as pre-normalised ONLY if already in
#     -100..100, otherwise None (never guess a scale for raw units).
# ---------------------------------------------------------------------------

EXPLICIT_SERIES: dict[str, str] = {
    # COT managed-money net as % of open interest; +50% of OI is the top
    # of the observed range, so net_pct_oi * 2 spans the scale.
    "cot_managed_money_net_pct_oi": "clamp(value * 2)",
    # Central-bank reported net buying, trailing 3 months, tonnes.
    # ~300t/quarter is a very strong quarter (Q3 2026 est. 321t).
    "central_bank_net_tonnes_3m": "clamp(value / 3)",
    # Gold ETF net flow, trailing 13 weeks, tonnes. ~200t is very strong.
    "etf_flow_tonnes_13w": "clamp(value / 2)",
    # 10y real yield (TIPS) change, percentage points, 30d.
    # Falling real yields are gold-supportive, hence the minus sign.
    # A 2pp fall maps to the full scale.
    "real_yield_10y_change_pp_30d": "clamp(-value * 50)",
    # Broad dollar change, %, 30d. A falling dollar is gold-supportive.
    "dxy_change_pct_30d": "clamp(-value * 20)",
    # GDELT gold news tone, -10..+10 -> *10.
    "gdelt_tone_gold": "clamp(value * 10)",
    # COMEX registered share change, percentage points, 30d.
    # A falling registered share = physical tightness (bullish), hence minus.
    "comex_registered_share_change_pp_30d": "clamp(-value * 10)",
}


def _explicit(series_id: str, value: float) -> float:
    formula = EXPLICIT_SERIES[series_id]
    if formula == "clamp(value * 2)":
        return clamp_score(value * 2)
    if formula == "clamp(value / 3)":
        return clamp_score(value / 3)
    if formula == "clamp(value / 2)":
        return clamp_score(value / 2)
    if formula == "clamp(-value * 50)":
        return clamp_score(-value * 50)
    if formula == "clamp(-value * 20)":
        return clamp_score(-value * 20)
    if formula == "clamp(value * 10)":
        return clamp_score(value * 10)
    if formula == "clamp(-value * 10)":
        return clamp_score(-value * 10)
    raise ValueError(f"unhandled explicit series formula: {formula!r}")


def normalise_snapshot(source: str, series_id: str, value: float | None) -> float | None:
    """Map one raw snapshot value to a -100..100 pillar score, or None.

    Returns None when the value cannot be mapped honestly (unknown scale).
    ``source`` is accepted for future per-source rules and recorded in
    callers; the mapping today is driven by ``series_id``.
    """
    if value is None:
        return None
    sid = series_id.strip().lower()
    if sid in EXPLICIT_SERIES:
        return _explicit(sid, float(value))
    if sid.endswith(("_pctile", "_percentile")):
        return clamp_score(2 * float(value) - 100)
    if sid.endswith("_z"):
        return clamp_score(max(-3.0, min(3.0, float(value))) / 3 * 100)
    if sid.endswith("_score"):
        return clamp_score(value)
    if sid.endswith(("_pct_change", "_change_pct")):
        return clamp_score(float(value) * 20)
    # Fallback: already-normalised values pass through; raw values on an
    # unknown scale are refused rather than guessed.
    if -100.0 <= float(value) <= 100.0:
        return float(value)
    return None
