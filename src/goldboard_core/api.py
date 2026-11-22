"""Stable Muuk-facing interface — keep these signatures stable.

``get_features`` / ``get_score`` / ``get_signal`` are the contract a
later JSON/HTTP wrapper (for Muuk C++) will call. Do not change their
signatures without bumping the rule version and noting it in README.

Data flow: latest snapshot per pillar (from the DB) -> normalised to
-100..100 via scoring.normalise_snapshot -> score_features -> signal.
A pillar with no usable snapshot returns None and is excluded from
the score (weights renormalised, see scoring.py).
"""

from __future__ import annotations

from pathlib import Path

from .models import ScoreResult, Signal
from .scoring import normalise_snapshot, score_features
from .signals import generate_signal
from .storage import (
    get_latest_snapshot_by_series,
    save_score,
    save_signal,
)

# Series ids tried per pillar, in priority order. Phase 2 connectors
# should store one of these (or any ``*_score`` / ``*_pctile`` / ``*_z``
# series — see scoring.normalise_snapshot for the full mapping rules).
COMPONENT_SERIES: dict[str, list[str]] = {
    "cot": [
        "cot_managed_money_net_pctile_52w",
        "cot_managed_money_net_pct_oi",
        "cot_gold_mm_net_pct_oi",
        "cot_score",
    ],
    "central_bank_etf": [
        "central_bank_etf_score",
        "central_bank_net_tonnes_3m",
        "etf_flow_tonnes_13w",
        "etf_flow_score",
    ],
    "macro": [
        "macro_score",
        "real_yield_10y_change_pp_30d",
        "dxy_change_pct_30d",
    ],
    "news": [
        "news_score",
        "gdelt_tone_gold",
        "news_sentiment_score",
    ],
    "microstructure": [
        "microstructure_score",
        "comex_registered_share_change_pp_30d",
        "comex_score",
        "options_score",
    ],
}


def get_features(db_path: str | Path) -> dict[str, float | None]:
    """Latest snapshot per pillar, mapped to -100..100 (None if no data)."""
    features: dict[str, float | None] = {}
    for pillar, series_ids in COMPONENT_SERIES.items():
        value: float | None = None
        for series_id in series_ids:
            snap = get_latest_snapshot_by_series(db_path, series_id)
            if snap is None or snap.status == "failed":
                continue
            mapped = normalise_snapshot(snap.source, snap.series_id, snap.value)
            if mapped is not None:
                value = mapped
                break
        features[pillar] = value
    return features


def get_score(db_path: str | Path) -> ScoreResult | None:
    """Score the latest features; saves the result. None if no data at all."""
    features = get_features(db_path)
    if all(v is None for v in features.values()):
        return None
    score = score_features(features)
    save_score(db_path, score)
    return score


def get_signal(
    db_path: str | Path, price: float, atr: float | None = None
) -> Signal | None:
    """Generate (and save) the current advisory signal. None if no data."""
    score = get_score(db_path)
    if score is None:
        return None
    signal = generate_signal(score, price, atr)
    save_signal(db_path, signal)
    return signal
