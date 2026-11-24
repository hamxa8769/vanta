"""Frozen e2e check for v0.8.0 (NOT a pytest test).

Launches the frozen dist/Vanta/Vanta binary headless (offscreen)
with an isolated HOME for ~180s, then inspects the DB it wrote:
price bars (daily + intraday), snapshots per source, and recomputes
the technical summary + setups + volume profile from the stored
real bars to prove the new v0.8.0 panels have real data behind them.

Usage: python scripts/e2e_frozen.py [seconds]
"""

from __future__ import annotations

import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / "dist" / "Vanta" / "Vanta"


def main() -> int:
    seconds = int(sys.argv[1]) if len(sys.argv) > 1 else 180
    home = Path(tempfile.mkdtemp(prefix="vanta-e2e-"))
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["XDG_CONFIG_HOME"] = str(home / ".config")
    # Chromium refuses to start as root without this (test-only flag;
    # Windows users never hit it).
    env.setdefault("QTWEBENGINE_CHROMIUM_FLAGS",
                   "--no-sandbox --disable-gpu --ignore-certificate-errors")
    print(f"HOME={home}  running frozen Vanta for {seconds}s ...")
    proc = subprocess.Popen([str(BIN)], env=env,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    time.sleep(seconds)
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    print(f"exit code: {proc.returncode}")
    dbs = [p for p in home.rglob("*.db") if ".pki" not in p.parts]
    if not dbs:
        print("NO DATABASE FOUND under isolated HOME")
        return 1
    # Chromium (QtWebEngine) writes its own .db files too — pick the
    # one that actually holds Vanta's price_history table.
    db = None
    for candidate in dbs:
        try:
            probe = sqlite3.connect(str(candidate))
            tables = {r[0] for r in probe.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            probe.close()
        except sqlite3.Error:
            continue
        if "price_history" in tables:
            db = candidate
            break
    if db is None:
        print(f"NO Vanta DB among {len(dbs)} .db files: "
              + ", ".join(str(p.relative_to(home)) for p in dbs))
        return 1
    print(f"DB: {db}")
    con = sqlite3.connect(str(db))
    tables = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    print(f"tables: {sorted(tables)}")
    if "price_history" in tables:
        for interval in ("1d", "15m"):
            n = con.execute(
                "SELECT COUNT(*) FROM price_history WHERE interval=?",
                (interval,)).fetchone()[0]
            print(f"price_history {interval}: {n} bars")
    if "snapshots" in tables:
        rows = con.execute(
            "SELECT source, COUNT(*) FROM snapshots GROUP BY source"
            " ORDER BY source").fetchall()
        total = sum(n for _, n in rows)
        print(f"snapshots total: {total}")
        for source, n in rows:
            print(f"  {source}: {n}")
    con.close()
    # Recompute the v0.3.0 summary from the stored real bars.
    sys.path.insert(0, str(ROOT / "src"))
    from goldboard_core.storage import get_price_bars
    from goldboard_core.technical import technical_summary
    from goldboard_core.setups import generate_setups
    from goldboard_core.volume_profile import volume_profile
    from goldboard_data.events import next_event, upcoming_events
    daily = get_price_bars(str(db), "1d", limit=600)
    intra = get_price_bars(str(db), "15m", limit=500)
    tech = technical_summary(daily, intra)
    print("technical summary:", {
        "complete": tech["complete"], "signal": tech["signal"],
        "buy": tech["buy"], "neutral": tech["neutral"],
        "sell": tech["sell"], "regime": tech["regime"],
        "rsi14": (round(tech["rsi14"], 1) if tech["rsi14"] is not None
                  else None),
        "adx": (round(tech["adx"], 1) if tech["adx"] is not None else None),
    })
    rf = tech.get("regime_filter") or {}
    print("regime filter:", {
        "label": rf.get("label"),
        "er": (round(rf["er"], 2) if rf.get("er") is not None else None),
        "chop": (round(rf["chop"], 1) if rf.get("chop") is not None else None),
        "atr_percentile": (
            round(rf["atr_percentile"], 1)
            if rf.get("atr_percentile") is not None else None),
        "reasons": rf.get("reasons", []),
    })
    nxt = next_event()
    setups = generate_setups(tech, None, None, None, None, daily, nxt,
                             tech.get("regime_filter"))
    print(f"setups ({len(setups)}):")
    for s in setups:
        print(f"  - {s['name']} [{s['direction']}/{s['confidence']}]")
    profile = volume_profile(intra)
    print("volume profile:", {
        "complete": profile["complete"],
        "poc": (round(profile["poc"], 2) if profile.get("poc") else None),
        "value_area": ((round(profile["value_area_low"], 2),
                        round(profile["value_area_high"], 2))
                       if profile.get("complete") else None),
        "reason": profile.get("reason") or "",
    })
    events = upcoming_events(days=60)
    print(f"events (next 60d: {len(events)}):")
    for e in events:
        print(f"  - {e['name']} in {e['days_away']}d ({e['date']})")
    # Institutional score (v0.6.0): the frozen app's worker runs the
    # derived step after each refresh, so the score should now fill
    # from the COT alias on a fresh DB; other pillars need history.
    from goldboard_core.api import get_features, get_score
    features = get_features(str(db))
    score = get_score(str(db))
    print("features:", {k: (round(v, 1) if v is not None else None)
                        for k, v in features.items()})
    if score is not None:
        print("score:", {
            "total": round(score.total, 1),
            "components": [(c.name, round(c.score, 1))
                           for c in score.components],
        })
    else:
        print("score: None — no pillar has data yet")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
