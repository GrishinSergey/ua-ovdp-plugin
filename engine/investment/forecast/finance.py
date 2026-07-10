"""
OVDP Portfolio Tracker — Фінансові розрахунки
Всі формули відповідають методології НБУ/Мінфін для ОВДП.

Day count convention: actual/365
Податки: ОВДП звільнені від ПДФО (ст. 165.1.52 ПКУ) → TAX_RATE = 0
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from engine.investment.domain.models import Bond, Position, Portfolio
from engine.investment.forecast.math_core import entry_price

# ── Константи ────────────────────────────────────────────────────────────────

DAY_COUNT_BASIS = Decimal("365")
TAX_RATE        = Decimal("0")      # ОВДП: ст. 165.1.52 ПКУ — без ПДФО
PRECISION       = Decimal("0.01")


# ── Result dataclasses ────────────────────────────────────────────────────────

@dataclass
class AccruedInterestResult:
    amount_per_bond: Decimal
    amount_total: Decimal
    days_accrued: int
    last_coupon_date: date
    next_coupon_date: Optional[date]
    days_to_next_coupon: Optional[int]


@dataclass
class CashflowItem:
    payment_date: date
    amount: Decimal
    amount_per_bond: Decimal
    kind: str                   # "coupon" | "maturity"
    isin: str
    bond_name: str
    currency: str


@dataclass
class PositionMetrics:
    position: Position
    as_of: date

    # ── Вкладення ──
    total_invested: Decimal
    clean_price_total: Decimal

    # ── НКД ──
    accrued_interest: AccruedInterestResult

    # ── Realized (фактично вже отримано) ──
    realized_income: Decimal
    realized_coupons_count: int
    realized_maturity: Decimal

    # ── Future cashflows (ще попереду) ──
    future_cashflows: list[CashflowItem]
    expected_coupons_total: Decimal
    expected_maturity_amount: Decimal
    total_expected_gross: Decimal
    total_expected_net: Decimal

    # ── P&L ──
    realized_profit: Decimal
    total_expected_profit_gross: Decimal
    total_expected_profit_net: Decimal
    total_profit: Decimal

    # ── Дохідність ──
    ytm_gross: Optional[float]
    ytm_net: Optional[float]
    simple_yield: Optional[float]

    # ── Duration / Convexity ──
    modified_duration: Optional[float]
    convexity: Optional[float]
    dv01: Optional[float]

    # ── Ринкова оцінка ──
    market_value: Optional[Decimal]
    unrealized_pnl: Optional[Decimal]
    unrealized_pnl_pct: Optional[float]
    current_ytm: Optional[float]

    # ── Статус ──
    is_matured: bool
    days_to_maturity: Optional[int]


@dataclass
class MonthlyForecast:
    year_month: str
    total_amount: Decimal
    currency_breakdown: dict[str, Decimal]
    items: list[CashflowItem]


@dataclass
class PortfolioMetrics:
    portfolio: Portfolio
    as_of: date
    position_metrics: list[PositionMetrics]

    total_invested: Decimal
    total_accrued_interest: Decimal

    # Realized
    total_realized_income: Decimal
    total_realized_profit: Decimal

    # Future
    total_expected_gross: Decimal
    total_expected_net: Decimal
    total_expected_profit_gross: Decimal
    total_expected_profit_net: Decimal

    # Total
    total_profit: Decimal

    # Market
    total_market_value: Optional[Decimal]
    total_unrealized_pnl: Optional[Decimal]

    # Yield
    avg_ytm_gross: Optional[float]
    avg_ytm_net: Optional[float]
    avg_modified_duration: Optional[float]

    # Breakdowns
    by_currency: dict[str, Decimal]
    by_maturity_bucket: dict[str, Decimal]

    # Cashflow
    monthly_forecast: list[MonthlyForecast]
    all_future_cashflows: list[CashflowItem]


# ── Core Calculations ─────────────────────────────────────────────────────────

def calculate_accrued_interest(
        bond: Bond,
        settlement_date: date,
        quantity: int = 1,
) -> AccruedInterestResult:
    """НКД = Номінал × Ставка × Дні_від_останнього_купону / 365"""
    coupon_dates = sorted(c.payment_date for c in bond.coupon_schedule)

    past   = [d for d in coupon_dates if d <= settlement_date]
    future = [d for d in coupon_dates if d > settlement_date]

    last_coupon_date = max(past) if past else bond.issue_date
    next_coupon_date = min(future) if future else None

    days_accrued = (settlement_date - last_coupon_date).days
    days_to_next = (next_coupon_date - settlement_date).days if next_coupon_date else None

    amount_per_bond = (
            bond.face_value * bond.coupon_rate * Decimal(days_accrued) / DAY_COUNT_BASIS
    ).quantize(PRECISION, rounding=ROUND_HALF_UP)

    return AccruedInterestResult(
        amount_per_bond=amount_per_bond,
        amount_total=amount_per_bond * quantity,
        days_accrued=days_accrued,
        last_coupon_date=last_coupon_date,
        next_coupon_date=next_coupon_date,
        days_to_next_coupon=days_to_next,
    )


def build_future_cashflows(
        bond: Bond,
        quantity: int,
        settlement_date: date,
) -> list[CashflowItem]:
    """Всі виплати ПІСЛЯ settlement_date."""
    items: list[CashflowItem] = []

    for c in bond.coupon_schedule:
        if c.payment_date > settlement_date:
            items.append(CashflowItem(
                payment_date=c.payment_date,
                amount=c.amount_per_bond * quantity,
                amount_per_bond=c.amount_per_bond,
                kind="coupon",
                isin=bond.isin,
                bond_name=bond.name,
                currency=bond.currency,
            ))

    if bond.maturity_date > settlement_date:
        items.append(CashflowItem(
            payment_date=bond.maturity_date,
            amount=bond.face_value * quantity,
            amount_per_bond=bond.face_value,
            kind="maturity",
            isin=bond.isin,
            bond_name=bond.name,
            currency=bond.currency,
        ))

    return sorted(items, key=lambda x: x.payment_date)


def calculate_ytm(
        dirty_price_per_bond: float,
        future_cashflows: list[CashflowItem],
        settlement_date: date,
) -> Optional[float]:
    try:
        from scipy.optimize import brentq
    except ImportError:
        return None

    cfs = [(cf.payment_date, float(cf.amount_per_bond)) for cf in future_cashflows]

    def price_error(r: float) -> float:
        pv = 0.0
        for cf_date, cf_amount in cfs:
            t = (cf_date - settlement_date).days / 365.0
            if t > 0:
                pv += cf_amount / ((1.0 + r) ** t)
        return pv - dirty_price_per_bond

    try:
        if price_error(-0.9) * price_error(50.0) > 0:
            return None
        return round(brentq(price_error, -0.9, 50.0, xtol=1e-10, maxiter=1000), 6)
    except (ValueError, RuntimeError):
        return None


def calculate_simple_yield(
        total_invested: float,
        total_all_cashflows: float,
        days_total: int,
) -> Optional[float]:
    if total_invested <= 0 or days_total <= 0:
        return None
    profit = total_all_cashflows - total_invested
    years  = days_total / 365.0
    return round((profit / total_invested) / years, 6)


def calculate_duration_convexity(
        ytm: float,
        dirty_price_per_bond: float,
        future_cashflows: list[CashflowItem],
        settlement_date: date,
) -> tuple[Optional[float], Optional[float], Optional[float]]:
    if ytm <= -1 or dirty_price_per_bond <= 0:
        return None, None, None

    mac_dur   = 0.0
    convexity = 0.0

    for cf in future_cashflows:
        t = (cf.payment_date - settlement_date).days / 365.0
        if t <= 0:
            continue
        pv     = float(cf.amount_per_bond) / ((1 + ytm) ** t)
        weight = pv / dirty_price_per_bond
        mac_dur   += t * weight
        convexity += t * (t + 1.0 / 365.0) * pv

    mod_dur   = mac_dur / (1 + ytm)
    convexity = convexity / (dirty_price_per_bond * (1 + ytm) ** 2)
    dv01      = mod_dur * dirty_price_per_bond / 10_000.0

    return round(mod_dur, 4), round(convexity, 4), round(dv01, 4)


# ── Position Metrics ──────────────────────────────────────────────────────────

def calculate_position_metrics(
        position: Position,
        as_of: Optional[date] = None,
) -> PositionMetrics:
    if as_of is None:
        as_of = date.today()

    bond = position.bond
    qty  = position.quantity
    is_matured        = bond.maturity_date <= as_of
    days_to_maturity  = (bond.maturity_date - as_of).days if not is_matured else 0

    ai = calculate_accrued_interest(bond, as_of, qty)

    realized_coupons_count = 0
    realized_income        = Decimal("0")
    realized_maturity      = Decimal("0")

    for rp in position.received_payments:
        realized_income += rp.amount
        if rp.kind == "coupon":
            realized_coupons_count += 1
        elif rp.kind == "maturity":
            realized_maturity += rp.amount

    ai_paid_total   = position.accrued_interest_paid * qty
    realized_profit = (realized_income - ai_paid_total).quantize(PRECISION)

    future_cfs   = build_future_cashflows(bond, qty, as_of)
    coupon_cfs   = [cf for cf in future_cfs if cf.kind == "coupon"]
    maturity_cfs = [cf for cf in future_cfs if cf.kind == "maturity"]

    expected_coupons  = sum((cf.amount for cf in coupon_cfs),   Decimal("0"))
    expected_maturity = sum((cf.amount for cf in maturity_cfs), Decimal("0"))
    total_gross       = expected_coupons + expected_maturity
    total_net         = total_gross

    total_invested       = position.total_invested
    remaining_invested   = (total_invested - realized_income).quantize(PRECISION)
    forward_profit       = (total_gross - remaining_invested).quantize(PRECISION)
    total_profit         = (realized_profit + forward_profit).quantize(PRECISION)

    ytm_gross  = None
    ytm_net    = None
    simple_yld = None
    mod_dur    = None
    conv       = None
    dv01       = None

    all_purchase_cfs = build_future_cashflows(bond, 1, position.purchase_date)
    if all_purchase_cfs:
        ytm_gross = calculate_ytm(
            dirty_price_per_bond=float(position.purchase_price_dirty),
            future_cashflows=all_purchase_cfs,
            settlement_date=position.purchase_date,
        )
        if ytm_gross is not None:
            ytm_net = round(float(ytm_gross) * (1 - float(TAX_RATE)), 6)

    all_purchase_cfs_full = build_future_cashflows(bond, qty, position.purchase_date)
    all_cashflow_total = float(
        sum((cf.amount for cf in all_purchase_cfs_full), Decimal("0"))
    )
    simple_yld = calculate_simple_yield(
        total_invested=float(total_invested),
        total_all_cashflows=all_cashflow_total,
        days_total=(bond.maturity_date - position.purchase_date).days,
    )

    if ytm_gross is not None and future_cfs:
        mod_dur, conv, dv01 = calculate_duration_convexity(
            ytm=ytm_gross,
            dirty_price_per_bond=float(position.purchase_price_dirty),
            future_cashflows=build_future_cashflows(bond, 1, as_of),
            settlement_date=as_of,
        )

    market_value       = None
    unrealized_pnl     = None
    unrealized_pnl_pct = None
    current_ytm        = None

    if bond.last_market_price is not None and not is_matured:
        # entry_price() is the canonical dirty<->clean conversion (math_core.py) — do not
        # add ai.amount_per_bond to bond.last_market_price here, it's already dirty
        # (domain/models.py). That used to double-count NKD and inflate market_value/
        # unrealized_pnl and skew current_ytm below.
        dirty_market   = entry_price(bond, as_of).dirty_price
        market_value   = (dirty_market * qty).quantize(PRECISION)
        unrealized_pnl = (market_value - total_invested).quantize(PRECISION)
        unrealized_pnl_pct = round(
            float(unrealized_pnl) / float(total_invested) * 100, 2
        )
        current_cfs = build_future_cashflows(bond, 1, as_of)
        if current_cfs:
            current_ytm = calculate_ytm(
                dirty_price_per_bond=float(dirty_market),
                future_cashflows=current_cfs,
                settlement_date=as_of,
            )

    return PositionMetrics(
        position=position,
        as_of=as_of,
        total_invested=total_invested.quantize(PRECISION),
        clean_price_total=((position.purchase_price_dirty - position.accrued_interest_paid) * qty).quantize(PRECISION),
        accrued_interest=ai,
        realized_income=realized_income.quantize(PRECISION),
        realized_coupons_count=realized_coupons_count,
        realized_maturity=realized_maturity.quantize(PRECISION),
        future_cashflows=future_cfs,
        expected_coupons_total=expected_coupons.quantize(PRECISION),
        expected_maturity_amount=expected_maturity.quantize(PRECISION),
        total_expected_gross=total_gross.quantize(PRECISION),
        total_expected_net=total_net.quantize(PRECISION),
        total_expected_profit_gross=forward_profit,
        total_expected_profit_net=forward_profit,
        realized_profit=realized_profit,
        total_profit=total_profit,
        ytm_gross=ytm_gross,
        ytm_net=ytm_net,
        simple_yield=simple_yld,
        modified_duration=mod_dur,
        convexity=conv,
        dv01=dv01,
        market_value=market_value,
        unrealized_pnl=unrealized_pnl,
        unrealized_pnl_pct=unrealized_pnl_pct,
        current_ytm=current_ytm,
        is_matured=is_matured,
        days_to_maturity=days_to_maturity,
    )


# ── Portfolio Metrics ─────────────────────────────────────────────────────────

def calculate_portfolio_metrics(
        portfolio: Portfolio,
        as_of: Optional[date] = None,
) -> PortfolioMetrics:
    if as_of is None:
        as_of = date.today()

    pm_list = [calculate_position_metrics(p, as_of) for p in portfolio.positions]

    total_invested      = sum((m.total_invested for m in pm_list), Decimal("0"))
    total_ai            = sum((m.accrued_interest.amount_total for m in pm_list), Decimal("0"))
    total_realized      = sum((m.realized_income for m in pm_list), Decimal("0"))
    total_realized_prof = sum((m.realized_profit for m in pm_list), Decimal("0"))
    total_gross         = sum((m.total_expected_gross for m in pm_list), Decimal("0"))
    total_net           = sum((m.total_expected_net for m in pm_list), Decimal("0"))
    profit_gross        = sum((m.total_expected_profit_gross for m in pm_list), Decimal("0"))
    profit_net          = sum((m.total_expected_profit_net for m in pm_list), Decimal("0"))
    total_profit        = sum((m.total_profit for m in pm_list), Decimal("0"))

    market_values = [m.market_value for m in pm_list if m.market_value is not None]
    total_market  = sum(market_values, Decimal("0")) if market_values else None
    total_pnl     = None
    if total_market is not None and len(market_values) == len(pm_list):
        total_pnl = (total_market - total_invested).quantize(PRECISION)

    ytm_items = [(m.ytm_gross, m.total_invested) for m in pm_list
                 if m.ytm_gross is not None]
    avg_ytm_gross = None
    avg_ytm_net   = None
    if ytm_items:
        total_w = sum(inv for _, inv in ytm_items)
        if total_w > 0:
            avg_ytm_gross = round(
                sum(ytm * float(inv) for ytm, inv in ytm_items) / float(total_w), 6
            )
            avg_ytm_net = round(avg_ytm_gross * (1 - float(TAX_RATE)), 6)

    dur_items = [(m.modified_duration, m.total_invested) for m in pm_list
                 if m.modified_duration is not None]
    avg_dur = None
    if dur_items:
        total_w = sum(inv for _, inv in dur_items)
        if total_w > 0:
            avg_dur = round(
                sum(d * float(inv) for d, inv in dur_items) / float(total_w), 4
            )

    by_currency: dict[str, Decimal] = defaultdict(Decimal)
    for m in pm_list:
        by_currency[m.position.bond.currency] += m.total_invested

    by_bucket: dict[str, Decimal] = defaultdict(Decimal)
    for m in pm_list:
        days = m.days_to_maturity or 0
        if days <= 365:   bucket = "< 1 рік"
        elif days <= 730:  bucket = "1–2 роки"
        elif days <= 1095: bucket = "2–3 роки"
        else:              bucket = "> 3 роки"
        by_bucket[bucket] += m.total_invested

    all_cfs: list[CashflowItem] = []
    for m in pm_list:
        all_cfs.extend(m.future_cashflows)
    all_cfs.sort(key=lambda x: x.payment_date)

    monthly: dict[str, dict] = defaultdict(
        lambda: {"total": Decimal("0"), "bycur": defaultdict(Decimal), "items": []}
    )
    for cf in all_cfs:
        ym = cf.payment_date.strftime("%Y-%m")
        monthly[ym]["total"] += cf.amount
        monthly[ym]["bycur"][cf.currency] += cf.amount
        monthly[ym]["items"].append(cf)

    monthly_forecast = [
        MonthlyForecast(
            year_month=ym,
            total_amount=data["total"].quantize(PRECISION),
            currency_breakdown=dict(data["bycur"]),
            items=data["items"],
        )
        for ym, data in sorted(monthly.items())
    ]

    return PortfolioMetrics(
        portfolio=portfolio,
        as_of=as_of,
        position_metrics=pm_list,
        total_invested=total_invested.quantize(PRECISION),
        total_accrued_interest=total_ai.quantize(PRECISION),
        total_realized_income=total_realized.quantize(PRECISION),
        total_realized_profit=total_realized_prof.quantize(PRECISION),
        total_expected_gross=total_gross.quantize(PRECISION),
        total_expected_net=total_net.quantize(PRECISION),
        total_expected_profit_gross=profit_gross.quantize(PRECISION),
        total_expected_profit_net=profit_net.quantize(PRECISION),
        total_profit=total_profit.quantize(PRECISION),
        total_market_value=total_market,
        total_unrealized_pnl=total_pnl,
        avg_ytm_gross=avg_ytm_gross,
        avg_ytm_net=avg_ytm_net,
        avg_modified_duration=avg_dur,
        by_currency=dict(by_currency),
        by_maturity_bucket=dict(by_bucket),
        monthly_forecast=monthly_forecast,
        all_future_cashflows=all_cfs,
    )


# ── Reinvestment Simulator ────────────────────────────────────────────────────

@dataclass
class ReinvestmentScenario:
    reinvest_rate: float
    years: int
    initial_invested: Decimal
    final_value: Decimal
    total_coupons_received: Decimal
    total_reinvestment_income: Decimal
    effective_annual_return: float

    # Baseline (reinvest_rate=0: coupons held as cash, not reinvested) — isolates the
    # portfolio's OWN return from the reinvestment assumption layered on top of it. See
    # simulate_reinvestment()'s docstring for why this pair exists.
    baseline_final_value: Decimal
    baseline_effective_annual_return: float


def simulate_reinvestment(
        pm: PortfolioMetrics,
        reinvest_rate: float = 0.15,
        years: int = 3,
) -> ReinvestmentScenario:
    """
    "What if I reinvest every coupon at reinvest_rate for `years`?" — projects the
    current portfolio's coupons forward with compounding reinvestment.

    effective_annual_return blends two distinct things: the return the CURRENT bonds
    themselves already generate (coupons + maturities vs. what was invested) and the
    EXTRA growth from the reinvest_rate assumption on top. A single number here can't be
    read as "how good is this portfolio" — a portfolio full of near-maturity bonds with a
    high reinvest_rate assumption looks similar to a strong long-duration portfolio with a
    conservative one. baseline_effective_annual_return isolates the first part (computed
    with reinvest_rate=0, i.e. coupons just held as cash) so the two are comparable
    side by side; the reinvestment assumption's own contribution is
    total_reinvestment_income (= final_value - baseline_final_value exactly).
    """
    as_of   = pm.as_of
    horizon = date(as_of.year + years, as_of.month, as_of.day)

    coupons_in_hor = [
        cf for cf in pm.all_future_cashflows
        if cf.kind == "coupon" and cf.payment_date <= horizon
    ]

    total_coupons       = sum(cf.amount for cf in coupons_in_hor)
    reinvestment_income = Decimal("0")

    for cf in coupons_in_hor:
        remaining_years = (horizon - cf.payment_date).days / 365.0
        fv = float(cf.amount) * ((1 + reinvest_rate) ** remaining_years)
        reinvestment_income += Decimal(str(round(fv - float(cf.amount), 2)))

    maturities = sum(
        cf.amount for cf in pm.all_future_cashflows
        if cf.kind == "maturity" and cf.payment_date <= horizon
    )

    final_value          = (total_coupons + reinvestment_income + maturities).quantize(PRECISION)
    baseline_final_value = (total_coupons + maturities).quantize(PRECISION)
    invested             = pm.total_invested

    def _cagr(value: Decimal) -> float:
        if float(invested) <= 0 or years <= 0:
            return 0.0
        return round((float(value) / float(invested)) ** (1.0 / years) - 1, 6)

    return ReinvestmentScenario(
        reinvest_rate=reinvest_rate,
        years=years,
        initial_invested=invested,
        final_value=final_value,
        total_coupons_received=total_coupons.quantize(PRECISION),
        total_reinvestment_income=reinvestment_income.quantize(PRECISION),
        effective_annual_return=_cagr(final_value),
        baseline_final_value=baseline_final_value,
        baseline_effective_annual_return=_cagr(baseline_final_value),
    )