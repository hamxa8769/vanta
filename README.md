# Vanta — Gold Institutional Analysis Board

**Vanta** is a standalone Windows desktop app for institutional gold
(XAUUSD) analysis: it tracks groups of institutions from public data
(CFTC COT, central-bank buying via WGC/IMF, gold ETFs GLD/IAU, COMEX
warehouse, LBMA aggregates, CME options open interest, macro data) and
produces advisory scores and signals — in **shadow mode only**. There
is no live trading, no order path, and no real-money gate to bypass.

> **Naming note (v0.1.0):** the product is **Vanta** and the display
> name lives in one constant, `APP_DISPLAY_NAME` in
> `src/goldboard_core/branding.py`. The workspace folder is still
> `gold-board/` and the internal package is still `goldboard_core`, so
> Phase 2 imports do not break. The logo (gold eclipse ring on black)
> is `assets/logo/media-generation-vanta-logo-0-e629295d-eb4a-474a-a1ef-083625bf172a.webp`,
> with the Windows icon `assets/logo/vanta.ico` generated from it.
> The black + gold palette is defined once in `branding.py` (`PALETTE`).

## What this phase (v0.1.0) contains

- Repo skeleton, packaging (`pyproject.toml`, src layout, `pip install -e .`)
- Core library `goldboard_core`: scoring, signals, SQLite storage,
  config, gates, and the stable Muuk-facing API
- Tests (pytest) — all passing; see "Run the tests"
- One clearly-labelled test fixture (`data/fixtures/q3_2026_snapshot.json`)

No UI, no data connectors, no backtest, no CI in this phase — those
are Phase 2+ (see "Not yet built").

## Stack choice — why Python + PySide6, not C++/ImGui here

| Concern | Decision |
|---|---|
| Data connectors (CFTC CSVs, FRED, WGC, sponsor sites, GDELT) | Python wins outright: `requests`, JSON/CSV parsing, fast iteration. Writing scrapers in C++ would be slow and brittle. |
| Buildable/testable here | Python + pytest runs in this Linux VM and in Windows CI with no native toolchain. A C++/ImGui app cannot be built or tested in this VM. |
| Desktop UI later | PySide6 (Qt) gives a production Windows desktop app from the same codebase — one language, one process, native widgets, in-app updater later. |
| Muuk (C++) later | The core is a **separate importable package** with a frozen API (`get_features` / `get_score` / `get_signal`). Muuk will talk to it through a JSON/HTTP wrapper in a later phase — Muuk's code is never touched by this repo. |

## Install & run

```bash
cd gold-board
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e .            # installs the package + runtime deps
pip install -r requirements-dev.txt
python -m pytest -q         # all tests must pass
python -m goldboard_core    # prints version + gate status (read-only check)
```

On first run there is no database yet; the CLI says so and exits
cleanly. Ingestion (Phase 2) creates the DB at the configured path.

## Configuration

Config file: `%APPDATA%/GoldBoard/config.json` on Windows,
`~/.config/goldboard/config.json` on Linux (`$XDG_CONFIG_HOME` respected).
`$GOLD_BOARD_CONFIG_DIR` overrides the directory (used by tests).

```json
{
  "db_path": "",
  "refresh_minutes": {"fred": 1440, "cftc": 10080, "wgc": 43200,
                      "etf": 1440, "comex": 1440, "lbma": 43200,
                      "gdelt": 60, "news": 60},
  "fred_api_key": "",
  "finnhub_api_key": "",
  "paid_sources_enabled": false,
  "data_dir": ""
}
```

API keys: read from the environment first (`GOLD_BOARD_FRED_KEY`,
`GOLD_BOARD_FINNHUB_KEY`), then from the config file. Never hardcoded,
never logged. Keys that came from the environment are never written
back to the config file. `.env`, `*.db` and key material are git-ignored.

## How scoring works (v0.1.0, frozen)

Five pillars, each −100 (bearish for gold) … +100 (bullish), or `None`
when the data is missing:

| Pillar | Weight |
|---|---|
| COT positioning | 25% |
| Central-bank + ETF flows | 20% |
| Macro (rates / USD / inflation) | 25% |
| News & sentiment | 15% |
| Microstructure (COMEX / options / physical) | 15% |

Missing pillars are excluded and weights renormalised over the pillars
present (recorded in the result — never silently treated as zero).
Total = weight-renormalised mean, clamped to −100…+100.

Signal rule: total **> +40** → buy, **< −40** → sell, otherwise none
(exactly ±40 is **no signal**). Entry = price; SL = entry ∓ 1.5×ATR;
TP = entry ± 3.0×ATR (2R). ATR missing/≤0 → ATR = price × 0.01.
Confidence = min(0.95, |total|/100 × 0.9 + 0.05) for a signal, else 0.

Raw snapshots are mapped to −100…+100 by `scoring.normalise_snapshot`
(percentile / z-score / `_score` / `% change` suffixes, plus a small
documented table for known series). A raw value on an unknown scale
maps to `None` — never guessed.

Derived series (v0.6.0): the connectors store raw latest values, but
the score contract asks for derived quantities, so after every
refresh the board computes them from stored snapshot history and
saves them as `source="derived"` snapshots (`is_proxy=True` +
`proxy_formula`): the COT 52-week percentile, the 13-week GLD+IAU
ETF flow, and the 30-day changes in the 10y real yield, the broad
dollar (FRED DTWEXBGS) and the COMEX registered share. Each needs
its history — the COT percentile needs ≥10 weekly values, the ETF
flow needs a snapshot ≥12 weeks old, the 30-day changes need a value
25–35 days old — otherwise it stays `None` and nothing is saved.
What fills immediately on a fresh install: the COT pillar, because
the connector's `cot_gold_mm_net_pct_oi` (managed-money net as % of
OI) is the exact quantity the score's `cot_managed_money_net_pct_oi`
formula describes, so it maps through the same `clamp(value * 2)`.
The other pillars fill in over the following weeks as history
accumulates. The news pillar is untouched: `gdelt_tone_gold` maps
when the news fetch succeeds.

## Stable Muuk-facing API (do not change signatures lightly)

```python
from goldboard_core import get_features, get_score, get_signal

get_features(db_path)            # dict[str, float | None] per pillar
get_score(db_path)               # ScoreResult | None (None = no data)
get_signal(db_path, price, atr)  # Signal | None (None = no data)
```

These three functions are the contract a later JSON/HTTP wrapper will
expose to Muuk C++. Signature changes require a rule-version bump and
a note here.

## Gates G0–G8 (validation before any Muuk integration)

| Gate | Rule |
|---|---|
| G0 | All required sources (fred, cftc, wgc, etf, comex) fetched <24h ago, status ok |
| G1 | ≥ 100 closed shadow trades |
| G2 | ≥ 90 days shadow history |
| G3 | Expectancy > 0.2R |
| G4 | Profit factor ≥ 1.5 |
| G5 | Win rate — reported only, no threshold |
| G6 | Max drawdown < 10R (reported) |
| G7 | Backtest vs shadow alignment — reported only if both exist; absence is **not** a pass |
| G8 | Shadow-only — always passes (`shadow_only=True`) |

`ready_for_muuk` = G0 **and** G1 **and** G2 **and** G3 **and** G4.
Gates never fake a pass: with no data, G0–G4 fail and G7 reports
"not evaluated".

## Honesty rules (from the 2026-11-21 research)

- **Individual firms' SL/TP are NOT public.** This board never claims
  to track them. Its own SL/TP come from the frozen ATR rule above.
- **World Bank holds/trades no gold.** **IMF holds 2,814.1t but does
  not trade it.** Neither is a flow source.
- **GOFO discontinued Jan 2015** — no public gold lease-rate
  benchmark exists. Any lease figure is a futures-carry proxy and must
  be stored with `is_proxy=True` + `proxy_formula`, and labelled
  "proxy" in the UI.
- Every snapshot stores source, vintage, `published_at`/`fetched_at`,
  status and source URL. Revisions are stored as new rows, never
  overwritten in place.

## Versioning

`VERSION` file + `goldboard_core.__version__` + `pyproject.toml` all
carry the same version. Current: **0.7.0**. Every release bumps the
patch version and gets a tag (same standing rule as the other apps).

v0.7.0 is the chart preview + tabs + settings + light theme. The
dashboard chart is now a small ~240px preview: the "⛶ Expand chart"
button — or a click on the chart itself — opens it large in a
centred, modeless window (~1150×720) holding the SAME chart widget
(reparented, never duplicated), with the status line, Retry button
and range buttons travelling with it; closing the window docks the
chart back into the preview. The lower panels are now top tabs
(Institutions, Macro and News, Technical, Signals and Gates, System) —
the summary strip and the chart preview stay above the tabs, always
visible — and the header Panels menu still hides/shows panels inside
their tabs (a tab with no visible panels hides itself). Settings
moved from a separate dialog into the System tab (same form: API keys
with Test buttons + live intervals, with Save / Reset), and the
header Settings button switches to that tab. A light theme joins the
dark one: a "☀ Light" toggle in the header and in the Settings tab,
remembered in QSettings, switching the palette, the TradingView embed
theme, and every custom-painted chart. No data, scoring, or setup
logic changed — display and layout only.

v0.6.0 is the dashboard redesign. A summary strip sits at the top
of the dashboard — always visible, never hideable — with seven KPI
cards: live price + change + source, technical signal (pill) with
buy/sell counts, regime filter (pill) with ER/CHOP, institutional
score with a verdict word and top component, active setups count
with the top setup, next macro event with countdown, and the volume
profile POC with its value area. Every card shows an honest empty
state before its data arrives. The chart panel is now full-width
directly under the strip, and the chart itself is robust: both the
TradingView page and the built-in fallback are 480px tall (the
fallback used to have no minimum height, so a failed TradingView
load collapsed the whole panel and the chart looked invisible),
with a visible status line — "TradingView LIVE" in green, or the
amber fallback note — and a "Retry TradingView" button that reloads
it and restarts the watchdog. Styling is modernised on the same
gold-on-black brand: 12px panel radius, softer borders, 16px
padding, more spacing, bigger key numbers (price 30px), 14px
section titles, and status pills for the regime and the technical
signal (green/amber/red/grey). No data or scoring logic changed —
display only.

v0.5.0 is the Regime Filter — the market's own answer to "trending
or choppy?", built from the spec's three independent methods so no
single indicator decides. The Technical Summary panel now shows
Kaufman's Efficiency Ratio ER(20), the Choppiness Index CHOP(14),
and the ATR(14) percentile over the last 250 daily bars, plus a
4-state regime label: TRENDING (ER > 0.5, CHOP < 38.2, ADX > 25),
CHOPPY (ER < 0.3, CHOP > 61.8), HIGH-RISK (ATR percentile > 90),
UNCLEAR (the methods disagree). The setups engine listens to it:
the Muuk-style range fade is suppressed while the filter says
TRENDING — fading a range in a trending market is the trap the
filter exists to stop — and gets a supporting reason when it says
CHOPPY. Two honest limits, stated in the panel: the spec's spread
rule (spread > 2x median) cannot be measured from Yahoo bars
because they carry no spread, and the news window is covered by the
existing event-caution setup (any macro event within 2 days).
Thresholds are the spec's starting points, NOT optimized.

v0.4.0 is the Live Research Engine + the modern dashboard. The chart
is now the real TradingView candlestick chart (Advanced Real-Time
Chart widget, OANDA:XAUUSD, dark theme, Volume study) inside the
app, with the built-in chart as an automatic fallback when
TradingView cannot load. A Volume Profile panel shows where trading
actually happened (POC + 70% value area, from stored 15-minute
bars — free; true bid/ask footprint needs paid CME tick data and is
never claimed). A macro event calendar panel (PCE / jobs / FOMC with
countdown + expected gold impact) feeds the setups engine, so event
caution now fires for any macro event within 2 days, not just FOMC.
Every panel can be hidden from the header "Panels" menu (choice
remembered); the header logo sits in a visible gold-bordered tile
(the black-on-black logo was invisible before), and the update button
is always visible — "Check for updates", "✓ Up to date", or a gold
"⬆ Update available: vX.Y.Z" — instead of appearing only after a
successful check.

v0.3.0 makes the advisor smarter, with its own algorithms — no
premium key needed. Finnhub's Technical Indicator and Aggregate
Indicator endpoints are both premium-only, so Vanta computes the
same read locally from its stored Yahoo history: RSI(14), MACD,
SMA20/50/200, ADX(14) + trend regime, Bollinger %B, ATR(14), and an
aggregate buy/neutral/sell summary shown in a new Technical Summary
panel. A transparent setups rule engine runs over the technical
summary + the institutional score (trend continuation, oversold /
overbought bounce, Muuk-style range fade, breakout watch, FOMC event
caution, and a conflict rule that says "no setup" when technical and
institutions disagree) — every setup lists its reasons, and nothing
is ever a trade order. The live ticker now uses a multi-source price
aggregator (gold-api.com live spot first, then Yahoo GC=F futures,
metals.live, Stooq daily close) with spot-consensus median and a
divergence flag; the frozen institutional score and gates are
untouched.

v0.2.0 makes the board live: a Settings page (Finnhub/FRED/FedWatch
keys with Test buttons + live intervals), a live price ticker (Yahoo
quote every 10s, short timeout, no retries), a live price chart from
stored OHLC history (2y daily + 5d intraday, fetched from Yahoo),
live news headlines (GDELT ArtList keyless + Finnhub when keyed,
click to open), and a history backtest panel (board signals + a
clearly-labelled price-only replay rule on the daily bars). The data
was always live — v0.2.0 makes the app *look* live too.

v0.1.2 makes the board actually functional: v0.1.1 fetched data but
never stored it, so every panel stayed on "no data yet" — `refresh_all`
now persists every fetched snapshot to the DB. Refresh also runs
connectors in parallel (sequential refresh never finished in 280s
when fredgraph.csv stalled) and the FOMC calendar file is bundled
into the frozen app (it was missing, so the macro panel said
"data/fomc_calendar.json not found"). Live check 2026-11-21: 7/12
sources OK (price, COT, ETF, central-bank, COMEX, LBMA,
microstructure); FedWatch/Finnhub need keys by design, FRED's CSV
fallback stalls from here (a free FRED key uses the fast JSON API),
GDELT rate-limits, USGS supply page parse fails.

## Not yet built (TODO — later phases own these)

- **UI (PySide6 desktop window)** — not in this phase. The black +
  gold palette and the Vanta logo/icon paths are already centralised
  in `branding.py` for it. No stub module exists yet; the UI will be
  added under `src/goldboard_app/` in Phase 2.
- **Data connectors** (FRED, CFTC, WGC, ETF sponsor sites, COMEX,
  LBMA, GDELT, news) — `src/goldboard_core/connectors.py` does not
  exist yet; connectors will write `SourceSnapshot` rows via
  `storage.save_snapshot`. Until then the DB stays empty and every
  gate correctly reports "no data".
- **Backtest engine** — no module yet. When it lands it must store its
  summary in the `meta` table (`backtest_expectancy_r`,
  `backtest_trades`) for gate G7; until then G7 stays unevaluated.
- **CI / GitHub release** — not in this phase; no repo has been created.
- **JSON/HTTP wrapper for Muuk C++** — later phase; it will call only
  the three stable API functions above.

## Layout

```
gold-board/
  pyproject.toml  requirements.txt  requirements-dev.txt  VERSION  README.md
  assets/logo/            # Vanta logo (.webp) + vanta.ico; rejected drafts kept as-is
  data/fixtures/          # test fixture ONLY (Q3 2026, labelled)
  src/goldboard_core/     # models, scoring, signals, config, storage, api, gates, branding
  tests/                  # pytest: scoring, signals, storage, gates, config, fixtures
```

## Phase 3: backtest & shadow (v0.1.0)

- **Backtest engine** (`src/goldboard_core/backtest.py`): pure functions
  over supplied data only. A signal is evaluated at the NEXT bar after
  `generated_at` (no look-ahead); TP first = +2R, SL first = −1R (same-bar
  both-hit takes SL, conservative); neither within the horizon (default
  10 days) = expired at the last in-horizon close. Reports n_trades,
  win rate, expectancy (R), profit factor, max drawdown (R, exit-ordered
  equity curve), and a per-trade-only Sharpe (mean R / population std,
  NOT annualised). `split_backtest` does chronological walk-forward
  folds. `load_cot_price_history` loads a fixture CSV (raises
  `FileNotFoundError` when absent — never substitutes other data).
- **Shadow runner** (`src/goldboard_core/shadow.py`): `ShadowRunner`
  freezes advisory signals into open shadow trades, closes them at an
  exit price (R vs initial risk), expires stale ones (outcome
  `"expired"`, still counted as closed), and marks unrealised R daily
  (stored in meta, never counted as a close). All writes go through
  `storage.py`; the only addition there is the appended
  `close_shadow_trade_expired` helper — no Phase 1/2 signature changed.
- **Gates** (`src/goldboard_core/gates.py`): `evaluate_gates` still reads
  real backtest + shadow data from the DB; G7 tolerance is now 0.3R
  (pass only when both exist and |backtest − shadow expectancy| < 0.3R,
  otherwise an informational fail with the reason). New `gates_summary`
  returns `{ready_for_muuk, overall, evaluated_at, gates: {...}}` for
  the UI. `ready_for_muuk` is still G0–G4 all passing.
- **CLIs**: `python scripts/backtest.py --prices <csv> --out <json>`
  (optional `--db` stores the summary in meta for G7);
  `python scripts/run_shadow.py [--db PATH] [--price P] [--atr A]
  [--no-refresh]` — one daily cycle: refresh connectors, score,
  generate a signal, open it in shadow mode, expire stale trades,
  print gate status. Output always says **SHADOW ONLY**; nothing here
  places an order or moves real money.
- **Fixture**: `data/fixtures/backtest_sample.csv` — hand-made, labelled
  fixture: 70 daily bars + 20 frozen signals with hand-computed
  outcomes (+9R total, expectancy +0.45R, win rate 50%, PF 1.947,
  max drawdown 3.0R). Not real market data.
- **Tests**: `tests/test_backtest.py`, `tests/test_shadow.py`,
  `tests/test_gates_live.py` — 34 new tests (140 total, all passing).

## Phase 4: desktop app (v0.1.0)

The PySide6/Qt desktop app lives in `src/goldboard_app/` (black + gold,
same `PALETTE` as `branding.py`; logo `assets/logo/vanta-logo.png`,
window icon `assets/logo/vanta.ico`).

### Run it

```bash
pip install -e .            # installs PySide6 + the package
python -m goldboard_app     # launches the Vanta window
```

One screen, ten panels: gold price (with source + timestamp), total
score gauge (−100…+100) with per-pillar breakdown, COT managed-money
chart (custom-painted, no matplotlib), ETF flow (GLD/IAU), central-bank
buying, macro (real yield, USD index, Fed funds, next FOMC), news &
sentiment (GDELT tone; Finnhub shows "key required" until
`GOLD_BOARD_FINNHUB_KEY` is set), advisory signals + history, gates
G0–G8 with a "NOT ready for Muuk integration" banner while any of
G0–G4 fails, and per-source freshness badges (ok / stale / failed).
Refresh button + auto-refresh (default 15 min, changeable in the
header); refresh runs in a QThread and never blocks the UI. Errors go
to the status bar and the Logs drawer. Signal alerts: when alerts are
on, a new directional signal or a score crossing ±40 logs an ALERT and
shows a tray notification. Shadow mode only — no live trading.

Every number shows its source + timestamp; a failed source shows
FAILED/STALE, never an invented value.

### Build the Windows app

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1
# → dist\Vanta\Vanta.exe + Vanta-<version>-Windows.zip (+ .sha256)
```

Or by hand: `pyinstaller --noconfirm vanta.spec` (see `vanta.spec`).

### In-app updater

The header shows an **Update available** button when a newer release
exists on GitHub (`hamxa8769/vanta`). It downloads the Windows zip,
verifies its SHA-256 sidecar when present, then installs via
`update.bat` (extract over the install dir, restart the app). Config
lives in `%APPDATA%/GoldBoard`, outside the install dir, so it is
preserved. The repo does not exist yet, so the updater currently
reports "no release channel yet" and does nothing — that is expected,
not an error.

### CI

`.github/workflows/ci.yml`: tests on `windows-latest` + `ubuntu-latest`
(Python 3.12, headless Qt), then a Windows PyInstaller build job that
uploads the app folder + zip as an artifact. No release is created by
CI — releases are made by hand, like the other apps.

### Screenshot (dev only)

`python scripts/screenshot.py` launches the window offscreen against a
throwaway DB seeded from the labelled test fixture
(`data/fixtures/q3_2026_snapshot.json`, demo data for the screenshot
only) and saves `docs/screenshot-dashboard.png`.

### Tests

`tests/test_ui.py` (headless, `QT_QPA_PLATFORM=offscreen`: window
builds, gates banner, worker emits on a mocked registry, empty DB
shows "no data" instead of fake values) and `tests/test_updater.py`
(version compare, release picking, SHA-256 verify, update script).
