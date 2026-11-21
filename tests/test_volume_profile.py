"""Volume Profile — POC, value area, and the empty-data path."""

from goldboard_core.volume_profile import volume_profile


def _bars(prices, volumes):
    bars = []
    for price, vol in zip(prices, volumes):
        bars.append({"open": price, "high": price + 1, "low": price - 1,
                     "close": price, "volume": vol})
    return bars


def test_poc_is_the_most_traded_price():
    # Most volume sits at 3500; the POC must land there.
    bars = _bars([3400] * 10 + [3500] * 20 + [3600] * 10,
                 [1] * 10 + [10] * 20 + [1] * 10)
    profile = volume_profile(bars, bins=12)
    assert profile["complete"] is True
    assert abs(profile["poc"] - 3500) < 20
    assert profile["total_volume"] == 220


def test_value_area_covers_seventy_percent_of_volume():
    bars = _bars([3400] * 10 + [3500] * 20 + [3600] * 10,
                 [1] * 10 + [10] * 20 + [1] * 10)
    profile = volume_profile(bars, bins=12)
    inside = sum(b["volume"] for b in profile["bins"]
                 if profile["value_area_low"] <= b["mid"]
                 <= profile["value_area_high"])
    assert inside >= 0.70 * profile["total_volume"]
    assert profile["value_area_low"] <= profile["poc"]
    assert profile["value_area_high"] >= profile["poc"]


def test_bars_without_volume_are_incomplete_not_invented():
    bars = _bars([3500] * 20, [0] * 20)
    profile = volume_profile(bars)
    assert profile["complete"] is False
    assert profile["bins"] == []
    assert "volume" in profile["reason"].lower()


def test_too_few_bars_are_incomplete():
    bars = _bars([3500] * 5, [10] * 5)
    profile = volume_profile(bars)
    assert profile["complete"] is False
