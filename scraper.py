"""
OVDP multi-source scraper  →  bonds.json (v4)

SOURCE OF TRUTH = the BROKER (can you actually buy it), NOT ovdp.in.ua.
ovdp.in.ua is treated purely as METADATA (schedule / name / yields) fetched per ISIN.

FLOW:
  1. In parallel, scrape the full catalog of each broker → Map<ISIN, value>:
       next.privat24.ua/bonds/list  (SPA, data-qa-node)  → price, yield, currency, military, maturity
       inzhur.reit/offer/ovdp       (Nuxt SSR)            → price, yield, maturity, coupon, future schedule
  2. universe = ISINs(Privat24) ∪ ISINs(Inzhur).  For every ISIN in the universe, in parallel:
       - fetch ovdp.in.ua/bonds/<ISIN> detail  (metadata: full schedule, name, issue_year, yields)
       - (once) ovdp.in.ua/prices               (Sense/Універі column price + ovdp actual yield)
       broker data for that ISIN we already have from step 1.
  3. reconcile: if a bond is on several sources → merge. Canonical schedule = ovdp (full,
     past+future) when present, else the broker's future-only schedule. Prices come ONLY from
     broker pages (Inzhur/Privat24 own pages; Sense from ovdp's column as enrichment).
     Bonds present on NO broker are NOT emitted (you can't buy them).

Invariant: schedule/coupon/maturity are ISIN-level facts → one canonical copy; only PRICE is
per-broker (where_to_buy[], never averaged). Displayed broker yields are kept per entry but are
NOT authoritative (engine recomputes YTM). Cross-source coupon/maturity/currency disagreement →
output["warnings"].

Observability: every fetch logs status/size/ms (DEBUG); per-item detail DEBUG; anomalies WARNING;
reconcile + run summaries INFO; stage exceptions caught with context. --inzhur-dump saves raw
Inzhur HTML so the text parser can be locked to the SSR payload if labels ever drift.

Run:
  python scraper.py
  python scraper.py --log-level DEBUG
  python scraper.py --p24-schedules -c 4        # future schedule for FX/broker-only bonds
  python scraper.py --inzhur-dump               # dump raw Inzhur HTML and exit
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from bs4 import BeautifulSoup
from loguru import logger
from playwright.async_api import BrowserContext, Page, async_playwright

BASE_OVDP  = "https://ovdp.in.ua"
PRICES_URL = f"{BASE_OVDP}/prices"
INZHUR_URL = "https://www.inzhur.reit/offer/ovdp"
P24_LIST   = "https://next.privat24.ua/bonds/list"
P24_CARD   = "https://next.privat24.ua/bonds/purchase/{isin}"

NOMINAL   = 1000.0
ISIN_RE   = re.compile(r"UA\d{10}")
UA_MONTHS = {"січня":"01","лютого":"02","березня":"03","квітня":"04","травня":"05",
             "червня":"06","липня":"07","серпня":"08","вересня":"09","жовтня":"10",
             "листопада":"11","грудня":"12"}

def _ms(t0: float) -> str:
    return f"{(time.perf_counter() - t0) * 1000:.0f}ms"

def _f(s: str) -> float | None:
    s = s.strip().lstrip("₴").replace("\xa0", "").replace(" ", "").replace(",", ".")
    try:
        return float(s) if s else None
    except ValueError:
        return None

def _pct(s: str) -> float | None:
    return _f(s.replace("%", ""))

def _iso_ddmmyyyy(s: str) -> str | None:
    m = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", s)
    return f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else None

def _iso_ua(s: str) -> str | None:
    s = s.strip().rstrip(".")
    if re.match(r"\d{4}-\d{2}-\d{2}", s):
        return s
    m = re.match(r"(\d{1,2})\s+(\S+)\s+(\d{4})", s)
    if m and (mo := UA_MONTHS.get(m.group(2).lower().rstrip("."))):
        return f"{m.group(3)}-{mo}-{m.group(1).zfill(2)}"
    return None

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

async def goto(page: Page, url: str, timeout: int, label: str = "") -> str | None:
    tag, t0 = label or url, time.perf_counter()
    logger.debug(f"GET  {tag}")
    try:
        resp = await page.goto(url, wait_until="networkidle", timeout=timeout * 1000)
        await page.wait_for_timeout(600)
        html, status = await page.content(), (resp.status if resp else 0)
        logger.debug(f"OK   {tag} [{status}] {len(html):,}B {_ms(t0)}")
        if resp and resp.status >= 400:
            logger.warning(f"{tag}: HTTP {resp.status}")
        return html
    except Exception as e:
        logger.error(f"FAIL {tag} after {_ms(t0)}: {type(e).__name__}: {e}")
        return None


# ══ ovdp.in.ua — METADATA ONLY ═══════════════════════════════════════════════

COL_ISIN, COL_MATURITY, COL_YIELD = 1, 2, 3
# columns we still want from ovdp (brokers we DON'T scrape directly). Приват24/Inzhur are
# dropped here because we take their prices from their own pages.
OVDP_EXTRA_BROKERS = {"Sense", "Універі"}

def parse_prices_meta(html: str) -> dict[str, dict]:
    """ovdp /prices → {isin: {yield_actual_pct, extra_where:[{broker,price} for Sense/Універі]}}."""
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table")
    if not table:
        logger.warning("ovdp: no <table> on /prices — Sense prices unavailable"); return {}
    headers = [th.get_text(strip=True) for th in table.find_all("th")]
    cols: dict[int, str] = {}
    for i, h in enumerate(headers):
        if "Приват" in h:   cols[i] = "Приват24"
        elif "Inzhur" in h: cols[i] = "Inzhur"
        elif "Sense" in h:  cols[i] = "Sense"
        elif "Універі" in h or "univeri" in h.lower(): cols[i] = "Універі"
    if not cols:
        cols = {4: "Приват24", 5: "Inzhur", 6: "Sense"}
        logger.warning(f"ovdp: broker columns not found — fallback {cols}")
    out: dict[str, dict] = {}
    for row in table.find_all("tr"):
        tds = row.find_all("td")
        if len(tds) <= COL_YIELD:
            continue
        link = tds[COL_ISIN].find("a")
        isin = (link.get_text(strip=True) if link else tds[COL_ISIN].get_text(strip=True))
        if not ISIN_RE.match(isin):
            continue
        extra = [{"broker": b, "price": p}
                 for c, b in cols.items()
                 if b in OVDP_EXTRA_BROKERS and c < len(tds) and (p := _f(tds[c].get_text(strip=True)))]
        out[isin] = {"yield_actual_pct": _pct(tds[COL_YIELD].get_text(strip=True)), "extra_where": extra}
    logger.success(f"ovdp: /prices meta → {len(out)} bonds (Sense/Універі columns)")
    return out

def parse_bond_page(html: str, isin: str) -> dict[str, Any] | None:
    """ovdp detail → metadata, or None if ovdp has no real data for this ISIN."""
    soup = BeautifulSoup(html, "html.parser")
    out: dict[str, Any] = {"isin": isin, "url": f"{BASE_OVDP}/bonds/{isin}"}
    name = isin
    for p in soup.find_all("p"):
        t = re.sub(r"\s+", " ", p.get_text(" ", strip=True))
        if "облігація" in t.lower() and "випущена" in t.lower():
            name = t.strip(); break
    out["name"] = name
    nl = name.lower()
    out["currency"] = ("USD" if ("долар" in nl or "usd" in nl)
                       else "EUR" if ("євро" in nl or "eur" in nl) else "UAH")
    m = re.search(r"випущена?\s+у\s+(\d{4})", name, re.I)
    out["issue_year"] = m.group(1) if m else ""
    iso = re.findall(r"\b(\d{4}-\d{2}-\d{2})\b", soup.get_text("\n"))
    out["maturity_date"] = iso[0] if iso else ""
    for key, label in (("yield_original_pct", "Оригінальна дохідність"),
                       ("yield_actual_pct", "Фактична дохідність")):
        out[key] = None
        for el in soup.find_all(string=re.compile(label)):
            if (parent := el.find_parent()) and (mm := re.search(r"(\d+[,\.]\d+)%", parent.get_text(strip=True))):
                out[key] = _pct(mm.group(1)); break
    schedule = []
    tables = soup.find_all("table")
    if tables:
        for row in tables[0].find_all("tr"):
            tds = row.find_all("td")
            if len(tds) < 3:
                continue
            date = _iso_ua(tds[0].get_text(strip=True))
            kind = "погашення" if "погашення" in tds[1].get_text(strip=True).lower() else "купон"
            amt = _f(tds[2].get_text(strip=True))
            if date and amt is not None:
                schedule.append({"date": date, "type": kind, "amount": amt})
    out["schedule"] = sorted(schedule, key=lambda x: x["date"])
    coupons = [s for s in schedule if s["type"] == "купон"]
    out["coupon_amount"] = coupons[0]["amount"] if coupons else None
    out["coupon_currency"] = out["currency"]
    out["first_payment_date"] = coupons[0]["date"] if coupons else None
    # ovdp has no real data if there's no schedule AND no descriptive name
    if not out["schedule"] and out["name"] == isin and out["yield_original_pct"] is None:
        return None
    return out

async def ovdp_details(ctx: BrowserContext, timeout: int, isins: list[str],
                       concurrency: int) -> dict[str, dict]:
    t0 = time.perf_counter()
    logger.info(f"ovdp: fetching {len(isins)} detail pages (concurrency={concurrency})")
    sem = asyncio.Semaphore(concurrency)
    out: dict[str, dict] = {}
    missing, failed = [], []
    async def one(isin: str):
        async with sem:
            page = await ctx.new_page()
            try:
                h = await goto(page, f"{BASE_OVDP}/bonds/{isin}", timeout, label=f"ovdp/{isin}")
                if not h:
                    failed.append(isin); return
                d = parse_bond_page(h, isin)
                if d is None:
                    missing.append(isin)
                    logger.debug(f"ovdp {isin}: no metadata on ovdp (broker-only bond)")
                    return
                out[isin] = d
                logger.debug(f"ovdp {isin}: cur={d['currency']} mat={d['maturity_date']} "
                             f"coupon={d['coupon_amount']} sched={len(d['schedule'])} "
                             f"yorig={d['yield_original_pct']}")
            except Exception as e:
                failed.append(isin); logger.error(f"ovdp {isin}: {type(e).__name__}: {e}")
            finally:
                await page.close()
    await asyncio.gather(*[one(i) for i in isins])
    logger.success(f"ovdp: metadata {len(out)}/{len(isins)} ok  ({_ms(t0)})")
    if missing:
        logger.info(f"ovdp: {len(missing)} broker-only (no ovdp page): {', '.join(sorted(missing))}")
    if failed:
        logger.warning(f"ovdp: {len(failed)} detail FAILED: {', '.join(sorted(failed))}")
    return out

async def ovdp_prices(ctx: BrowserContext, timeout: int) -> dict[str, dict]:
    page = await ctx.new_page()
    html = await goto(page, PRICES_URL, timeout, label="ovdp/prices"); await page.close()
    return parse_prices_meta(html) if html else {}


# ══ inzhur.reit — tolerant text parser (look-ahead for value after label) ═════

_MONEY = re.compile(r"([\d\s\u00a0]+[.,]\d{2})\s*₴")
_DATE  = re.compile(r"^\s*(\d{2})\.(\d{2})\.(\d{4})")
_PCT   = re.compile(r"(\d+(?:[.,]\d+)?)\s*%")

def _inz_money(s: str) -> float | None:
    m = _MONEY.search(s)
    return float(m.group(1).replace("\u00a0","").replace(" ","").replace(",",".")) if m else None

def _inz_pct(s: str) -> float | None:
    m = _PCT.search(s)
    return float(m.group(1).replace(",", ".")) if m else None

def _find_after(block: list[str], low: list[str], label: str,
                value_fn: Callable[[str], Any], window: int = 4) -> Any:
    """First non-None value on the label line or within `window` lines after it.
    Handles both 'label value' (same line) and 'label' / 'value' (separate lines)."""
    for i, l in enumerate(low):
        if label in l:
            for j in range(i, min(i + window + 1, len(block))):
                v = value_fn(block[j])
                if v is not None:
                    return v
    return None

def parse_inzhur_text(text: str) -> dict[str, dict]:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    starts = [i for i, ln in enumerate(lines) if ISIN_RE.search(ln)]
    cards: dict[str, dict] = {}
    for k, s in enumerate(starts):
        e = starts[k + 1] if k + 1 < len(starts) else len(lines)
        if (c := _inz_card(lines[s:e])):
            cards[c["isin"]] = c
    if not cards:
        logger.warning("inzhur: parsed 0 cards — SSR labels changed or page not hydrated")
    else:
        logger.success(f"inzhur: {len(cards)} cards")
    for c in cards.values():
        logger.debug(f"inzhur {c['isin']}: buy={c['buy_price']} sell={c['sell_price']} "
                     f"y={c['yield_actual_pct']} mat={c['maturity_date']} "
                     f"coupon={c['coupon_amount']} sched={len(c['schedule_future'])}")
    noprice = [i for i, c in cards.items() if c["buy_price"] is None]
    if noprice:
        logger.warning(f"inzhur: {len(noprice)} cards without buy price: {', '.join(noprice)} "
                       f"— run --inzhur-dump and check labels")
    return cards

def _inz_card(block: list[str]) -> dict[str, Any] | None:
    m = ISIN_RE.search(block[0])
    if not m:
        return None
    low = [ln.lower() for ln in block]
    y    = _find_after(block, low, "дохідність", _inz_pct)
    mat  = _find_after(block, low, "дата погашення", _iso_ddmmyyyy)
    buy  = _find_after(block, low, "вартість купівлі", _inz_money)
    sell = _find_after(block, low, "вартість продажу", _inz_money)
    sched_start = None
    for i, l in enumerate(low):                       # start after the LAST "графік…виплат"
        if "графік" in l and "виплат" in l:
            sched_start = i + 1
    schedule = _inz_schedule(block[sched_start:]) if sched_start is not None else []
    coupons = [s for s in schedule if s["type"] == "купон"]
    blob = " ".join(block)                            # currency from the glyph, not assumed
    currency = "UAH" if "₴" in blob else "USD" if "$" in blob else "EUR" if "€" in blob else "UAH"
    return {"isin": m.group(0), "currency": currency, "maturity_date": mat,
            "yield_actual_pct": y, "buy_price": buy, "sell_price": sell,
            "schedule_future": schedule,
            "coupon_amount": coupons[0]["amount"] if coupons else None}

def _inz_schedule(rest: list[str]) -> list[dict[str, Any]]:
    out, stop = [], {"інвестувати", "поповнення рахунку", "закрити", "зрозуміло!"}
    pend, redeem = None, False
    for ln in rest:
        if ln.lower() in stop:
            break
        dm, money = _DATE.match(ln), "₴" in ln
        if dm and not money:
            pend, redeem = f"{dm.group(3)}-{dm.group(2)}-{dm.group(1)}", "погашення" in ln.lower()
            continue
        if money and pend and (amt := _inz_money(ln)) is not None:
            out.append({"date": pend, "type": "погашення" if (redeem or amt >= NOMINAL) else "купон",
                        "amount": amt})
            pend, redeem = None, False
    return out

async def inzhur_provider(ctx: BrowserContext, timeout: int, dump: bool = False) -> dict[str, dict]:
    t0 = time.perf_counter()
    page = await ctx.new_page()
    try:
        await page.goto(INZHUR_URL, wait_until="networkidle", timeout=timeout * 1000)
        await page.wait_for_timeout(800)
        if dump:
            Path("inzhur_dump.html").write_text(await page.content(), encoding="utf-8")
            Path("inzhur_innertext.txt").write_text(await page.inner_text("body"), encoding="utf-8")
            logger.warning("inzhur: dumped inzhur_dump.html + inzhur_innertext.txt — send these to lock the parser")
            return {}
        text = await page.inner_text("body")
        logger.debug(f"inzhur: inner_text {len(text):,} chars {_ms(t0)}")
    except Exception as e:
        logger.error(f"inzhur: FAIL {type(e).__name__}: {e}"); return {}
    finally:
        await page.close()
    cards = parse_inzhur_text(text)
    logger.info(f"inzhur: done ({_ms(t0)})")
    return cards


# ══ next.privat24.ua — verified against real DOM ═════════════════════════════

_CUR = re.compile(r"\b(UAH|USD|EUR)\b")

def parse_privat24_list(html: str) -> dict[str, dict]:
    soup = BeautifulSoup(html, "html.parser")
    fresh = _iso_ddmmyyyy(m.group(0)) if (m := re.search(r"Актуально на \d{2}\.\d{2}\.\d{4}", soup.get_text())) else None
    if fresh:
        logger.info(f"p24: data actual on {fresh}")
    rows = soup.select('[data-qa-node="bond"]')
    logger.debug(f"p24: {len(rows)} [data-qa-node=bond] rows in DOM")
    out, noprice = {}, []
    for row in rows:
        isin = (row.get("data-qa-value") or "").strip()
        if not ISIN_RE.fullmatch(isin):
            el = row.select_one('[data-qa-node="isin"]')
            isin = el.get_text(strip=True) if el else ""
        if not ISIN_RE.fullmatch(isin):
            continue
        names = row.select('[data-qa-node="name"]')
        name = names[-1].get_text(" ", strip=True) if names else ""
        price_txt = (el.get_text(" ", strip=True) if (el := row.select_one('[data-qa-node="price"]')) else "")
        y_el = row.select_one('[data-qa-node="yield"]')
        d_el = row.select_one('[data-qa-node="date"]')
        price = _f(price_txt.replace("UAH","").replace("USD","").replace("EUR",""))
        out[isin] = {"buy_price": price,
                     "currency": (cm.group(1) if (cm := _CUR.search(price_txt)) else None),
                     "is_military": "військ" in name.lower(),
                     "maturity_date": _iso_ddmmyyyy(d_el.get_text()) if d_el else None,
                     "yield_actual_pct": _pct(y_el.get_text()) if y_el else None,
                     "name_p24": name, "data_date": fresh}
        if price is None:
            noprice.append(isin)
        logger.debug(f"p24 {isin}: price={price} {out[isin]['currency']} "
                     f"mil={out[isin]['is_military']} mat={out[isin]['maturity_date']} "
                     f"y={out[isin]['yield_actual_pct']}")
    if not out:
        logger.warning("p24: parsed 0 bonds — list may be lazy-loaded (scroll) or selectors drifted")
    else:
        logger.success(f"p24: {len(out)} bonds (list)")
    if noprice:
        logger.warning(f"p24: {len(noprice)} bonds without price: {', '.join(noprice)}")
    return out

def parse_privat24_schedule(html: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    payout = soup.select_one('[data-qa-node="payout"]')
    if not payout:
        return []
    out = []
    for row in payout.find_all("div", recursive=False):
        txt = row.get_text(" ", strip=True)
        date = _iso_ddmmyyyy(txt)
        if not date:
            continue
        am = re.search(r"([\d\s\u00a0]+[.,]\d{2})\s*(?:UAH|USD|EUR)", txt)
        if not am:
            continue
        amt = float(am.group(1).replace("\u00a0","").replace(" ","").replace(",","."))
        out.append({"date": date, "type": "погашення" if ("погаш" in txt.lower() or amt >= NOMINAL) else "купон",
                    "amount": amt})
    return out

async def privat24_list_provider(ctx: BrowserContext, timeout: int) -> dict[str, dict]:
    t0 = time.perf_counter()
    page = await ctx.new_page()
    html = await goto(page, P24_LIST, timeout, label="p24/list"); await page.close()
    if not html:
        return {}
    out = parse_privat24_list(html)
    logger.info(f"p24 list: done ({_ms(t0)})")
    return out

async def privat24_schedules(ctx: BrowserContext, isins: list[str],
                             timeout: int, concurrency: int) -> dict[str, list]:
    t0 = time.perf_counter()
    sem = asyncio.Semaphore(concurrency)
    result: dict[str, list] = {}
    empty = []
    async def one(isin: str):
        async with sem:
            page = await ctx.new_page()
            try:
                await page.goto(P24_CARD.format(isin=isin), wait_until="networkidle", timeout=timeout * 1000)
                await page.wait_for_timeout(500)
                fully = False
                for _ in range(20):
                    btn = page.get_by_text("Показати ще", exact=False)
                    if await btn.count() == 0:
                        fully = True; break
                    try:
                        await btn.first.click(timeout=2000); await page.wait_for_timeout(250)
                    except Exception as e:
                        logger.debug(f"p24 {isin}: expand stopped: {e}"); break
                if not fully:
                    logger.warning(f"p24 {isin}: 'Показати ще' still present after 20 clicks — schedule may be truncated")
                sched = parse_privat24_schedule(await page.content())
                result[isin] = sched
                if not sched:
                    empty.append(isin)
                logger.debug(f"p24 {isin}: {len(sched)} payout rows")
            except Exception as e:
                logger.warning(f"p24 card {isin}: {type(e).__name__}: {e}")
            finally:
                await page.close()
    await asyncio.gather(*[one(i) for i in isins])
    logger.success(f"p24: {len(result)}/{len(isins)} card schedules ({_ms(t0)})")
    if empty:
        logger.warning(f"p24: {len(empty)} empty payout schedules: {', '.join(empty)}")
    return result


# ══ RECONCILE (universe = brokers) ═══════════════════════════════════════════

def _key(s: dict) -> tuple:
    return (s["date"], s["type"], round(s["amount"], 2))

def reconcile(universe: list[str], ovdp: dict[str, dict], ovdp_px: dict[str, dict],
              inzhur: dict[str, dict], p24: dict[str, dict],
              p24_sched: dict[str, list]) -> tuple[list[dict], list[str]]:
    warnings: list[str] = []
    bonds: list[dict] = []
    n_meta = n_brokeronly = 0
    cov_iz = cov_p24 = cov_sense = 0

    for isin in sorted(universe):
        o  = ovdp.get(isin)          # ovdp metadata (may be None)
        op = ovdp_px.get(isin)       # {yield_actual_pct, extra_where:[Sense..]}
        iz = inzhur.get(isin)
        pv = p24.get(isin)

        if o:
            n_meta += 1
            bond = {"isin": isin, "url": o["url"], "name": o["name"], "currency": o["currency"],
                    "issue_year": o["issue_year"], "maturity_date": o["maturity_date"],
                    "yield_original_pct": o.get("yield_original_pct"),
                    "yield_actual_pct": o.get("yield_actual_pct") or (op.get("yield_actual_pct") if op else None),
                    "schedule": o.get("schedule", []), "coupon_amount": o.get("coupon_amount"),
                    "coupon_currency": o.get("coupon_currency", o["currency"]),
                    "first_payment_date": o.get("first_payment_date")}
        else:
            n_brokeronly += 1
            cur = (pv.get("currency") if pv else None) or (iz.get("currency") if iz else None) or "UAH"
            fut = (iz.get("schedule_future") if iz else None) or p24_sched.get(isin, [])
            cpn = (iz.get("coupon_amount") if iz else None) or \
                  next((s["amount"] for s in fut if s["type"] == "купон"), None)
            bond = {"isin": isin, "url": (INZHUR_URL if iz else P24_LIST),
                    "name": (pv.get("name_p24") if pv else None) or isin, "currency": cur,
                    "issue_year": "", "maturity_date": (pv.get("maturity_date") if pv else None) or (iz.get("maturity_date") if iz else None),
                    "yield_original_pct": None, "yield_actual_pct": (op.get("yield_actual_pct") if op else None),
                    "schedule": fut, "coupon_amount": cpn, "coupon_currency": cur,
                    "first_payment_date": (fut[0]["date"] if fut else None)}
            warnings.append(f"{isin}: no ovdp metadata (broker-only) — "
                            f"{'broker future schedule' if fut else 'no schedule'}")

        bond["is_military"] = pv.get("is_military") if pv else None   # military flag only known via P24

        # cross-validate ISSUE facts across sources
        canon = {_key(s) for s in bond["schedule"]}
        for nm, src in (("inzhur", iz), ("p24", pv)):
            if o and src:
                if src.get("maturity_date") and o["maturity_date"] and src["maturity_date"] != o["maturity_date"]:
                    warnings.append(f"{isin}: maturity mismatch ovdp={o['maturity_date']} {nm}={src['maturity_date']}")
                if src.get("currency") and src["currency"] != o["currency"]:
                    warnings.append(f"{isin}: currency mismatch ovdp={o['currency']} {nm}={src['currency']}")
        if o and iz and o.get("coupon_amount") and iz.get("coupon_amount") \
                and abs(o["coupon_amount"] - iz["coupon_amount"]) > 0.01:
            warnings.append(f"{isin}: coupon mismatch ovdp={o['coupon_amount']} inzhur={iz['coupon_amount']} — DATA ERROR")
        for s in (iz.get("schedule_future", []) if iz else []):
            if canon and _key(s) not in canon:
                warnings.append(f"{isin}: Inzhur payment {s['date']} {s['amount']} not in ovdp schedule — check staleness")

        # where_to_buy: prices ONLY from broker pages; Sense from ovdp column as enrichment
        wtb: list[dict] = []
        if iz and iz.get("buy_price") is not None:
            wtb.append({"broker": "Inzhur", "price": iz["buy_price"], "yield_pct": iz.get("yield_actual_pct")}); cov_iz += 1
        if pv and pv.get("buy_price") is not None:
            wtb.append({"broker": "Приват24", "price": pv["buy_price"], "yield_pct": pv.get("yield_actual_pct")}); cov_p24 += 1
        if op:
            for w in op.get("extra_where", []):
                wtb.append({"broker": w["broker"], "price": w["price"]})
                if w["broker"] == "Sense": cov_sense += 1
        bond["where_to_buy"] = wtb
        bonds.append(bond)

    bonds.sort(key=lambda b: b.get("maturity_date") or "9999")

    logger.info("── reconcile summary ──")
    logger.info(f"  bonds: {len(bonds)}  (ovdp-metadata={n_meta}, broker-only={n_brokeronly})")
    logger.info(f"  price coverage: Inzhur={cov_iz}, Приват24={cov_p24}, Sense={cov_sense} of {len(bonds)}")
    if warnings:
        cats: dict[str, int] = {}
        for w in warnings:
            k = ("coupon mismatch" if "coupon mismatch" in w else
                 "maturity mismatch" if "maturity mismatch" in w else
                 "currency mismatch" if "currency mismatch" in w else
                 "staleness" if "not in ovdp schedule" in w else
                 "broker-only" if "no ovdp metadata" in w else "other")
            cats[k] = cats.get(k, 0) + 1
        logger.info(f"  warnings: {len(warnings)} → " + ", ".join(f"{k}={v}" for k, v in sorted(cats.items())))
    return bonds, warnings


# ══ main ═════════════════════════════════════════════════════════════════════

async def run(out_path: Path, headless: bool, timeout: int, isins_filter: list[str] | None,
              concurrency: int, p24_do_schedules: bool, inzhur_dump: bool) -> None:
    t_run = time.perf_counter()
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless)
        ctx = await browser.new_context(locale="uk-UA",
                                        extra_http_headers={"Accept-Language": "uk-UA,uk;q=0.9"}, viewport={"width": 1280, "height": 900})
        logger.info(f"browser up (headless={headless})")

        if inzhur_dump:
            await inzhur_provider(ctx, timeout, dump=True)
            await ctx.close(); await browser.close(); return

        # 1 — brokers in parallel = the buyable universe
        try:
            p24, inzhur = await asyncio.gather(privat24_list_provider(ctx, timeout),
                                               inzhur_provider(ctx, timeout))
        except Exception:
            logger.error(f"broker stage crashed:\n{traceback.format_exc()}"); p24, inzhur = {}, {}

        universe = sorted(set(p24) | set(inzhur))
        if isins_filter:
            universe = [i for i in universe if i in isins_filter]
        logger.info(f"universe = P24({len(p24)}) ∪ Inzhur({len(inzhur)}) = {len(universe)} ISINs")
        if not universe:
            logger.error("empty universe — both brokers returned nothing; aborting")

        # 2 — ovdp metadata for the universe (per-ISIN details + /prices once), in parallel
        ovdp, ovdp_px = {}, {}
        try:
            ovdp_px, ovdp = await asyncio.gather(ovdp_prices(ctx, timeout),
                                                 ovdp_details(ctx, timeout, universe, concurrency))
        except Exception:
            logger.error(f"ovdp stage crashed:\n{traceback.format_exc()}")

        # 3 — optional P24 card schedules (valuable for broker-only/FX bonds)
        p24_sched: dict[str, list] = {}
        if p24_do_schedules:
            targets = [i for i in universe if i in p24]
            logger.info(f"p24: fetching {len(targets)} card schedules (concurrency={concurrency})")
            try:
                p24_sched = await privat24_schedules(ctx, targets, timeout, concurrency)
            except Exception:
                logger.error(f"p24 schedules stage crashed:\n{traceback.format_exc()}")

        await ctx.close(); await browser.close()

    bonds, warnings = reconcile(universe, ovdp, ovdp_px, inzhur, p24, p24_sched)
    output = {"parsed_at": now_iso(),
              "sources": {"ovdp": PRICES_URL, "inzhur": INZHUR_URL, "privat24": P24_LIST},
              "total_bonds": len(bonds), "warnings": warnings, "bonds": bonds}
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")

    logger.info("── run summary ──")
    logger.info(f"  brokers: p24={len(p24)}, inzhur={len(inzhur)}  ovdp-meta={len(ovdp)}"
                + (f"  p24_sched={len(p24_sched)}" if p24_do_schedules else ""))
    logger.success(f"  wrote {len(bonds)} bonds → {out_path}  (total {_ms(t_run)})")
    if warnings:
        logger.warning(f"  {len(warnings)} data-quality note(s):")
        for w in warnings:
            logger.warning(f"    • {w}")

def main() -> None:
    ap = argparse.ArgumentParser(description="OVDP scraper (broker = source of truth) → bonds.json (v4)")
    ap.add_argument("--out", "-o", default="bonds.json")
    ap.add_argument("--headless", default="true", choices=["true", "false"])
    ap.add_argument("--timeout", "-t", type=int, default=25)
    ap.add_argument("--concurrency", "-c", type=int, default=4)
    ap.add_argument("--isins", nargs="*")
    ap.add_argument("--p24-schedules", action="store_true",
                    help="navigate P24 cards for future schedules (fills FX/broker-only bonds)")
    ap.add_argument("--inzhur-dump", action="store_true",
                    help="dump raw Inzhur HTML + inner_text and exit (to lock the parser)")
    ap.add_argument("--log-level", default="INFO", choices=["DEBUG","INFO","WARNING","ERROR"])
    ap.add_argument("--log-file", default="scraper.log")
    ap.add_argument("--no-console-logs", default="false", choices=["true", "false"])
    args = ap.parse_args()

    logger.remove()
    if args.no_console_logs == "true":
        logger.add(sys.stderr, level=args.log_level, colorize=True,
                   format="<green>{time:HH:mm:ss}</green> | <level>{level:<7}</level> | {message}")

    logger.add(args.log_file, rotation="10 MB", retention=5, level="DEBUG", encoding="utf-8",
               format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level:<7} | {name}:{function}:{line} | {message}")

    try:
        logger.info("OVDP scraper starting")
        logger.info("config: " + ", ".join(f"{k}={v}" for k, v in vars(args).items()))

        asyncio.run(run(Path(args.out), args.headless == "true", args.timeout,
                        args.isins or None, args.concurrency, args.p24_schedules, args.inzhur_dump))
    except KeyboardInterrupt:
        logger.warning("interrupted by user")
    except Exception:
        logger.critical(f"unhandled crash:\n{traceback.format_exc()}"); sys.exit(1)

if __name__ == "__main__":
    main()
