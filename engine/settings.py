"""
Minimal settings shim for engine/services.

engine/ was extracted from a larger FastAPI backend; its services referenced
`app.settings.settings`, which held who-knows-what (DB config, CORS, auth, ...) for that
app. None of that applies to an MCP server, so it is NOT reconstructed here — this shim
provides only the one field a service actually uses: portfolio_data_path.

Defaults to a CLAUDE_PROJECT_DIR-relative path, matching server.py's own convention
(CLAUDE_PROJECT_DIR is guaranteed set by Claude Code for any stdio MCP server; falls back
to CWD for standalone/test runs).

Market data has its own path (market_history/, resolved in server.py) — engine/ never
reads it directly; server.py resolves the snapshot and hands engine/ plain Bond objects
via engine/services/market_bridge.py.
"""

from __future__ import annotations

import os
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


def _project_dir() -> Path:
    return Path(os.environ.get("CLAUDE_PROJECT_DIR", ".")).resolve()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OVDP_")

    portfolio_data_path: str = str(_project_dir() / "data" / "portfolio.json")


settings = Settings()
