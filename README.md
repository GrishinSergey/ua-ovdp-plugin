# ua-ovdp-plugin

Claude Code plugin for working with Ukrainian government bonds (ОВДП). Bundles an MCP
server (`ovdp-bonds`) backed by a Playwright scraper that reconciles bond data across
`ovdp.in.ua`, Приват24, and Inzhur — broker offers are the source of truth, `ovdp.in.ua`
supplies metadata (full schedule, name, official yields).

See [CLAUDE.md](./CLAUDE.md) for architecture, invariants, and known issues.

## What it gives Claude

- **`ovdp-bonds` MCP server** (`server.py` / `scraper.py`): run a fresh scrape, list/filter
  bonds, pull a single bond's full schedule, recompute real yield (XIRR) from actual
  cashflows instead of trusting the broker's displayed number, and track yield/price
  history across snapshots. Full tool list in [CLAUDE.md](./CLAUDE.md#mcp-tool-surface-serverpy).
- **Position tracking & portfolio construction** (`engine/`, wired in via
  `engine/services/market_bridge.py`): record bonds you actually own and get real P&L/YTM/
  duration on them, compare candidate bonds before buying, or build a portfolio targeting a
  given income over a horizon. See [CLAUDE.md](./CLAUDE.md#analytics-engine-engine).
- **Skills** (`skills/`): none yet — see [skills/README.md](./skills/README.md).

## Requirements

- [`uv`](https://docs.astral.sh/uv/getting-started/installation/) — that's it. `.mcp.json`
  launches the server via `uv run --project ${CLAUDE_PLUGIN_ROOT} python server.py`, which
  creates its own venv and installs every Python dependency from `pyproject.toml`/`uv.lock`
  automatically on first launch (~30-60s once, then cached — instant after). No manual
  `pip install` step. `run_scraper()` similarly auto-installs Playwright's chromium build
  the first time it's actually called if it isn't already cached.
- Python ≥3.10 — `uv` will fetch a matching interpreter itself if none is on your PATH.

Install `uv`:
```bash
brew install uv          # macOS
# or: curl -LsSf https://astral.sh/uv/install.sh | sh
```

## Install this plugin in a project

```bash
claude plugin marketplace add GrishinSergey/ua-ovdp-plugin
claude plugin install ua-ovdp-plugin@ua-ovdp-plugin
```

Then open (or `cd` into) whatever project you want bond data tracked in, and run `/mcp`
inside Claude Code to confirm the `ovdp-bonds` server connected. First call to any tool
that touches the scraper will take a bit longer while dependencies/chromium install —
everything after that is fast. Call `market_info()` first each session — it reports whether
a market snapshot exists yet (call `run_scraper()` if not). Snapshots land in
`market_history/` in the *host* project (not the plugin's own directory) — see
[CLAUDE.md](./CLAUDE.md#runtime-data-directories).

## Local testing (plugin dev only)

```bash
claude --plugin-dir /path/to/ua-ovdp-plugin
```
loads the plugin straight from a local checkout instead of through the marketplace —
for iterating on the plugin itself, not for normal use.

## Environment variables

See [CLAUDE.md](./CLAUDE.md#environment-variables-serverpy) for the full list
(`OVDP_SCRAPER_PATH`, `OVDP_PYTHON`, `OVDP_MARKET_DIR`, `OVDP_LOG_PATH`,
`OVDP_SUBPROCESS_TIMEOUT_SEC`). None are required — all have working defaults.

## License

MIT
