"""Tests for scoring: weights, renormalisation on missing data, clamping."""

from goldboard_core.scoring import WEIGHTS, score_features


def test_all_present_weighted_mean():
    result = score_features(
        {"cot": 100, "central_bank_etf": 0, "macro": 0, "news": 0, "microstructure": 0}
    )
    assert result.total == 25.0  # 100 * 0.25
    assert len(result.components) == 5
    assert abs(sum(c.weight for c in result.components) - 1.0) < 1e-9


def test_weights_exact():
    assert WEIGHTS == {
        "cot": 0.25,
        "central_bank_etf": 0.20,
        "macro": 0.25,
        "news": 0.15,
        "microstructure": 0.15,
    }
    assert abs(sum(WEIGHTS.values()) - 1.0) < 1e-9


def test_missing_pillars_renormalise():
    # Only cot (+80) and macro (-40) present: weights 0.25/0.25 -> 50/50.
    result = score_features({"cot": 80, "central_bank_etf": None, "macro": -40,
                             "news": None, "microstructure": None})
    assert result.total == 20.0  # (80 - 40) / 2
    weights = {c.name: c.weight for c in result.components}
    assert weights == {"cot": 0.5, "macro": 0.5}
    assert any("renormalised" in r for r in result.reasons)


def test_single_pillar_gets_full_weight():
    result = score_features({"cot": 60})
    assert result.total == 60.0
    assert result.components[0].weight == 1.0


def test_all_missing_returns_zero_with_reason():
    result = score_features(
        {"cot": None, "central_bank_etf": None, "macro": None,
         "news": None, "microstructure": None}
    )
    assert result.total == 0.0
    assert result.components == []
    assert result.reasons


def test_empty_dict_returns_zero():
    assert score_features({}).total == 0.0


def test_inputs_clamped_to_range():
    result = score_features({"cot": 500, "macro": -500})
    assert result.total == 0.0  # (+100 - 100) / 2
    assert all(-100 <= c.score <= 100 for c in result.components)


def test_total_clamped_and_rule_version():
    result = score_features({"cot": 100, "macro": 100})
    assert result.total == 100.0
    assert result.rule_version == "v0.1.0"


def test_per_component_reasons_present():
    result = score_features({"cot": 42, "news": -10})
    assert len(result.reasons) == 2
    assert all("42" in r or "-10" in r for r in result.reasons)
    assert all(c.reason for c in result.components)


def test_unknown_keys_ignored():
    result = score_features({"cot": 40, "not_a_pillar": 100})
    assert result.total == 40.0
    assert len(result.components) == 1
