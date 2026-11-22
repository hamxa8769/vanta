"""SQLite storage (stdlib sqlite3 only). No invented data.

Tables: snapshots, scores, signals, shadow_trades, gates, events, meta,
price_history (real OHLC bars fetched from Yahoo — the chart and the
history backtest read these, never invented prices), news (real
headlines fetched from GDELT ArtList / Finnhub — titles + URLs only).
All datetimes are stored as ISO-8601 strings; statuses, source URLs and
proxy flags are stored with every snapshot so nothing is ever presented
without provenance.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .models import (
    ComponentScore,
    GateResult,
    ScoreResult,
    ShadowTrade,
    Signal,
    SourceSnapshot,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    series_id TEXT NOT NULL,
    value REAL NOT NULL,
    unit TEXT NOT NULL DEFAULT '',
    as_of TEXT,
    published_at TEXT,
    fetched_at TEXT,
    status TEXT NOT NULL DEFAULT 'ok',
    source_url TEXT NOT NULL DEFAULT '',
    is_proxy INTEGER NOT NULL DEFAULT 0,
    proxy_formula TEXT,
    vintage TEXT
);
CREATE INDEX IF NOT EXISTS idx_snapshots_source_series
    ON snapshots (source, series_id, fetched_at);

CREATE TABLE IF NOT EXISTS scores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    total REAL NOT NULL,
    components TEXT NOT NULL DEFAULT '[]',
    reasons TEXT NOT NULL DEFAULT '[]',
    as_of TEXT,
    rule_version TEXT NOT NULL DEFAULT 'v0.1.0'
);

CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    direction TEXT NOT NULL,
    entry REAL NOT NULL,
    sl REAL,
    tp REAL,
    confidence REAL NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    generated_at TEXT,
    rule_version TEXT NOT NULL DEFAULT 'v0.1.0',
    horizon_days INTEGER NOT NULL DEFAULT 10,
    status TEXT NOT NULL DEFAULT 'open'
);

CREATE TABLE IF NOT EXISTS shadow_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id INTEGER,
    direction TEXT NOT NULL,
    entry REAL NOT NULL,
    sl REAL,
    tp REAL,
    generated_at TEXT,
    rule_version TEXT NOT NULL DEFAULT 'v0.1.0',
    horizon_days INTEGER NOT NULL DEFAULT 10,
    status TEXT NOT NULL DEFAULT 'open',
    outcome TEXT,
    exit_price REAL,
    closed_at TEXT,
    r_multiple REAL,
    reason TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS gates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    passed INTEGER NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    threshold TEXT,
    evaluated_at TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL,
    event_time TEXT,
    actual REAL,
    consensus REAL,
    prior REAL,
    surprise_z REAL,
    source_url TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS price_history (
    ts TEXT NOT NULL,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    volume REAL,
    interval TEXT NOT NULL DEFAULT '1d',
    fetched_at TEXT,
    PRIMARY KEY (ts, interval)
);
CREATE INDEX IF NOT EXISTS idx_price_history_interval_ts
    ON price_history (interval, ts);

CREATE TABLE IF NOT EXISTS news (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    url TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    published_at TEXT,
    fetched_at TEXT,
    provider TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_news_published
    ON news (published_at DESC);
"""


def _connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: str | Path) -> Path:
    """Create the database file and all tables (idempotent)."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _connect(path) as conn:
        conn.executescript(_SCHEMA)
    return path


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


# ---------------------------------------------------------------- snapshots

def save_snapshot(db_path: str | Path, snap: SourceSnapshot) -> int:
    """Insert one snapshot; returns its row id."""
    init_db(db_path)
    with _connect(db_path) as conn:
        cur = conn.execute(
            """INSERT INTO snapshots
               (source, series_id, value, unit, as_of, published_at, fetched_at,
                status, source_url, is_proxy, proxy_formula, vintage)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                snap.source, snap.series_id, snap.value, snap.unit,
                _iso(snap.as_of), _iso(snap.published_at), _iso(snap.fetched_at),
                snap.status, snap.source_url, 1 if snap.is_proxy else 0,
                snap.proxy_formula, snap.vintage,
            ),
        )
        return int(cur.lastrowid)


def _row_to_snapshot(row: sqlite3.Row) -> SourceSnapshot:
    return SourceSnapshot(
        source=row["source"],
        series_id=row["series_id"],
        value=float(row["value"]),
        unit=row["unit"] or "",
        as_of=_parse(row["as_of"]),
        published_at=_parse(row["published_at"]),
        fetched_at=_parse(row["fetched_at"]),
        status=row["status"] or "ok",
        source_url=row["source_url"] or "",
        is_proxy=bool(row["is_proxy"]),
        proxy_formula=row["proxy_formula"],
        vintage=row["vintage"],
    )


def get_latest_snapshot(db_path: str | Path, source: str, series_id: str) -> SourceSnapshot | None:
    """Latest snapshot for one (source, series_id), by fetched_at."""
    if not Path(db_path).is_file():
        return None
    with _connect(db_path) as conn:
        row = conn.execute(
            """SELECT * FROM snapshots WHERE source = ? AND series_id = ?
               ORDER BY fetched_at DESC, id DESC LIMIT 1""",
            (source, series_id),
        ).fetchone()
    return _row_to_snapshot(row) if row else None


def get_latest_snapshot_by_series(db_path: str | Path, series_id: str) -> SourceSnapshot | None:
    """Latest snapshot for a series_id across all sources."""
    if not Path(db_path).is_file():
        return None
    with _connect(db_path) as conn:
        row = conn.execute(
            """SELECT * FROM snapshots WHERE series_id = ?
               ORDER BY fetched_at DESC, id DESC LIMIT 1""",
            (series_id,),
        ).fetchone()
    return _row_to_snapshot(row) if row else None


def get_latest_snapshot_for_source(db_path: str | Path, source: str) -> SourceSnapshot | None:
    """Latest snapshot from one source (any series), by fetched_at."""
    if not Path(db_path).is_file():
        return None
    with _connect(db_path) as conn:
        row = conn.execute(
            """SELECT * FROM snapshots WHERE source = ?
               ORDER BY fetched_at DESC, id DESC LIMIT 1""",
            (source,),
        ).fetchone()
    return _row_to_snapshot(row) if row else None


def get_snapshot_history(db_path: str | Path, series_id: str,
                         limit: int = 100) -> list[tuple[datetime | None, float]]:
    """(date, value) history for one series, oldest first.

    Failed snapshots are excluded (they are not data). The date is
    the snapshot's ``as_of`` when set, else its ``fetched_at`` —
    connectors store the observation date in ``as_of``, so 30-day /
    13-week deltas are computed on data dates, not fetch times.
    Used by the derived-series step (goldboard_core.derived).
    """
    if not Path(db_path).is_file():
        return []
    with _connect(db_path) as conn:
        rows = conn.execute(
            """SELECT as_of, fetched_at, value FROM snapshots
               WHERE series_id = ? AND status != 'failed'
               ORDER BY fetched_at DESC, id DESC LIMIT ?""",
            (series_id, limit),
        ).fetchall()
    history: list[tuple[datetime | None, float]] = []
    for row in rows:
        date = _parse(row["as_of"]) or _parse(row["fetched_at"])
        history.append((date, float(row["value"])))
    history.reverse()
    return history


# -------------------------------------------------------------------- scores

def save_score(db_path: str | Path, score: ScoreResult) -> int:
    init_db(db_path)
    components = [
        {
            "name": c.name, "score": c.score, "weight": c.weight,
            "reason": c.reason, "source": c.source,
            "as_of": _iso(c.as_of),
        }
        for c in score.components
    ]
    with _connect(db_path) as conn:
        cur = conn.execute(
            """INSERT INTO scores (total, components, reasons, as_of, rule_version)
               VALUES (?, ?, ?, ?, ?)""",
            (
                score.total, json.dumps(components), json.dumps(score.reasons),
                _iso(score.as_of), score.rule_version,
            ),
        )
        return int(cur.lastrowid)


def get_latest_score(db_path: str | Path) -> ScoreResult | None:
    if not Path(db_path).is_file():
        return None
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM scores ORDER BY id DESC LIMIT 1"
        ).fetchone()
    if not row:
        return None
    components = [
        ComponentScore(
            name=c["name"], score=float(c["score"]), weight=float(c["weight"]),
            reason=c["reason"], source=c.get("source", ""),
            as_of=_parse(c.get("as_of")),
        )
        for c in json.loads(row["components"])
    ]
    return ScoreResult(
        total=float(row["total"]),
        components=components,
        reasons=list(json.loads(row["reasons"])),
        as_of=_parse(row["as_of"]),
        rule_version=row["rule_version"],
    )


# ------------------------------------------------------------------- signals

def save_signal(db_path: str | Path, signal: Signal) -> int:
    init_db(db_path)
    with _connect(db_path) as conn:
        cur = conn.execute(
            """INSERT INTO signals
               (direction, entry, sl, tp, confidence, reason, generated_at,
                rule_version, horizon_days, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                signal.direction, signal.entry, signal.sl, signal.tp,
                signal.confidence, signal.reason, _iso(signal.generated_at),
                signal.rule_version, signal.horizon_days, signal.status,
            ),
        )
        return int(cur.lastrowid)


def _row_to_signal(row: sqlite3.Row) -> Signal:
    return Signal(
        direction=row["direction"],
        entry=float(row["entry"]),
        sl=None if row["sl"] is None else float(row["sl"]),
        tp=None if row["tp"] is None else float(row["tp"]),
        confidence=float(row["confidence"]),
        reason=row["reason"] or "",
        generated_at=_parse(row["generated_at"]),
        rule_version=row["rule_version"],
        horizon_days=int(row["horizon_days"]),
        status=row["status"],
    )


def list_signals(
    db_path: str | Path, limit: int = 100, status: str | None = None
) -> list[Signal]:
    if not Path(db_path).is_file():
        return []
    query = "SELECT * FROM signals"
    params: list = []
    if status is not None:
        query += " WHERE status = ?"
        params.append(status)
    query += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with _connect(db_path) as conn:
        rows = conn.execute(query, params).fetchall()
    return [_row_to_signal(r) for r in rows]


# -------------------------------------------------------------- shadow trades

def log_shadow_trade(db_path: str | Path, trade: ShadowTrade) -> int:
    init_db(db_path)
    with _connect(db_path) as conn:
        cur = conn.execute(
            """INSERT INTO shadow_trades
               (signal_id, direction, entry, sl, tp, generated_at, rule_version,
                horizon_days, status, outcome, exit_price, closed_at,
                r_multiple, reason)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                trade.signal_id, trade.direction, trade.entry, trade.sl, trade.tp,
                _iso(trade.generated_at), trade.rule_version, trade.horizon_days,
                trade.status, trade.outcome, trade.exit_price,
                _iso(trade.closed_at), trade.r_multiple, trade.reason,
            ),
        )
        return int(cur.lastrowid)


def _row_to_shadow_trade(row: sqlite3.Row) -> ShadowTrade:
    return ShadowTrade(
        direction=row["direction"],
        entry=float(row["entry"]),
        sl=None if row["sl"] is None else float(row["sl"]),
        tp=None if row["tp"] is None else float(row["tp"]),
        generated_at=_parse(row["generated_at"]),
        rule_version=row["rule_version"],
        horizon_days=int(row["horizon_days"]),
        status=row["status"],
        outcome=row["outcome"],
        exit_price=None if row["exit_price"] is None else float(row["exit_price"]),
        closed_at=_parse(row["closed_at"]),
        r_multiple=None if row["r_multiple"] is None else float(row["r_multiple"]),
        reason=row["reason"] or "",
        signal_id=row["signal_id"],
        id=int(row["id"]),
    )


def get_shadow_trade(db_path: str | Path, trade_id: int) -> ShadowTrade | None:
    if not Path(db_path).is_file():
        return None
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM shadow_trades WHERE id = ?", (trade_id,)
        ).fetchone()
    return _row_to_shadow_trade(row) if row else None


def close_shadow_trade(
    db_path: str | Path,
    trade_id: int,
    exit_price: float,
    closed_at: datetime | None = None,
) -> ShadowTrade | None:
    """Close an open shadow trade; R is measured against initial risk.

    R = (exit - entry) / (entry - SL) for buys,
        (entry - exit) / (SL - entry) for sells.
    Outcome: win (R > 0), loss (R < 0), breakeven (R == 0).
    Returns the updated trade, or None if the id does not exist.
    """
    trade = get_shadow_trade(db_path, trade_id)
    if trade is None:
        return None
    if trade.status != "open":
        return trade
    risk = None
    if trade.direction == "buy" and trade.sl is not None:
        risk = trade.entry - trade.sl
        r_multiple = (exit_price - trade.entry) / risk if risk else None
    elif trade.direction == "sell" and trade.sl is not None:
        risk = trade.sl - trade.entry
        r_multiple = (trade.entry - exit_price) / risk if risk else None
    else:
        r_multiple = None
    if r_multiple is None:
        outcome = "breakeven"
    elif r_multiple > 0:
        outcome = "win"
    elif r_multiple < 0:
        outcome = "loss"
    else:
        outcome = "breakeven"
    closed = closed_at or datetime.now()
    with _connect(db_path) as conn:
        conn.execute(
            """UPDATE shadow_trades
               SET status = 'closed', outcome = ?, exit_price = ?,
                   closed_at = ?, r_multiple = ?
               WHERE id = ?""",
            (outcome, float(exit_price), _iso(closed), r_multiple, trade_id),
        )
    return get_shadow_trade(db_path, trade_id)


def list_shadow_trades(
    db_path: str | Path, status: str | None = None
) -> list[ShadowTrade]:
    if not Path(db_path).is_file():
        return []
    query = "SELECT * FROM shadow_trades"
    params: list = []
    if status is not None:
        query += " WHERE status = ?"
        params.append(status)
    query += " ORDER BY id ASC"
    with _connect(db_path) as conn:
        rows = conn.execute(query, params).fetchall()
    return [_row_to_shadow_trade(r) for r in rows]


def close_shadow_trade_expired(
    db_path: str | Path,
    trade_id: int,
    exit_price: float,
    closed_at: datetime | None = None,
    reason: str = "",
) -> ShadowTrade | None:
    """Close an open shadow trade as EXPIRED (Phase 3 helper).

    Same R math as :func:`close_shadow_trade`, but the outcome is
    recorded as ``"expired"`` (no TP/SL hit within the horizon) so
    expiries stay distinguishable from wins/losses while still
    counting as closed trades for the gates. The trade's stored
    reason is replaced with ``reason`` when one is given.
    Returns the updated trade, or None if the id does not exist.
    """
    trade = get_shadow_trade(db_path, trade_id)
    if trade is None:
        return None
    if trade.status != "open":
        return trade
    risk = None
    if trade.direction == "buy" and trade.sl is not None:
        risk = trade.entry - trade.sl
        r_multiple = (exit_price - trade.entry) / risk if risk else None
    elif trade.direction == "sell" and trade.sl is not None:
        risk = trade.sl - trade.entry
        r_multiple = (trade.entry - exit_price) / risk if risk else None
    else:
        r_multiple = None
    closed = closed_at or datetime.now()
    stored_reason = reason if reason else trade.reason
    with _connect(db_path) as conn:
        conn.execute(
            """UPDATE shadow_trades
               SET status = 'closed', outcome = 'expired', exit_price = ?,
                   closed_at = ?, r_multiple = ?, reason = ?
               WHERE id = ?""",
            (float(exit_price), _iso(closed), r_multiple, stored_reason, trade_id),
        )
    return get_shadow_trade(db_path, trade_id)


# --------------------------------------------------------------------- gates

def save_gate(db_path: str | Path, gate: GateResult, evaluated_at: datetime | None = None) -> int:
    init_db(db_path)
    with _connect(db_path) as conn:
        cur = conn.execute(
            """INSERT INTO gates (name, passed, detail, threshold, evaluated_at)
               VALUES (?, ?, ?, ?, ?)""",
            (
                gate.name, 1 if gate.passed else 0, gate.detail, gate.threshold,
                _iso(evaluated_at or datetime.now()),
            ),
        )
        return int(cur.lastrowid)


def get_gate_status(db_path: str | Path) -> list[GateResult]:
    """Latest stored evaluation per gate name (empty list if none stored)."""
    if not Path(db_path).is_file():
        return []
    with _connect(db_path) as conn:
        rows = conn.execute(
            """SELECT g.* FROM gates g
               INNER JOIN (
                   SELECT name, MAX(id) AS max_id FROM gates GROUP BY name
               ) latest ON g.id = latest.max_id
               ORDER BY g.name ASC"""
        ).fetchall()
    return [
        GateResult(
            name=r["name"],
            passed=bool(r["passed"]),
            detail=r["detail"] or "",
            threshold=r["threshold"],
        )
        for r in rows
    ]


# ---------------------------------------------------------------------- meta

def set_meta(db_path: str | Path, key: str, value: str) -> None:
    init_db(db_path)
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def get_meta(db_path: str | Path, key: str) -> str | None:
    if not Path(db_path).is_file():
        return None
    with _connect(db_path) as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


# ------------------------------------------------------------ price history

def save_price_bars(db_path: str | Path, bars: list[dict]) -> int:
    """Upsert OHLC bars (by ts + interval); returns rows written.

    ``bars``: dicts with ts (ISO str or datetime), open/high/low/close,
    optional volume, interval ('1d' or '15m'). Bars come only from the
    Yahoo chart API (see goldboard_data.price) — never invented.
    """
    if not bars:
        return 0
    init_db(db_path)
    fetched_at = datetime.now(timezone.utc).isoformat()
    rows = []
    for bar in bars:
        ts = bar.get("ts")
        ts_text = ts.isoformat() if isinstance(ts, datetime) else str(ts)
        rows.append((
            ts_text,
            float(bar["open"]), float(bar["high"]),
            float(bar["low"]), float(bar["close"]),
            None if bar.get("volume") is None else float(bar["volume"]),
            str(bar.get("interval", "1d")),
            fetched_at,
        ))
    with _connect(db_path) as conn:
        conn.executemany(
            """INSERT INTO price_history
               (ts, open, high, low, close, volume, interval, fetched_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(ts, interval) DO UPDATE SET
                 open = excluded.open, high = excluded.high,
                 low = excluded.low, close = excluded.close,
                 volume = excluded.volume, fetched_at = excluded.fetched_at""",
            rows,
        )
    return len(rows)


def get_price_bars(
    db_path: str | Path, interval: str = "1d", limit: int = 600
) -> list[dict]:
    """Oldest→newest OHLC bars for one interval (empty list if none)."""
    if not Path(db_path).is_file():
        return []
    with _connect(db_path) as conn:
        rows = conn.execute(
            """SELECT ts, open, high, low, close, volume, interval
               FROM price_history WHERE interval = ?
               ORDER BY ts ASC LIMIT ?""",
            (interval, limit),
        ).fetchall()
    return [
        {
            "ts": _parse(r["ts"]),
            "open": float(r["open"]),
            "high": float(r["high"]),
            "low": float(r["low"]),
            "close": float(r["close"]),
            "volume": None if r["volume"] is None else float(r["volume"]),
            "interval": r["interval"],
        }
        for r in rows
    ]


# --------------------------------------------------------------------- news

def save_news(db_path: str | Path, items: list[dict]) -> int:
    """Insert headlines (dedupe by title+url); returns rows written."""
    if not items:
        return 0
    init_db(db_path)
    written = 0
    with _connect(db_path) as conn:
        for item in items:
            title = str(item.get("title") or "").strip()
            url = str(item.get("url") or "").strip()
            if not title:
                continue
            published = item.get("published_at")
            published_text = (
                published.isoformat() if isinstance(published, datetime)
                else (str(published) if published else None))
            cur = conn.execute(
                """INSERT INTO news
                   (title, url, source, published_at, fetched_at, provider)
                   SELECT ?, ?, ?, ?, ?, ?
                   WHERE NOT EXISTS (
                       SELECT 1 FROM news WHERE title = ? AND url = ?)""",
                (
                    title, url, str(item.get("source") or ""),
                    published_text, _iso(datetime.now(timezone.utc)),
                    str(item.get("provider") or ""),
                    title, url,
                ),
            )
            written += cur.rowcount
    return written


def get_latest_news(db_path: str | Path, limit: int = 30) -> list[dict]:
    """Newest-first headlines (empty list if none stored)."""
    if not Path(db_path).is_file():
        return []
    with _connect(db_path) as conn:
        rows = conn.execute(
            """SELECT title, url, source, published_at, provider
               FROM news ORDER BY
                 COALESCE(published_at, fetched_at) DESC, id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return [
        {
            "title": r["title"],
            "url": r["url"] or "",
            "source": r["source"] or "",
            "published_at": _parse(r["published_at"]),
            "provider": r["provider"] or "",
        }
        for r in rows
    ]
