# ua-ovdp-plugin

Claude Code plugin (in progress) for working with Ukrainian government bonds (ОВДП).
Wraps a Playwright-based scraper of `ovdp.in.ua` + Приват24 + Inzhur as an MCP server so
Claude can query the bond market, compute real yields, and track price history over time.

## Current state

A working, installable Claude Code plugin, pushed to
[GrishinSergey/ua-ovdp-plugin](https://github.com/GrishinSergey/ua-ovdp-plugin) (`master`):

- `.claude-plugin/plugin.json` — plugin manifest (name `ua-ovdp-plugin`). No `version` field
  by design — see Known issues.
- `.claude-plugin/marketplace.json` — makes this repo installable as its own single-plugin
  marketplace (`source: "./"`, same repo). Without this file the plugin is NOT installable
  from GitHub at all — `claude plugin install` requires a marketplace-registered plugin.
- `.mcp.json` — wires the `ovdp-bonds` MCP server to `server.py`, launched via
  `uv run --project ${CLAUDE_PLUGIN_ROOT} python server.py` — `uv` creates its own venv and
  installs every dependency from `pyproject.toml`/`uv.lock` automatically on first launch.
  The only external prerequisite is `uv` itself being installed. Verified end-to-end from a
  simulated fresh machine (no `.venv`, no cached chromium build) via a real MCP client
  session — see Known issues for the one thing this doesn't cover.
- `scraper.py` — standalone Playwright scraper → writes a `bonds.json`-shaped snapshot.
- `server.py` — FastMCP server, 17 tools: market data/scraping (8, original), position
  tracking + forecasting + portfolio construction (9, backed by `engine/`).
- `engine/` — analytics/portfolio-construction engine (position tracking, bond comparison, 3
  portfolio-building strategies), wired into `server.py` via `engine/services/market_bridge.py`
  — see [Analytics engine (engine/)](#analytics-engine-engine).
- `skills/` — empty, scaffolded for future skills (see `skills/README.md`). None written yet.
- `pyproject.toml` / `uv.lock` — package metadata + locked dependency versions (committed,
  for reproducible installs across machines).

What does **not** exist yet:

- No actual skills, agents, or hooks — just the MCP server. `skills/` will grow as more
  workflows get built on top of the 17 raw tools.
- No tests.

The `plugin-dev@claude-plugins-official` plugin is installed at project scope
(`.claude/settings.json`) and was used to determine this structure (manifest layout,
`.mcp.json` conventions, `${CLAUDE_PLUGIN_ROOT}` usage). Run `/reload-plugins` after
installing/updating it mid-session to pick it up without restarting.

## Architecture

```
scraper.py  (Playwright, async)          server.py  (FastMCP, stdio)
──────────────────────────────           ─────────────────────────────
next.privat24.ua/bonds/list  ──┐         run_scraper()      → subprocess → scraper.py
inzhur.reit/offer/ovdp       ──┼─ universe = ISINs(P24) ∪    → writes market_history/<ts>.json
                                │  ISINs(Inzhur)
ovdp.in.ua/bonds/<ISIN>      ──┤  (per-ISIN metadata,        list_bonds() / get_bond() /
                                │   full schedule)            get_warnings()   → read latest
ovdp.in.ua/prices            ──┘  (Sense/Універі enrichment)  (or given) snapshot, filter/
                                                               shape server-side
                reconcile()                                  compute_ytm()    → recomputed
                 → bonds.json (v5)                            XIRR from real cashflows,
                 → warnings[]                                 not the broker's number

                                                               market_info() / list_snapshots()
                                                               / yield_history() → snapshot dir
                                                               bookkeeping + per-ISIN trend
```

### Core invariant (scraper.py)

**Source of truth = the broker** (can you actually buy it), not `ovdp.in.ua`. `ovdp.in.ua` is
treated purely as metadata (schedule / name / yields), fetched per ISIN. A bond with no broker
offer is not emitted at all, even if ovdp knows about it.

- `universe = ISINs(Privat24) ∪ ISINs(Inzhur)` — the buyable set.
- Canonical schedule/coupon/maturity = one copy per ISIN: ovdp's full schedule when present,
  else the broker's future-only schedule.
- Price is **never averaged** — it's per-broker in `where_to_buy[]`.
- Displayed broker yields are kept for reference but are NOT authoritative; `server.py`'s
  `compute_ytm` recomputes XIRR from actual cashflows + price instead.
- Cross-source disagreement (currency/maturity/coupon mismatches, stale broker payments not in
  the ovdp schedule, broker-only bonds with no ovdp metadata) → `output["warnings"]`, surfaced
  via `get_warnings()`. Of these, currency/maturity/coupon mismatches specifically mean a
  bond's OWN computed numbers (price, YTM, NKD) would be actively wrong, not just
  incomplete — `list_bonds`/`compare_bonds`/`build_target_portfolio` now exclude those bonds
  by **default** (`server.py`'s `_categorize_warnings()`), surfacing the reason inline as
  `data_warnings` (`{isin, message, status: "прибрано"|"неприбрано"}`) instead of requiring a
  separate `get_warnings()` call before ranking. Broker-only/staleness warnings stay
  informational only — the bond isn't excluded for those.
- **Per-broker price fields (v5, additive):** `where_to_buy[]` entries now also carry
  `dirty_price` (explicit alias of `price`, unchanged), `nominal_price` (clean, i.e.
  `dirty_price` minus accrued interest), and `nkd_price` (accrued interest), computed at
  scrape time from the bond's own coupon schedule (`scraper.py`'s `compute_price_breakdown()`
  — deliberately duplicates `math_core.accrued_interest()`'s formula rather than importing
  `engine/`, to keep the scraper dependency-free of it; see that function's docstring).
  `nominal_price`/`nkd_price` are `null` for a bond with no past coupon yet. **Old snapshot
  files predate these fields** — every reader must `.get()` them, never assume presence.
  These fields are informational/display-only: nothing in `engine/` reads them —
  `compare_bonds`/`position_metrics`/etc. always re-derive clean/dirty fresh at the actual
  settlement date in use via `math_core.entry_price()`, deliberately, not as a stopgap:
  a stored `nominal_price` is only valid exactly at `snapshot_taken_at`, every real call
  uses a *different* settlement date (today, a backdated purchase, a future horizon), and
  `entry_price()` is pure Decimal arithmetic (no I/O) — there's no staleness risk or
  performance reason to prefer a cached field over recomputing, and trusting two
  independently-updatable numbers (a stored one + a freshly computed one) instead of
  deriving one from the other is exactly the kind of divergence that caused the
  double-NKD bug below in the first place. No `commission_price` field yet — deferred
  pending an independent reference/fair-value price, see Known limitations.

### Runtime data directories

Both directories below are **host-project-local, not shipped with the plugin** — they live
under `CLAUDE_PROJECT_DIR` (guaranteed set by Claude Code in any stdio MCP server's env,
whether plugin-provided or project-level `.mcp.json`; falls back to CWD for standalone runs).
Any future skill/tool that needs market data or logs should resolve paths the same way
(`OVDP_MARKET_DIR`/`OVDP_LOG_PATH` env override → `CLAUDE_PROJECT_DIR`-relative default), not
invent a new convention.

- **`market_history/`** (default; override with `OVDP_MARKET_DIR`) — one JSON snapshot per
  `run_scraper()` call, named `<YYYYMMDDHHMMSS>.json` (e.g. `20260706142200.json`) — sorts
  correctly as a plain string. "Latest" is still resolved by *parsing* the timestamp
  (`_parse_market_ts` / `_snapshot_time`) rather than trusting name sort, as a defensive
  measure against any malformed or manually-dropped files.
- **`data/`** (default; `scraper.log` path overridable via `OVDP_LOG_PATH`) — full DEBUG log
  from the scraper subprocess. Never surfaced to Claude directly (see `run_scraper` below).
- `list_bonds`/`get_bond`/`compute_ytm` return `snapshot_file` + `snapshot_taken_at` (just the
  filename and its parsed timestamp — never the file contents) so Claude can judge freshness
  from the response itself, without a separate `market_info()` round-trip. This was a
  deliberate choice over making `data_path` a required parameter: an optional param with
  auto-latest-resolution is the standard MCP pattern (sensible default + escape hatch), and a
  mandatory explicit path everywhere would double round-trips without anything actually
  enforcing that Claude always passes the freshest one anyway.
- `server.py` must **never** `print()` to stdout — it talks to Claude Code over stdio and that
  would corrupt the MCP protocol stream. Diagnostics go to stderr / the log file only.

## MCP tool surface (server.py)

| Tool | Purpose |
|---|---|
| `run_scraper(...)` | Run the scraper subprocess, write a new timestamped snapshot, return a compact summary (never the raw log). |
| `list_bonds(...)` | Filter/sort bonds server-side (currency, military flag, broker, yield range, maturity range) so the model doesn't pull the whole dataset into context. Response includes `snapshot_file`/`snapshot_taken_at` + `data_warnings` (bonds excluded by default for a currency/maturity/coupon mismatch, and why). |
| `get_bond(isin)` | Full record for one ISIN, plus related warnings. Response includes `snapshot_file`/`snapshot_taken_at`. |
| `get_warnings()` | Data-quality warnings from the last scrape. |
| `compute_ytm(isin, ...)` | Recompute annualized yield (XIRR via bisection) from real cashflows + purchase price — the "honest" number vs. the broker-displayed one. Response includes `snapshot_file`/`snapshot_taken_at`. |
| `market_info()` | Orientation call: market dir, snapshot count, latest snapshot summary. Call this first each session. |
| `list_snapshots()` | All snapshots oldest→newest, for picking an explicit `data_path`. |
| `yield_history(isin)` | How a bond's yield/price moved across all snapshots — for trend charts. |
| `add_position(isin, purchase_date, quantity, purchase_price_dirty, ...)` | Record a bond you actually own (today or backdated, as long as it's still active/resolvable). Freezes the bond's schedule/maturity/face value into the position at this point; only price is ever re-resolved live afterward. Auto-computes `accrued_interest_paid` if omitted. |
| `list_positions(label=None)` | List recorded positions, optionally filtered by label. |
| `update_position(position_id, ...)` | Correct fields on a recorded position (quantity/price/fee/broker/label). Not isin/purchase_date — remove + re-add instead. |
| `remove_position(position_id)` | Delete a recorded position. |
| `position_metrics(position_id, as_of=None, ...)` | Full P&L/YTM/duration/convexity for one owned position. Realized cashflows are auto-derived from the frozen schedule (dates between purchase and `as_of` assumed paid) — see engine/ section below. |
| `portfolio_metrics(as_of=None, label=None, ...)` | Aggregate across all (or `label`-filtered) positions: totals, avg YTM/duration, currency/maturity breakdowns, monthly cashflow forecast. |
| `simulate_reinvestment(reinvest_rate=0.15, years=3, ...)` | "What if I reinvest every coupon at X%" projection over the current portfolio. Returns both `effective_annual_return` (with the reinvestment assumption) and `baseline_effective_annual_return` (`reinvest_rate=0`) side by side, so the portfolio's own quality isn't conflated with the reinvestment-rate assumption. |
| `compare_bonds(isins, ...)` | Rank 2+ candidate bonds (not yet owned) by effective annual return over a shared horizon. Candidates with a currency/maturity/coupon mismatch are excluded by default before ranking (see `data_warnings` in the response and the Core invariant section above). |
| `build_target_portfolio(target_income, horizon_days, ...)` | Build a portfolio hitting a target income — 3 modes: `max_efficiency` (greedy, fewest bonds), `monthly_income` (even monthly coverage), `max_efficiency_reinvest` (month-by-month reinvestment simulation). Same default candidate exclusion + `data_warnings` as `compare_bonds`. |

## Analytics engine (`engine/`)

A separate, considerably richer engine dropped into the project as `backend/`, renamed to
`engine/`, and now wired into `server.py`'s tool surface (the 9 rows above) via
`engine/services/market_bridge.py`. It was originally extracted from a real FastAPI backend
(root import package there was `app`) — imports fixed to `engine.*`, `__init__.py` added to
every subpackage, a minimal `engine/settings.py` shim created, REIT-specific code removed
(OVDP-only scope; Inzhur itself is untouched as an OVDP broker source).

### Layout

| Path | What |
|---|---|
| `engine/investment/domain/models.py` | Core dataclasses: `Bond`, `Position`, `Portfolio`, `CouponPayment`, `ReceivedPayment`, `BrokerPrice`. Foundation for everything else. |
| `engine/investment/forecast/finance.py` | Position/portfolio-level math: accrued interest, YTM (via `scipy.optimize.brentq`), simple yield, modified duration/convexity/DV01, full `PositionMetrics`/`PortfolioMetrics` (P&L, breakdowns, monthly cashflow forecast), reinvestment scenario simulation. |
| `engine/investment/forecast/forecast_core.py` | `compare_bonds()` — ranks 2–10 candidate bonds over a horizon by effective annual return. |
| `engine/investment/forecast/math_core.py` | Lower-level primitives (`bond_horizon_result`, `entry_price`, `coupons_in_horizon`, `real_income`) — the `efficiency = real_income / dirty_price` metric the portfolio engine is built on. **Also the single canonical dirty↔clean/НКД conversion** (`entry_price()`/`accrued_interest()`) — `forecast_core.py` and `finance.py` both delegate to it now instead of their own arithmetic (see Known limitations: double-NKD-counting bug). Any new code needing "price without accrued interest" should call `entry_price()`, not reimplement the formula. |
| `engine/investment/forecast/strategy.py` | `StandardStrategy` vs `Privat24Strategy` — different conventions for "how much did I actually make," to match broker UIs. |
| `engine/investment/portfolio/engine.py` | `build_portfolio(EngineRequest)` — dispatches to 3 modes: `max_efficiency` (optimizer.py, greedy), `monthly_income` (monthly_allocator.py, even monthly coverage), `max_efficiency_reinvest` (simulator.py, month-by-month reinvestment simulation with event timeline). |
| `engine/schemas/*.py` | Pydantic HTTP-shaped contracts (`BondInput`/`BondResponse`, `EngineRequest`/`EngineResponse`, `PositionCreate/Response`, `ReceivedPaymentCreate/Response`) — a parallel, unused-by-MCP path for a hypothetical future HTTP API. `server.py`'s tools bypass this entirely (see `market_bridge.py`). |
| `engine/services/analytics_service.py` | `compute_analytics(HttpEngineRequest)` — converts the *HTTP* schema into domain `Bond` objects and calls `build_portfolio()`. Not used by any MCP tool (which go through `market_bridge.py` instead); kept for whatever originally called it. |
| `engine/services/portfolio_service.py` | JSON-file CRUD for owned positions (`get_positions`/`add_position`/`update_position`/`delete_position`), backed by `engine/settings.py`'s `portfolio_data_path`. Used directly by `server.py`'s position tools. |
| `engine/services/market_bridge.py` | **The real bridge.** `bond_from_snapshot()` converts one scraper bonds.json record straight into a domain `Bond` (bypassing `BondInput`/`analytics_service` entirely — field names don't line up and `coupon_rate` doesn't exist in scraper output, so it derives `coupon_rate` from `coupon_amount * coupon_frequency / face_value`). `freeze_bond()`/`bond_from_frozen()` round-trip a `Bond`'s contractual facts into/out of a position record. `position_from_record()` rebuilds a domain `Position` from a stored record, auto-deriving realized cashflows from the frozen schedule (see below) instead of reading a payment log. |
| `engine/serialize.py` | `to_jsonable()` — shared dataclass/Decimal/date → JSON conversion, used by `market_bridge`-adjacent server.py tools and both services above (previously three separate copies of the same function). |

### Realized income: derived from schedule, not logged

`position_metrics`/`portfolio_metrics` need to know which coupons/maturity have *actually*
been paid vs. are still projected. Rather than requiring a `record_payment`-style tool (a
manual log nobody would keep up to date), `market_bridge.position_from_record()` treats every
schedule entry between `purchase_date` and `as_of` as received at its exact scheduled amount.
Deliberate simplification specific to OVDP: state-guaranteed, coupons don't realistically
deviate from schedule. `ReceivedPayment`/`ReceivedPaymentCreate` still exist in the domain
model/schemas if a real discrepancy ever needs logging instead — nothing currently writes to
`Position.received_payments` from storage, it's always synthesized at read time.

### Known limitations

- **REIT tracking was intentionally removed.** The original FastAPI backend covered both
  OVDP and a separate Inzhur REIT product (purchases/dividends/tax/summary) — all
  REIT-specific schemas and service functions have been stripped from `engine/` since this
  plugin is OVDP-only. **Inzhur itself is untouched and remains valid** — it's one of the two
  brokers `scraper.py` scrapes for OVDP bonds; only the unrelated REIT-investment-tracking
  feature (a different product from the same broker) was removed.
- **`engine/settings.py` is a minimal shim**, not a reconstruction of the original app's
  settings — it provides only the one field actually referenced (`portfolio_data_path`),
  resolved `CLAUDE_PROJECT_DIR`-relative like everything else in this project (env override
  `OVDP_PORTFOLIO_DATA_PATH`). `engine/services/snapshot_service.py` (the single-file
  `market_data.json` service that conflicted with `market_history/`) was deleted — superseded
  by `market_bridge.py`, which reads `market_history/` the same way `server.py` already does.
- ~~`compare_bonds`'s `broker` parameter is accepted but not actually honored~~ **Fixed.**
  `forecast_core.compare_bonds()` now excludes (with a warning) any candidate the requested
  broker doesn't actually offer, then prices the rest via `Bond.price_for_broker(broker)` —
  matching how `math_core.entry_price()` (used by `build_target_portfolio`) already worked.
  Deliberately excludes rather than falls back to a different broker's price for a missing
  bond: silently mixing broker-X price for one candidate with a fallback price for another
  would make a "compare as broker X" result misleadingly plausible rather than merely
  imprecise. Verified against real data: broker-filtered runs now produce different prices
  per broker and correctly drop candidates that specific broker doesn't carry.
  (`server.py`'s own `compare_bonds` tool docstring had independently drifted stale — it
  still claimed broker filtering wasn't honored — and has now been corrected to match.)
- ~~`forecast_core._calculate_bond_forecast()` (used by `compare_bonds`) and
  `finance.calculate_position_metrics()` (used by `position_metrics`/`portfolio_metrics`/
  `simulate_reinvestment`) both double-counted НКД~~ **Fixed.** Both independently
  mislabeled `bond.last_market_price`/`Bond.price_for_broker()` — which are always DIRTY
  price per `domain/models.py`'s own contract — as "clean" and then added accrued interest
  a second time, inflating the computed entry/market price and skewing every downstream
  number built on it: `dirty_entry_price`/`actual_invested`/`ytm_to_maturity` in
  `compare_bonds` (the `ytm_to_maturity` field was corrupted on *every* call, not just
  hold-to-maturity ones — occasionally producing absurd negative YTMs on longer horizons),
  and `market_value`/`unrealized_pnl`/`current_ytm` in `position_metrics`. Root cause was a
  code bug, not schema ambiguity — `math_core.py`'s own `entry_price()`/`accrued_interest()`
  already did this correctly and were just never called from these two modules. Both now
  delegate to `math_core.entry_price()` instead of reimplementing the conversion (see that
  module's docstring for the canonical explanation + call-site list). Verified against real
  snapshot data: `dirty_entry_price`/`market_value` now match the raw scraped broker price
  exactly (previously inflated by ~1x accrued interest).
- `analytics_service._to_domain_bond` hardcodes `face_value=Decimal("1000")`, ignoring
  `BondInput.face_value` entirely. Harmless in practice (OVDP face value is always 1000 UAH)
  but means that schema field does nothing today. (Moot for anything MCP-driven — that path
  goes through `market_bridge.bond_from_snapshot`, not `_to_domain_bond`.)
- `compute_ytm` in `server.py` (its own bisection solver, works off raw bonds.json without
  needing a recorded position) and `engine.investment.forecast.finance.calculate_ytm`
  (`scipy.brentq`, used internally by `position_metrics`/`compare_bonds`/etc.) are two
  separate implementations of the same math, kept deliberately: different call sites
  (quick hypothetical lookup vs. actual position/comparison math), not true duplication
  anymore now that both are in active use.
- `pyproject.toml` has no package config for `engine/` (no `[build-system]`/packages list) —
  not needed today: `server.py` runs as a plain script, and Python auto-adds its own directory
  to `sys.path`, so `engine/` is importable as a sibling package for free (verified: `engine.*`
  imports cleanly alongside `server.py` under the exact `sys.path` mechanics Claude Code uses
  to launch the plugin). Revisit only if `engine/` ever needs importing from somewhere not
  already on that path (e.g. a real `pip install`).
- Minor: `engine/investment/portfolio/engine.py` means there's a module literally named
  `engine.py` living inside the top-level `engine/` package. Not a functional problem (fully
  qualified paths are unambiguous), just easy to misread in isolation.

## Environment variables (server.py)

- `OVDP_SCRAPER_PATH` — path to `scraper.py` (default: bundled copy next to `server.py`).
- `OVDP_PYTHON` — interpreter to run the scraper with (default: same interpreter running the
  server — needs Playwright installed).
- `OVDP_MARKET_DIR` — snapshot directory (default: `<project>/market_history`).
- `OVDP_LOG_PATH` — full scraper DEBUG log file (default: `<project>/data/scraper.log`).
- `OVDP_SUBPROCESS_TIMEOUT_SEC` — hard cap on a scraper run, seconds (default: 900).

## Dev environment

- Requires `uv` (the only real prerequisite — see README's "Requirements") and Python ≥3.10
  (uv fetches a matching interpreter itself if none is on PATH). Deps live in
  `pyproject.toml`/`uv.lock` (Playwright, BeautifulSoup4, loguru, pydantic(-settings), scipy,
  `mcp>=1.28.0`).
- Run the scraper directly: `uv run python scraper.py [--log-level DEBUG] [--p24-schedules] [--inzhur-dump]`.
- Run the MCP server standalone: `uv run python server.py` (or via the `ua-ovdp-mcp` console
  script, once that entry point actually exists — see Known issues).
- `.venv/` is now a disposable, `uv`-managed artifact (gitignored) — don't hand-maintain it;
  delete and let `uv run` recreate it if it ever gets into a weird state.

## Known issues

- **`scraper.py` still silently drops all console logging by default** — same symptom as
  before but the cause has moved: the string-truthiness crash (`if not args.no_console_logs:`,
  always `False` for either `"true"`/`"false"`) is gone, replaced with
  `if args.no_console_logs == "true":`, but the flag's *sense* is inverted for what its name
  suggests — passing `--no-console-logs true` (i.e. asking to suppress console output) is what
  currently ADDS the stderr handler, and the default `"false"` leaves it un-added, so a normal
  run still produces no console output. Only the file log (`--log-file`) gets anything either
  way. Not blocking, but worth a one-line fix (`args.no_console_logs != "true"`, or rename the
  flag) when next touching that code.
- **`plugin.json` intentionally has no `version` field.** Per Claude Code's docs: if a
  version is set (in `plugin.json` or the marketplace entry), users only get updates when
  that string is bumped — pushing commits alone does nothing. Left unset on purpose while
  this is under active development, so `claude plugin update` picks up every new commit on
  `master` automatically. Add a real semver version once this stabilizes into actual releases.
- `pyproject.toml` has no `[project.scripts]` entry, so the `ua-ovdp-mcp` console script that
  `server.py`'s docstring references doesn't actually exist yet. `.mcp.json` sidesteps this by
  invoking `server.py` directly via `uv run`.
- `inzhur_provider(..., dump=True)` writes `inzhur_dump.html/.txt` to CWD, not under `data/`
  (now gitignored either way).

## Next steps (tracked outside this file)

`engine/` is now fully wired into `server.py` (17 tools total) and verified end-to-end with
real scraped data — position CRUD, position/portfolio metrics, reinvestment simulation, bond
comparison, and target-portfolio construction all confirmed working against a real
`market_history` snapshot. No skills exist yet — that's the natural next layer, wrapping
common multi-tool workflows (e.g. "record this purchase" or "what should I buy for $X/month"
end to end) on top of the 17 raw tools. Use the `plugin-dev` plugin's `skill-development`
skill for authoring conventions, and its `plugin-validator`/`skill-reviewer` agents to check
quality before considering something done. The still-open engine/ quirk
(`analytics_service`/`BondInput` path being effectively dead for MCP purposes) is documented
above and fine to leave as-is unless it starts actually mattering.