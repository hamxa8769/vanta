"""Frozen e2e check for v0.3.0 (NOT a pytest test).

Launches the frozen dist/Vanta/Vanta binary headless (offscreen)
with an isolated HOME for ~180s, then inspects the DB it wrote:
price bars (daily + intraday), snapshots per source, and recomputes
the technical summary + setups from the stored real bars to prove
the new v0.3.0 panels have real data behind them.

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
    dbs = list(home.rglob("*.db")) + list(home.rglob("*.sqlite*"))
    if not dbs:
        print("NO DATABASE FOUND under isolated HOME")
        return 1
    db = dbs[0]
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
    setups = generate_setups(tech, None, None, None, None, daily)
    print(f"setups ({len(setups)}):")
    for s in setups:
        print(f"  - {s['name']} [{s['direction']}/{s['confidence']}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
