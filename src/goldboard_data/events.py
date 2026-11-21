"""Macro event calendar — the Live Research Engine's diary.

Loads the manually-maintained ``data/macro_events.json`` (PCE, jobs,
FOMC…) the same way the FOMC calendar is loaded: bundled with the
app, updated by hand from the official sources. The board never
invents an event date — a missing or unreadable file yields an
empty list and the panels say so.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

_EVENTS_FILENAME = "macro_events.json"


def _calendar_paths() -> list[Path]:
    here = Path(__file__).resolve()
    paths = []
    for parent in [here.parent, *here.parents]:
        paths.append(parent / "data" / _EVENTS_FILENAME)
        paths.append(parent / _EVENTS_FILENAME)
    # Frozen app: PyInstaller _MEIPASS.
    import sys
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        paths.append(Path(meipass) / "data" / _EVENTS_FILENAME)
    return paths


def load_macro_events() -> list[dict]:
    """All events from the bundled calendar, sorted by date."""
    for path in _calendar_paths():
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return []
            events = data.get("events", []) if isinstance(data, dict) else []
            out = []
            for event in events:
                try:
                    day = datetime.strptime(
                        str(event["date"]), "%Y-%m-%d").date()
                except (KeyError, ValueError, TypeError):
                    continue
                out.append({**event, "date_obj": day})
            return sorted(out, key=lambda e: e["date_obj"])
    return []


def next_event(today: date | None = None) -> dict | None:
    """The next upcoming event (today counts), or None."""
    today = today or datetime.now(timezone.utc).date()
    for event in load_macro_events():
        if event["date_obj"] >= today:
            return {**event, "days_away":
                    (event["date_obj"] - today).days}
    return None


def upcoming_events(days: int = 30,
                    today: date | None = None) -> list[dict]:
    """Events within the next ``days`` days, with days_away each."""
    today = today or datetime.now(timezone.utc).date()
    out = []
    for event in load_macro_events():
        delta = (event["date_obj"] - today).days
        if 0 <= delta <= days:
            out.append({**event, "days_away": delta})
    return out
