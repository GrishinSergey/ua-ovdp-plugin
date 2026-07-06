#!/usr/bin/env python3
"""
OVDP MCP Server
================
Wraps the OVDP scraper (scraper.py) as an MCP server so Claude Code can:

  1. trigger a fresh scrape (run_scraper)
  2. query/filter the resulting bonds.json without dumping the whole
     file into context every time (list_bonds, get_bond, get_warnings)
  3. get an honestly recomputed yield from actual cash flows + price,
     instead of trusting the broker-displayed number (compute_ytm)
  4. track how a bond's price/yield moved over time across scrapes
     (list_snapshots, yield_history) so trend dashboards have real data
  5. track bonds you actually own (add_position/list_positions/update_position/
     remove_position) and compute real P&L/YTM/duration on them (position_metrics,
     portfolio_metrics, simulate_reinvestment) via engine/
  6. compare candidate bonds before buying (compare_bonds) or build a target-income
     portfolio from a bond universe (build_target_portfolio) via engine/

IMPORTANT: this process talks to Claude Code over stdio. Never `print()`
to stdout — anything printed there corrupts the MCP protocol stream.
Diagnostics go to stderr only (the default for uncaught exceptions/logging).

Zero config needed by default: the scraper is bundled in this package and all
data is written into the HOST PROJECT's ./data/ directory (Claude Code sets
CLAUDE_PROJECT_DIR in the server's environment; standalone it falls back to CWD).

Optional environment overrides:

  OVDP_SCRAPER_PATH        path to scraper.py       (default: bundled scraper.py)
  OVDP_PYTHON              python to run it with     (default: this interpreter — has playwright)
  OVDP_MARKET_DIR          dir of timestamped market snapshots (default: <project>/market_history)
  OVDP_LOG_PATH            full scraper log file     (default: <project>/data/scraper.log)
  OVDP_SUBPROCESS_TIMEOUT_SEC  hard cap on a run, s  (default: 900)

run_scraper never returns the scraper's log stream to the model — only a compact
summary; the full DEBUG log goes to OVDP_LOG_PATH on disk.

Entry point: `ua-ovdp-mcp` (console script) — runs the server over stdio.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

from mcp.server import FastMCP

from engine.investment.domain.models import Portfolio
from engine.investment.forecast.finance import (
    calculate_accrued_interest,
    calculate_position_metrics,
    calculate_portfolio_metrics,
    simulate_reinvestment as _simulate_reinvestment_engine,
)
from engine.investment.forecast.forecast_core import compare_bonds as _compare_bonds_engine
from engine.investment.forecast.strategy import get_strategy
from engine.investment.portfolio.engine import EngineRequest, build_portfolio
from engine.serialize import to_jsonable
from engine.services import portfolio_service
from engine.services.market_bridge import bond_from_snapshot, freeze_bond, position_from_record


# ══ configuration ═════════════════════════════════════════════════════════

def _env_path(name: str, default: Path) -> Path:
    v = os.environ.get(name)
    return Path(v).expanduser().resolve() if v else default

PYTHON_BIN = os.environ.get("OVDP_PYTHON", sys.executable)
# scraper.py is bundled inside this package — no env var needed by default.
_PKG_DIR = Path(__file__).resolve().parent
# data lives in the HOST PROJECT: Claude Code sets CLAUDE_PROJECT_DIR to the project
# root in the server's environment; fall back to CWD when run standalone.
_PROJECT_DIR = Path(os.environ.get("CLAUDE_PROJECT_DIR", ".")).resolve()
_DATA_DIR = _PROJECT_DIR / "data"

SCRAPER_PATH = _env_path("OVDP_SCRAPER_PATH", _PKG_DIR / "scraper.py")
MARKET_DIR = _env_path("OVDP_MARKET_DIR", _PROJECT_DIR / "market_history")   # timestamped snapshots
LOG_PATH = _env_path("OVDP_LOG_PATH", _DATA_DIR / "scraper.log")
SUBPROCESS_TIMEOUT_SEC = int(os.environ.get("OVDP_SUBPROCESS_TIMEOUT_SEC", "900"))

VALID_BROKERS = {"Приват24", "Inzhur", "Sense", "Універі"}

mcp = FastMCP("ovdp-bonds")


# ══ small helpers ═════════════════════════════════════════════════════════

# Snapshot filenames are YYYYMMDDHHMMSS.json (e.g. 20260706142200.json → 6 Jul 2026
# 14:22:00, local time) — sorts correctly as a plain string. "Latest" is still resolved
# by PARSING the timestamp rather than trusting name sort, as a defensive measure against
# any malformed or manually-dropped files.
_MARKET_TS_FMT = "%Y%m%d%H%M%S"

def market_snapshot_name(dt: Optional[datetime] = None) -> str:
    return (dt or datetime.now()).strftime(_MARKET_TS_FMT) + ".json"

def _parse_market_ts(name: str) -> Optional[datetime]:
    stem = name[:-5] if name.lower().endswith(".json") else name
    try:
        return datetime.strptime(stem, _MARKET_TS_FMT)
    except ValueError:
        return None

def _market_files() -> list[Path]:
    if not MARKET_DIR.exists():
        return []
    return [p for p in MARKET_DIR.glob("*.json") if p.is_file()]

def _snapshot_time(p: Path) -> float:
    ts = _parse_market_ts(p.name)                 # parsed DDMMYYYYHHMMSS, else file mtime
    return ts.timestamp() if ts else p.stat().st_mtime

def _latest_market_file() -> Optional[Path]:
    files = _market_files()
    return max(files, key=_snapshot_time) if files else None

def _resolve_path(data_path: Optional[str]) -> Path:
    """Explicit path if given, else the newest snapshot in MARKET_DIR (by parsed
    timestamp). Raises a Claude-friendly error when there is nothing to read yet."""
    if data_path:
        return Path(data_path).expanduser().resolve()
    latest = _latest_market_file()
    if latest is None:
        raise FileNotFoundError(
            f"no market snapshots in {MARKET_DIR} yet — call run_scraper() first, "
            f"or pass an explicit data_path to a snapshot file."
        )
    return latest

def _snapshot_meta(path: Path) -> dict[str, Any]:
    """Which snapshot a read tool actually used — just the filename + its timestamp,
    never the file contents, so Claude can judge freshness without an extra round-trip
    to market_info()/list_snapshots()."""
    ts = _parse_market_ts(path.name)
    return {
        "snapshot_file": path.name,
        "snapshot_taken_at": ts.isoformat() if ts else None,
    }


# ══ engine/ bridging (position tracking, forecasting, portfolio construction) ═══════

def _load_engine_bond(isin: str, path: Path) -> Optional[Any]:
    """One bond, converted to an engine Bond via market_bridge. None if not in this
    snapshot (not: malformed — a genuine conversion error is a real bug and should raise)."""
    data = _load_bonds_file(path)
    raw = next((b for b in data["bonds"] if b["isin"] == isin), None)
    return bond_from_snapshot(raw) if raw is not None else None


def _load_engine_bonds(path: Path, isins: Optional[list[str]] = None) -> dict[str, Any]:
    """All (or a filtered subset of) bonds in a snapshot, converted to engine Bonds."""
    data = _load_bonds_file(path)
    return {
        b["isin"]: bond_from_snapshot(b)
        for b in data["bonds"]
        if not isins or b["isin"] in isins
    }


def _compute_portfolio_metrics(as_of: date, label: Optional[str], data_path: Optional[str]):
    """Shared by portfolio_metrics and simulate_reinvestment: load recorded positions
    (optionally filtered by label), enrich with live prices when a snapshot is available
    (optional — a position is still fully computable from its frozen bond_snapshot alone),
    and return the engine's PortfolioMetrics dataclass (caller serializes)."""
    records = portfolio_service.get_positions()
    if label:
        records = [r for r in records if r.get("label") == label]
    if not records:
        raise ValueError("no positions recorded yet — call add_position() first")

    try:
        live_bonds = _load_engine_bonds(_resolve_path(data_path))
    except FileNotFoundError:
        live_bonds = {}

    positions = [
        position_from_record(
            r, as_of,
            live_price=(live_bonds[r["isin"]].last_market_price if r["isin"] in live_bonds else None),
        )
        for r in records
    ]
    return calculate_portfolio_metrics(Portfolio(name="default", positions=positions), as_of)

def _load_bonds_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(
            f"{path} does not exist — run run_scraper() first, or pass an "
            f"explicit data_path to an existing snapshot."
        )
    return json.loads(path.read_text(encoding="utf-8"))

def _xirr(cashflows: list[tuple[date, float]]) -> Optional[float]:
    """
    Solve for the annualized effective rate r such that
    sum(amount / (1+r)^(days_since_t0/365)) == 0, via bisection.
    cashflows[0] is expected to be the negative purchase price at t0;
    the rest are positive future inflows. Returns None if it can't bracket a root.
    """
    if len(cashflows) < 2:
        return None
    t0 = cashflows[0][0]

    def npv(r: float) -> float:
        total = 0.0
        for d, amt in cashflows:
            days = (d - t0).days
            total += amt / ((1 + r) ** (days / 365.0))
        return total

    lo, hi = -0.99, 10.0
    f_lo, f_hi = npv(lo), npv(hi)
    if f_lo * f_hi > 0:
        return None  # no sign change in range — shouldn't happen for a normal bond
    for _ in range(200):
        mid = (lo + hi) / 2
        f_mid = npv(mid)
        if abs(f_mid) < 1e-6:
            return mid
        if f_lo * f_mid < 0:
            hi, f_hi = mid, f_mid
        else:
            lo, f_lo = mid, f_mid
    return (lo + hi) / 2


# ══ tools: running the scraper ════════════════════════════════════════════

@mcp.tool()
async def run_scraper(
        output_path: Optional[str] = None,
        timeout: int = 25,
        concurrency: int = 4,
        isins: Optional[list[str]] = None,
        p24_schedules: bool = False,
        headless: bool = True,
        log_level: str = "INFO",
) -> dict[str, Any]:
    """
    Run the OVDP scraper (ovdp.in.ua + Приват24 + Inzhur) and save a fresh market
    snapshot. Takes ~10s-2min depending on universe size and p24_schedules.

    By DEFAULT the result is written to a new timestamped file in the market dir:
        <MARKET_DIR>/<YYYYMMDDHHMMSS>.json     e.g. 20260706142200.json
    The read tools (list_bonds/get_bond/compute_ytm/get_warnings) then pick up the
    NEWEST snapshot automatically. Use `market_info()` to see the market dir & latest.

    Args:
        output_path: write the snapshot to THIS exact path instead of the default
            timestamped file. Use it when you want to control the filename/location
            (the value overrides the market dir for this run only).
        timeout: per-page navigation timeout, seconds (scraper's --timeout).
        concurrency: parallel page fetches (scraper's --concurrency).
        isins: restrict the universe to specific ISINs (scraper's --isins).
        p24_schedules: also fetch Privat24 card payout schedules — needed for a full
            future schedule on FX/broker-only bonds. Slow: one page per ISIN.
        headless: run the browser headless (set False only for local debugging).
        log_level: DEBUG/INFO/WARNING/ERROR for the scraper's own log file.

    Returns a COMPACT summary only (never the log stream): ok, return_code,
    output_path (the file actually written), parsed_at, total_bonds, warning_count,
    log_file. On failure it adds a short stderr_tail. The full DEBUG log is on disk.
    """
    if not SCRAPER_PATH.exists():
        return {
            "ok": False,
            "error": f"scraper not found at {SCRAPER_PATH} (override with OVDP_SCRAPER_PATH if intentional)"
        }

    if output_path:
        out_path = Path(output_path).expanduser().resolve()
    else:
        out_path = MARKET_DIR / market_snapshot_name()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    _DATA_DIR.mkdir(parents=True, exist_ok=True)   # for the log file

    argv = [
        str(PYTHON_BIN), str(SCRAPER_PATH),
        "--out", str(out_path),
        "--timeout", str(timeout),
        "--concurrency", str(concurrency),
        "--headless", "true" if headless else "false",
        "--log-level", log_level,
        "--log-file", str(LOG_PATH),           # full DEBUG log to disk, not to Claude
    ]
    if isins:
        argv += ["--isins", *isins]
    if p24_schedules:
        argv.append("--p24-schedules")

    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(_PROJECT_DIR),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=SUBPROCESS_TIMEOUT_SEC
        )
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        return {"ok": False, "error": f"scraper timed out after {SUBPROCESS_TIMEOUT_SEC}s "
                                      f"(raise OVDP_SUBPROCESS_TIMEOUT_SEC if this is expected)"}
    except FileNotFoundError as e:
        return {"ok": False, "error": f"could not launch scraper: {e}. "
                                      f"Check OVDP_PYTHON={PYTHON_BIN} and OVDP_SCRAPER_PATH={SCRAPER_PATH}"}

    ok = proc.returncode == 0
    # Compact payload only. Full DEBUG log is on disk at LOG_PATH; stderr surfaced
    # (trimmed) only on failure so it never burns the model's context on success.
    result: dict[str, Any] = {
        "ok": ok,
        "return_code": proc.returncode,
        "log_file": str(LOG_PATH),
    }
    if not ok:
        result["stderr_tail"] = stderr.decode("utf-8", "replace")[-1500:]

    if not out_path.exists():
        result["error"] = f"scraper finished (rc={proc.returncode}) but {out_path} was not written"
        if "stderr_tail" not in result:
            result["stderr_tail"] = stderr.decode("utf-8", "replace")[-1500:]
        return result

    data = _load_bonds_file(out_path)
    result.update({
        "output_path": str(out_path),
        "snapshot": out_path.name,
        "parsed_at": data.get("parsed_at"),
        "total_bonds": data.get("total_bonds"),
        "warning_count": len(data.get("warnings", [])),
    })
    return result


# ══ tools: reading data ═══════════════════════════════════════════════════

@mcp.tool()
def list_bonds(
        currency: Optional[str] = None,
        is_military: Optional[bool] = None,
        broker: Optional[str] = None,
        min_yield_pct: Optional[float] = None,
        max_yield_pct: Optional[float] = None,
        maturity_before: Optional[str] = None,
        maturity_after: Optional[str] = None,
        sort_by: str = "maturity_date",
        limit: Optional[int] = None,
        fields: Optional[list[str]] = None,
        data_path: Optional[str] = None,
) -> dict[str, Any]:
    """
    List bonds from the last scrape, filtered/sorted server-side so you don't
    have to pull the whole dataset into context for every question.

    Args:
        currency: "UAH" | "USD" | "EUR".
        is_military: True/False to filter on the military-bond flag (Приват24-sourced;
            None for both filter-off and "unknown", since is_military can itself be null).
        broker: only bonds currently purchasable at this broker, e.g. "Приват24", "Inzhur", "Sense".
        min_yield_pct / max_yield_pct: filter on yield_actual_pct (the ovdp/broker-displayed
            figure, NOT a recomputed YTM — use compute_ytm for that).
        maturity_before / maturity_after: ISO dates "YYYY-MM-DD", exclusive.
        sort_by: "maturity_date" | "yield_actual_pct" | "yield_original_pct" | "best_price".
        limit: cap the number of results.
        fields: return only these top-level keys per bond, e.g.
            ["isin", "maturity_date", "yield_actual_pct", "where_to_buy"], to keep
            the response small. Omit for full records including full schedules.
        data_path: read a specific snapshot file instead of the latest one
            in the market dir (default is the newest snapshot).

    Returns {count, total_in_source, parsed_at, snapshot_file, snapshot_taken_at, bonds}.
    """
    path = _resolve_path(data_path)
    data = _load_bonds_file(path)
    bonds = data["bonds"]

    def best_price(b: dict) -> float:
        prices = [w["price"] for w in b.get("where_to_buy", []) if w.get("price") is not None]
        return min(prices) if prices else float("inf")

    out = []
    for b in bonds:
        if currency and b.get("currency") != currency:
            continue
        if is_military is not None and b.get("is_military") != is_military:
            continue
        if broker and not any(w.get("broker") == broker for w in b.get("where_to_buy", [])):
            continue
        y = b.get("yield_actual_pct")
        if min_yield_pct is not None and (y is None or y < min_yield_pct):
            continue
        if max_yield_pct is not None and (y is None or y > max_yield_pct):
            continue
        md = b.get("maturity_date")
        if maturity_before and (not md or md >= maturity_before):
            continue
        if maturity_after and (not md or md <= maturity_after):
            continue
        out.append(b)

    key_fns = {
        "maturity_date": lambda b: b.get("maturity_date") or "9999",
        "yield_actual_pct": lambda b: b.get("yield_actual_pct") if b.get("yield_actual_pct") is not None else -1,
        "yield_original_pct": lambda b: b.get("yield_original_pct") if b.get("yield_original_pct") is not None else -1,
        "best_price": best_price,
    }
    out.sort(key=key_fns.get(sort_by, key_fns["maturity_date"]),
             reverse=sort_by in ("yield_actual_pct", "yield_original_pct"))

    if limit:
        out = out[:limit]
    if fields:
        out = [{k: b.get(k) for k in fields} for b in out]

    return {
        "count": len(out),
        "total_in_source": len(bonds),
        "parsed_at": data.get("parsed_at"),
        **_snapshot_meta(path),
        "bonds": out,
    }


@mcp.tool()
def get_bond(isin: str, data_path: Optional[str] = None) -> dict[str, Any]:
    """
    Full record for a single bond by ISIN: name, currency, full schedule,
    where_to_buy (per-broker price/yield), and any warnings mentioning this ISIN.
    """
    path = _resolve_path(data_path)
    data = _load_bonds_file(path)
    bond = next((b for b in data["bonds"] if b["isin"] == isin), None)
    if not bond:
        return {"error": f"ISIN {isin} not found in {path}"}
    related_warnings = [w for w in data.get("warnings", []) if isin in w]
    return {**_snapshot_meta(path), **bond, "related_warnings": related_warnings}


@mcp.tool()
def get_warnings(data_path: Optional[str] = None) -> dict[str, Any]:
    """
    Data-quality warnings from the last scrape: currency/maturity/coupon
    mismatches between sources, stale broker payments not in the ovdp schedule,
    and bonds with no ovdp metadata (broker-only).
    """
    data = _load_bonds_file(_resolve_path(data_path))
    return {
        "parsed_at": data.get("parsed_at"),
        "count": len(data.get("warnings", [])),
        "warnings": data.get("warnings", []),
    }


# ══ tools: yield recomputation ════════════════════════════════════════════

@mcp.tool()
def compute_ytm(
        isin: str,
        broker: Optional[str] = None,
        price: Optional[float] = None,
        settlement_date: Optional[str] = None,
        data_path: Optional[str] = None,
) -> dict[str, Any]:
    """
    Recompute the actual annualized yield (XIRR) for a bond from its real cash-flow
    schedule and a purchase price — rather than trusting the yield a broker displays,
    which the source scraper's own notes flag as "not authoritative".

    Args:
        isin: bond ISIN.
        broker: which where_to_buy entry's price to use (e.g. "Приват24", "Inzhur", "Sense").
            If omitted and no explicit `price` is given, the cheapest available broker is used.
        price: override the purchase price per 1000 nominal, ignoring where_to_buy entirely.
        settlement_date: ISO date "YYYY-MM-DD" to discount from. Defaults to today (UTC).
        data_path: read a specific snapshot file instead of the latest one
            in the market dir (default is the newest snapshot).

    Returns ytm_effective_annual_pct (the recomputed figure), alongside price,
    broker, days_to_maturity, and the broker's own displayed yield for comparison.
    """
    path = _resolve_path(data_path)
    data = _load_bonds_file(path)
    bond = next((b for b in data["bonds"] if b["isin"] == isin), None)
    if not bond:
        return {"error": f"ISIN {isin} not found"}

    settlement = date.fromisoformat(settlement_date) if settlement_date else datetime.now(timezone.utc).date()

    used_price = price
    used_broker = broker
    if used_price is None:
        wtb = bond.get("where_to_buy", [])
        if broker:
            match = next((w for w in wtb if w.get("broker") == broker), None)
            if not match:
                return {"error": f"{isin} not offered by broker '{broker}'. "
                                 f"Available: {[w.get('broker') for w in wtb]}"}
            used_price = match["price"]
        else:
            priced = [w for w in wtb if w.get("price") is not None]
            if not priced:
                return {"error": f"{isin} has no where_to_buy price and no price override given"}
            match = min(priced, key=lambda w: w["price"])
            used_price, used_broker = match["price"], match["broker"]
    else:
        used_broker = broker or "custom"

    future_cf = [
        (date.fromisoformat(s["date"]), s["amount"])
        for s in bond.get("schedule", [])
        if date.fromisoformat(s["date"]) > settlement
    ]
    if not future_cf:
        return {"error": f"{isin}: no future cash flows after {settlement.isoformat()} "
                         f"— bond may already be past maturity or schedule is empty"}

    grouped: dict[date, float] = {}
    for d, amt in future_cf:
        grouped[d] = grouped.get(d, 0.0) + amt

    cashflows = [(settlement, -used_price)] + sorted(grouped.items())
    ytm = _xirr(cashflows)

    return {
        "isin": isin,
        "broker": used_broker,
        "price": used_price,
        "settlement_date": settlement.isoformat(),
        "currency": bond.get("currency"),
        "ytm_effective_annual_pct": round(ytm * 100, 3) if ytm is not None else None,
        "days_to_maturity": (max(grouped) - settlement).days,
        "total_future_cashflow": round(sum(grouped.values()), 2),
        **_snapshot_meta(path),
        "broker_displayed_yield_pct": next(
            (w.get("yield_pct") for w in bond.get("where_to_buy", []) if w.get("broker") == used_broker),
            None,
        ),
        "note": "ytm_effective_annual_pct is recomputed from actual cashflows and price "
                "(effective annual compounding). broker_displayed_yield_pct is whatever "
                "convention the broker itself uses and may not match.",
    }


# ══ tools: market directory / history / trends ════════════════════════════

@mcp.tool()
def market_info() -> dict[str, Any]:
    """
    Where market snapshots live and which one is current. Call this at the START of
    a session to orient: it returns the market directory, how many snapshots exist,
    and the LATEST one (the file the read tools use by default). Snapshots are named
    <YYYYMMDDHHMMSS>.json; "latest" is resolved by parsing that timestamp, not by
    name sort (defensive, in case of malformed filenames). If count is 0, call
    run_scraper() to create the first snapshot.
    """
    files = sorted(_market_files(), key=_snapshot_time)
    latest = files[-1] if files else None
    info: dict[str, Any] = {
        "market_dir": str(MARKET_DIR),
        "count": len(files),
        "snapshots": [f.name for f in files],   # oldest → newest
        "latest": None,
    }
    if latest is not None:
        try:
            data = _load_bonds_file(latest)
            info["latest"] = {
                "file": latest.name,
                "path": str(latest),
                "parsed_at": data.get("parsed_at"),
                "total_bonds": data.get("total_bonds"),
                "warning_count": len(data.get("warnings", [])),
            }
        except Exception as e:
            info["latest"] = {"file": latest.name, "path": str(latest), "error": str(e)}
    return info


@mcp.tool()
def list_snapshots() -> dict[str, Any]:
    """
    List market snapshots (oldest → newest). Pass any snapshot's filename or full
    path as `data_path` in list_bonds/get_bond/compute_ytm to query that exact point
    in time instead of the latest.
    """
    files = sorted(_market_files(), key=_snapshot_time)
    return {
        "count": len(files),
        "market_dir": str(MARKET_DIR),
        "snapshots": [
            {
                "file": f.name,
                "path": str(f),
                "taken": (ts.isoformat() if (ts := _parse_market_ts(f.name)) else None)
            }

            for f in files
        ]
    }


@mcp.tool()
def yield_history(isin: str) -> dict[str, Any]:
    """
    Track how a bond's displayed yield and per-broker prices changed across ALL
    market snapshots (oldest → newest). Use before a "how has this bond's yield
    moved" trend chart — pulls real historical points, not a single reading.
    """
    files = sorted(_market_files(), key=_snapshot_time)
    if not files:
        return {
            "error": f"no snapshots in {MARKET_DIR} yet — run run_scraper() a few times to build history"
        }

    points = []
    for f in files:
        try:
            data = _load_bonds_file(f)
        except Exception:
            continue
        bond = next((b for b in data.get("bonds", []) if b["isin"] == isin), None)
        if not bond:
            continue
        points.append({
            "snapshot": f.name,
            "parsed_at": data.get("parsed_at"),
            "yield_actual_pct": bond.get("yield_actual_pct"),
            "where_to_buy": bond.get("where_to_buy"),
        })
    if not points:
        return {"isin": isin, "points": [], "note": "ISIN not found in any snapshot"}
    return {"isin": isin, "points": points}


# ══ tools: position tracking (bonds you actually own) ═══════════════════════

@mcp.tool()
def add_position(
        isin: str,
        purchase_date: str,
        quantity: int,
        purchase_price_dirty: float,
        broker_fee: float = 0.0,
        accrued_interest_paid: Optional[float] = None,
        broker: Optional[str] = None,
        label: Optional[str] = None,
        data_path: Optional[str] = None,
) -> dict[str, Any]:
    """
    Record a bond you actually bought — either today or in the past, as long as it's
    still active (not matured) and resolvable in a market snapshot (current or, if you
    pass one, an older one via data_path). The bond's schedule/maturity/face value/currency
    are frozen into the position at this point (a contractual fact, independent of later
    scrapes); only price is ever re-resolved live afterward.

    Args:
        isin: bond ISIN — must be resolvable in the snapshot (see data_path).
        purchase_date: ISO "YYYY-MM-DD", can be in the past.
        quantity: number of bonds bought.
        purchase_price_dirty: what you actually paid per bond (dirty price, incl. НКД) —
            always explicit; never inferred from market data, since this is the one fact
            only you know regardless of purchase date.
        broker_fee: total fee paid for this purchase (flat amount, not a %).
        accrued_interest_paid: НКД paid per bond at purchase. If omitted, auto-computed
            from the bond's schedule as of purchase_date — override only if your broker's
            figure genuinely differs.
        broker: which broker you bought from (informational only).
        label: free-text tag for grouping/filtering positions later.
        data_path: resolve the bond from this snapshot instead of the latest one.

    Returns the created position record (including its new id) or {"error": ...} if the
    ISIN isn't resolvable in the snapshot used.
    """
    path = _resolve_path(data_path)
    bond = _load_engine_bond(isin, path)
    if bond is None:
        return {
            "error": f"{isin} not found in {path.name} — check the ISIN, or that it's "
                     f"still tracked (run_scraper() to refresh, or pass an older data_path)."
        }

    pdate = date.fromisoformat(purchase_date)
    if accrued_interest_paid is None:
        accrued_interest_paid = float(calculate_accrued_interest(bond, pdate, quantity=1).amount_per_bond)

    record = {
        "isin": isin,
        "purchase_date": purchase_date,
        "quantity": quantity,
        "purchase_price_dirty": purchase_price_dirty,
        "broker_fee": broker_fee,
        "accrued_interest_paid": accrued_interest_paid,
        "broker": broker,
        "label": label,
        "bond_snapshot": freeze_bond(bond),
    }
    saved = portfolio_service.add_position(record)
    return {"position": saved, **_snapshot_meta(path)}


@mcp.tool()
def list_positions(label: Optional[str] = None) -> dict[str, Any]:
    """List recorded positions (bonds you own), optionally filtered by label."""
    positions = portfolio_service.get_positions()
    if label:
        positions = [p for p in positions if p.get("label") == label]
    return {"count": len(positions), "positions": positions}


@mcp.tool()
def update_position(
        position_id: str,
        quantity: Optional[int] = None,
        purchase_price_dirty: Optional[float] = None,
        broker_fee: Optional[float] = None,
        accrued_interest_paid: Optional[float] = None,
        broker: Optional[str] = None,
        label: Optional[str] = None,
) -> dict[str, Any]:
    """
    Correct fields on a recorded position (e.g. fixing a typo'd quantity/price/fee).
    Only pass the fields you want to change. To change the ISIN or purchase_date,
    remove_position() and add_position() again instead — that's a different bond/schedule.
    """
    updates = {k: v for k, v in {
        "quantity": quantity,
        "purchase_price_dirty": purchase_price_dirty,
        "broker_fee": broker_fee,
        "accrued_interest_paid": accrued_interest_paid,
        "broker": broker,
        "label": label,
    }.items() if v is not None}
    if not updates:
        return {"error": "no fields given to update"}
    updated = portfolio_service.update_position(position_id, updates)
    if updated is None:
        return {"error": f"position {position_id} not found"}
    return {"position": updated}


@mcp.tool()
def remove_position(position_id: str) -> dict[str, Any]:
    """Delete a recorded position (e.g. entered by mistake, or fully sold/closed out)."""
    ok = portfolio_service.delete_position(position_id)
    if not ok:
        return {"error": f"position {position_id} not found"}
    return {"ok": True, "position_id": position_id}


# ══ tools: position/portfolio analytics ═════════════════════════════════════

@mcp.tool()
def position_metrics(
        position_id: str,
        as_of: Optional[str] = None,
        data_path: Optional[str] = None,
) -> dict[str, Any]:
    """
    Full analytics for one owned position: realized income/profit (coupons/maturity
    that occurred between purchase and as_of, assumed paid on schedule), expected future
    cashflows, YTM at purchase, modified duration/convexity/DV01, and — if a market
    snapshot is available — current market value and unrealized P&L.

    Args:
        position_id: from add_position()/list_positions().
        as_of: ISO date to compute as of. Defaults to today.
        data_path: use this snapshot for the live price enrichment instead of the latest
            one. Missing/unavailable snapshot degrades gracefully (core metrics from the
            position's own frozen bond data still compute; only market_value/
            unrealized_pnl/current_ytm come back null).
    """
    record = portfolio_service.get_position_by_id(position_id)
    if record is None:
        return {"error": f"position {position_id} not found"}

    as_of_date = date.fromisoformat(as_of) if as_of else date.today()

    live_meta: dict[str, Any] = {}
    try:
        path = _resolve_path(data_path)
        live_bond = _load_engine_bond(record["isin"], path)
        live_price = live_bond.last_market_price if live_bond else None
        if live_bond is not None:
            live_meta = _snapshot_meta(path)
    except FileNotFoundError:
        live_price = None

    position = position_from_record(record, as_of_date, live_price=live_price)
    metrics = calculate_position_metrics(position, as_of_date)
    return {**to_jsonable(metrics), **live_meta}


@mcp.tool()
def portfolio_metrics(
        as_of: Optional[str] = None,
        label: Optional[str] = None,
        data_path: Optional[str] = None,
) -> dict[str, Any]:
    """
    Aggregate analytics across all recorded positions (or those matching `label`):
    total invested, realized + expected profit, average YTM/duration, breakdowns by
    currency and maturity bucket, and a month-by-month cashflow forecast.
    """
    as_of_date = date.fromisoformat(as_of) if as_of else date.today()
    try:
        metrics = _compute_portfolio_metrics(as_of_date, label, data_path)
    except ValueError as e:
        return {"error": str(e)}
    return to_jsonable(metrics)


@mcp.tool()
def simulate_reinvestment(
        reinvest_rate: float = 0.15,
        years: int = 3,
        as_of: Optional[str] = None,
        label: Optional[str] = None,
        data_path: Optional[str] = None,
) -> dict[str, Any]:
    """
    "What if I reinvest every coupon at reinvest_rate for `years`?" — projects your
    current portfolio's coupons forward with compounding reinvestment, returning the
    final value and effective annualized return.
    """
    as_of_date = date.fromisoformat(as_of) if as_of else date.today()
    try:
        pm = _compute_portfolio_metrics(as_of_date, label, data_path)
    except ValueError as e:
        return {"error": str(e)}
    scenario = _simulate_reinvestment_engine(pm, reinvest_rate=reinvest_rate, years=years)
    return to_jsonable(scenario)


# ══ tools: shopping — compare candidates, build a target portfolio ══════════

@mcp.tool()
def compare_bonds(
        isins: list[str],
        quantity: int = 1,
        as_of: Optional[str] = None,
        horizon_months: Optional[int] = None,
        compare_by_full_period: bool = False,
        broker: Optional[str] = None,
        strategy: str = "standard",
        data_path: Optional[str] = None,
) -> dict[str, Any]:
    """
    Rank 2+ candidate bonds (not yet owned) by effective annual return over a shared
    horizon — "which of these is actually the better buy."

    Args:
        isins: 2+ ISINs to compare (must resolve in the snapshot used).
        quantity: bonds per candidate, for comparable absolute totals.
        as_of: settlement date. Defaults to today.
        horizon_months: shared comparison horizon. Omit to use the nearest candidate's
            maturity. Ignored if compare_by_full_period=True.
        compare_by_full_period: hold each bond to ITS OWN maturity instead of a shared
            horizon (so results aren't directly comparable period-for-period, but each
            reflects that bond's true full-life return).
        broker: informational only — NOTE: forecast_core's own math currently prices
            every candidate off Bond.last_market_price (cheapest available), not a
            broker-specific price, regardless of this parameter. Kept for parity with the
            engine's own signature; doesn't yet filter/select price by broker.
        strategy: "standard" (dirty-price basis, financially correct) or "приват24"/
            "privat24" (clean-price basis, matches Privat24's own displayed numbers).
        data_path: use this snapshot instead of the latest one.

    Returns ranked BondForecastItem list + any data-quality warnings (e.g. a candidate
    excluded for having no market price).
    """
    path = _resolve_path(data_path)
    bonds_by_isin = _load_engine_bonds(path, isins=isins)
    missing = [i for i in isins if i not in bonds_by_isin]

    try:
        strat = get_strategy(strategy)
    except ValueError as e:
        return {"error": str(e)}

    try:
        result = _compare_bonds_engine(
            bonds=list(bonds_by_isin.values()), quantity=quantity,
            as_of=(date.fromisoformat(as_of) if as_of else None),
            horizon_months=horizon_months, compare_by_full_period=compare_by_full_period,
            broker=broker, strategy=strat,
        )
    except ValueError as e:
        return {"error": str(e), "missing_isins": missing, **_snapshot_meta(path)}

    return {**to_jsonable(result), "missing_isins": missing, **_snapshot_meta(path)}


@mcp.tool()
def build_target_portfolio(
        target_income: float,
        horizon_days: int,
        isins: Optional[list[str]] = None,
        settlement_date: Optional[str] = None,
        mode: str = "max_efficiency",
        broker: Optional[str] = None,
        data_path: Optional[str] = None,
) -> dict[str, Any]:
    """
    Build a portfolio that targets `target_income` total return over `horizon_days`,
    picked from a bond universe (or the whole current market if isins is omitted).

    Args:
        target_income: total income you want by the horizon (in whatever the bonds'
            shared currency is — mixing currencies is not converted/normalized).
        horizon_days: investment horizon in days.
        isins: candidate universe. Omit to consider every bond in the snapshot used.
        settlement_date: purchase date for the plan. Defaults to today.
        mode:
          - "max_efficiency": greedy allocation, fewest bonds to reach target_income.
          - "monthly_income": allocate for even income >= target_income/N every month
            of the horizon (not just a lump sum by the end).
          - "max_efficiency_reinvest": simulate reinvesting coupons/maturities back into
            the best available bond each month — a fuller month-by-month projection
            with an event timeline.
        broker: restrict pricing to this broker's offer (properly honored here, unlike
            compare_bonds — build_portfolio's underlying math uses Bond.price_for_broker).
        data_path: use this snapshot instead of the latest one.

    Returns the allocation (which bonds, how many units each) + whether target_income
    was actually reachable + any warnings (e.g. candidates excluded for no coupons in
    the horizon).
    """
    path = _resolve_path(data_path)
    bonds_by_isin = _load_engine_bonds(path, isins=isins)
    if not bonds_by_isin:
        return {
            "error": "no resolvable bonds found — check isins, or that the snapshot isn't empty",
            **_snapshot_meta(path),
        }

    req = EngineRequest(
        bonds=list(bonds_by_isin.values()),
        target_income=Decimal(str(target_income)),
        horizon_days=horizon_days,
        settlement_date=(date.fromisoformat(settlement_date) if settlement_date else date.today()),
        mode=mode,
        broker=broker,
    )
    try:
        result = build_portfolio(req)
    except ValueError as e:
        return {"error": str(e), **_snapshot_meta(path)}

    return {**to_jsonable(result), **_snapshot_meta(path)}


def main() -> None:
    """Console-script entry point (`ua-ovdp-mcp`). Runs the MCP server over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()