"""Tests for signal rules: thresholds, boundaries, SL/TP math, confidence."""

import pytest

from goldboard_core.models import ScoreResult
from goldboard_core.signals import generate_signal


def _score(total: float) -> ScoreResult:
    return ScoreResult(total=total, components=[], reasons=[], rule_version="v0.1.0")


def test_buy_above_threshold():
    sig = generate_signal(_score(41), price=4000.0, atr=20.0)
    assert sig.direction == "buy"
    assert sig.entry == 4000.0
    assert sig.sl == pytest.approx(4000.0 - 1.5 * 20.0)  # 3970
    assert sig.tp == pytest.approx(4000.0 + 3.0 * 20.0)  # 4060
    assert sig.tp - sig.entry == pytest.approx(2 * (sig.entry - sig.sl))  # 2R


def test_sell_below_threshold():
    sig = generate_signal(_score(-41), price=4000.0, atr=20.0)
    assert sig.direction == "sell"
    assert sig.sl == pytest.approx(4000.0 + 1.5 * 20.0)  # 4030
    assert sig.tp == pytest.approx(4000.0 - 3.0 * 20.0)  # 3940
    assert sig.entry - sig.tp == pytest.approx(2 * (sig.sl - sig.entry))


def test_boundary_exactly_plus_40_is_no_signal():
    sig = generate_signal(_score(40), price=4000.0, atr=20.0)
    assert sig.direction == "none"
    assert sig.sl is None and sig.tp is None
    assert sig.confidence == 0.0


def test_boundary_exactly_minus_40_is_no_signal():
    sig = generate_signal(_score(-40), price=4000.0, atr=20.0)
    assert sig.direction == "none"
    assert sig.confidence == 0.0


def test_zero_score_is_no_signal():
    assert generate_signal(_score(0), 4000.0, 20.0).direction == "none"


def test_atr_missing_uses_one_percent_of_price():
    sig = generate_signal(_score(80), price=4000.0, atr=None)
    # ATR fallback = 40; SL = 4000 - 60, TP = 4000 + 120
    assert sig.sl == pytest.approx(3940.0)
    assert sig.tp == pytest.approx(4120.0)


def test_atr_zero_or_negative_uses_fallback():
    for bad_atr in (0.0, -5.0):
        sig = generate_signal(_score(80), price=2000.0, atr=bad_atr)
        assert sig.sl == pytest.approx(2000.0 - 1.5 * 20.0)


def test_confidence_formula():
    # min(0.95, |total|/100 * 0.9 + 0.05)
    sig = generate_signal(_score(100), price=4000.0, atr=20.0)
    assert sig.confidence == pytest.approx(0.95)  # capped
    sig = generate_signal(_score(50), price=4000.0, atr=20.0)
    assert sig.confidence == pytest.approx(0.50)
    sig = generate_signal(_score(-60), price=4000.0, atr=20.0)
    assert sig.confidence == pytest.approx(0.59)


def test_reason_cites_score_and_components():
    from goldboard_core.models import ComponentScore
    score = ScoreResult(
        total=55,
        components=[ComponentScore(name="cot", score=80, weight=0.5, reason="x")],
        reasons=["x"],
    )
    sig = generate_signal(score, price=4000.0, atr=20.0)
    assert "55" in sig.reason
    assert "cot" in sig.reason


def test_invalid_price_rejected():
    with pytest.raises(ValueError):
        generate_signal(_score(80), price=0, atr=20.0)
    with pytest.raises(ValueError):
        generate_signal(_score(80), price=-100, atr=20.0)


def test_rule_version_and_horizon():
    sig = generate_signal(_score(90), price=4000.0, atr=10.0)
    assert sig.rule_version == "v0.1.0"
    assert sig.horizon_days == 10
    assert sig.status == "open"
