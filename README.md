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

## Setup

Requires Python ≥3.10.

```bash
python -m venv .venv
.venv/bin/pip install -e .
.venv/bin/playwright install chromium
```

`.mcp.json` launches the server with `${CLAUDE_PLUGIN_ROOT}/.venv/bin/python` — this
assumes the `.venv/` above exists at the plugin root with dependencies installed. That's
fine for local development but won't survive a fresh install on another machine; revisit
before distributing this plugin beyond local testing (e.g. `--plugin-dir`).

## Local testing

```bash
claude --plugin-dir /path/to/ua-ovdp-plugin
```

Then run `/mcp` inside Claude Code to confirm the `ovdp-bonds` server connected and its
tools are listed. Call `market_info()` first each session — it reports whether any market
snapshot exists yet (call `run_scraper()` if not). Snapshots land in `market_history/` in the
*host* project (not the plugin's own directory) — see
[CLAUDE.md](./CLAUDE.md#runtime-data-directories).

## Environment variables

See [CLAUDE.md](./CLAUDE.md#environment-variables-serverpy) for the full list
(`OVDP_SCRAPER_PATH`, `OVDP_PYTHON`, `OVDP_MARKET_DIR`, `OVDP_LOG_PATH`,
`OVDP_SUBPROCESS_TIMEOUT_SEC`). None are required — all have working defaults.

## License

MIT
