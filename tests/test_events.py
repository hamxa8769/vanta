"""Macro event calendar — loading, countdown, and the setups wiring."""

from datetime import date

from goldboard_core.setups import generate_setups
from goldboard_data.events import load_macro_events, next_event


def test_calendar_loads_the_three_named_events():
    events = load_macro_events()
    names = [e["name"] for e in events]
    assert any("PCE" in n for n in names)
    assert any("jobs" in n for n in names)
    assert any("FOMC" in n for n in names)
    dates = [e["date_obj"] for e in events]
    assert dates == sorted(dates)


def test_next_event_countdown_from_known_date():
    event = next_event(today=date(2026, 11, 24))
    assert event is not None
    assert "PCE" in event["name"]
    assert event["days_away"] == 1


def test_no_event_after_the_calendar_ends():
    assert next_event(today=date(2027, 1, 1)) is None


def test_setups_event_caution_names_the_event():
    setups = generate_setups(
        {"complete": True, "signal": "neutral", "buy": 0, "neutral": 5,
         "sell": 0},
        score_total=None, cot_pctile=None, etf_gld_change=None,
        fomc_days_away=None,
        next_event={"name": "US PCE inflation (Oct)", "days_away": 1,
                    "impact": "Hot PCE -> gold pressured."})
    caution = [s for s in setups if s.get("kind") == "caution"]
    assert len(caution) == 1
    assert "PCE" in caution[0]["name"]
    assert any("Hot PCE" in r for r in caution[0]["reasons"])


def test_setups_no_caution_when_no_event_near():
    setups = generate_setups(
        {"complete": True, "signal": "neutral", "buy": 0, "neutral": 5,
         "sell": 0},
        score_total=None, cot_pctile=None, etf_gld_change=None,
        fomc_days_away=30,
        next_event={"name": "US PCE inflation (Oct)", "days_away": 30})
    assert not [s for s in setups if s.get("kind") == "caution"]
