"""
Portfolio Engine — Simulator
=============================
Mode: "max_efficiency_reinvest"

Симуляція життя портфеля з реінвестуванням cashflows.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP, ROUND_FLOOR
from typing import Optional

from engine.investment.domain.models import Bond
from engine.investment.forecast.math_core import (
    accrued_interest, coupons_in_horizon,
    EntryPrice,
)
from engine.investment.portfolio.optimizer import build_max_efficiency_portfolio
from engine.investment.portfolio.result_models import (
    SimulationEvent, ChainStep,
    ReinvestmentChain, SimulationResult,
)

_OUT  = Decimal("0.01")
_PREC = Decimal("0.000001")
FACE_VALUE = Decimal("1000")


def simulate_portfolio(
        bonds: list[Bond],
        target_income: Decimal,
        settlement_date: date,
        horizon_days: int,
        broker: Optional[str] = None,
) -> SimulationResult:
    horizon_date = settlement_date + timedelta(days=horizon_days)
    warnings: list[str] = []
    events: list[SimulationEvent] = []

    initial_result = build_max_efficiency_portfolio(
        bonds=bonds,
        target_income=target_income,
        settlement_date=settlement_date,
        horizon_days=horizon_days,
        broker=broker,
    )
    warnings.extend(initial_result.warnings)

    initial_allocations = initial_result.allocations
    initial_investment  = initial_result.total_investment

    if not initial_allocations:
        return _empty_result(
            settlement_date, horizon_days, horizon_date,
            target_income, broker,
            warnings + ["Не вдалось побудувати початковий портфель."]
        )

    cashflow_schedule: dict[date, list[tuple[str, str, Decimal, str]]] = defaultdict(list)

    for alloc in initial_allocations:
        bond = alloc.bond
        for cp in bond.coupon_schedule:
            if settlement_date < cp.payment_date:
                cashflow_schedule[cp.payment_date].append((
                    bond.isin, bond.name,
                    cp.amount_per_bond * alloc.units,
                    "coupon",
                ))
        if bond.maturity_date > settlement_date:
            cashflow_schedule[bond.maturity_date].append((
                bond.isin, bond.name,
                FACE_VALUE * alloc.units,
                "maturity",
            ))

    cash_pool             = Decimal("0")
    total_coupon_income   = Decimal("0")
    total_maturity_income = Decimal("0")

    total_ai_paid = Decimal("0")
    for alloc in initial_allocations:
        total_ai_paid += alloc.accrued_interest_per_unit * alloc.units

    chains: dict[str, ReinvestmentChain] = {}
    for alloc in initial_allocations:
        step = ChainStep(
            bond=alloc.bond,
            units=alloc.units,
            buy_date=settlement_date,
            dirty_price_per_unit=alloc.investment_per_unit,
            total_invested=alloc.total_investment,
            source="initial",
            reinvested_from_isin=None,
            horizon_at_buy=horizon_date,
            expected_income=alloc.total_income,
        )
        chains[alloc.bond.isin] = ReinvestmentChain(steps=[step])

    active_reinvest_cashflows: dict[date, list[tuple[str, str, Decimal, str]]] = defaultdict(list)

    current = _month_start(settlement_date)

    while current <= horizon_date:
        month_end = _month_end(current)

        for cf_date in sorted(cashflow_schedule.keys()):
            if current <= cf_date <= month_end:
                for isin, name, amount, kind in cashflow_schedule[cf_date]:
                    cash_pool += amount
                    if kind == "coupon":
                        total_coupon_income += amount
                    else:
                        total_maturity_income += amount

                    events.append(SimulationEvent(
                        event_date=cf_date,
                        kind=kind,
                        isin=isin,
                        bond_name=name,
                        amount=amount,
                        cash_pool_after=cash_pool,
                        note=f"{'Купон' if kind == 'coupon' else 'Погашення'}: ₴{amount:,.2f}",
                    ))

        for cf_date in sorted(active_reinvest_cashflows.keys()):
            if current <= cf_date <= month_end:
                for isin, name, amount, kind in active_reinvest_cashflows[cf_date]:
                    cash_pool += amount
                    if kind == "coupon":
                        total_coupon_income += amount
                    else:
                        total_maturity_income += amount

                    events.append(SimulationEvent(
                        event_date=cf_date,
                        kind=kind,
                        isin=isin,
                        bond_name=name,
                        amount=amount,
                        cash_pool_after=cash_pool,
                        note=f"[реінвест] {'Купон' if kind == 'coupon' else 'Погашення'}: ₴{amount:,.2f}",
                    ))

        reinvest_date = min(month_end, horizon_date)

        if cash_pool > Decimal("0") and reinvest_date < horizon_date:
            best = _find_best_bond(
                bonds=bonds,
                buy_date=reinvest_date,
                horizon_date=horizon_date,
                cash_available=cash_pool,
            )

            if best is not None:
                bond, units, ep, income_per_unit = best
                cost = (ep.dirty_price * units).quantize(_OUT)

                if cost <= cash_pool:
                    cash_pool  -= cost
                    total_ai_paid += (ep.accrued_interest * units).quantize(_OUT)

                    for cp in bond.coupon_schedule:
                        if reinvest_date < cp.payment_date:
                            active_reinvest_cashflows[cp.payment_date].append((
                                bond.isin, bond.name,
                                cp.amount_per_bond * units,
                                "coupon",
                            ))
                    if bond.maturity_date > reinvest_date:
                        active_reinvest_cashflows[bond.maturity_date].append((
                            bond.isin, bond.name,
                            FACE_VALUE * units,
                            "maturity",
                        ))

                    parent_chain = _find_parent_chain(chains)
                    step = ChainStep(
                        bond=bond,
                        units=units,
                        buy_date=reinvest_date,
                        dirty_price_per_unit=ep.dirty_price,
                        total_invested=cost,
                        source="reinvest",
                        reinvested_from_isin=parent_chain.origin_isin if parent_chain else None,
                        horizon_at_buy=horizon_date,
                        expected_income=(income_per_unit * units).quantize(_OUT),
                    )
                    if parent_chain:
                        parent_chain.steps.append(step)
                    else:
                        chains[bond.isin] = ReinvestmentChain(steps=[step])

                    events.append(SimulationEvent(
                        event_date=reinvest_date,
                        kind="reinvest",
                        isin=bond.isin,
                        bond_name=bond.name,
                        amount=cost,
                        units=units,
                        cash_pool_after=cash_pool,
                        note=(
                            f"Реінвестування: {units} шт × ₴{ep.dirty_price:,.2f}"
                            f" = ₴{cost:,.2f}  |  залишок cash: ₴{cash_pool:,.2f}"
                        ),
                    ))

            else:
                events.append(SimulationEvent(
                    event_date=reinvest_date,
                    kind="cash_carry",
                    isin="—",
                    bond_name="—",
                    amount=cash_pool,
                    cash_pool_after=cash_pool,
                    note="Cash чекає: немає підходящих бондів для реінвестування",
                ))

        current = _next_month(current)

    residual_cash = cash_pool.quantize(_OUT)
    total_ai_paid = total_ai_paid.quantize(_OUT)

    total_income = (total_coupon_income - total_ai_paid + residual_cash).quantize(_OUT)

    real_yield = Decimal("0")
    if initial_investment > Decimal("0"):
        real_yield = (total_income / initial_investment).quantize(_PREC, rounding=ROUND_HALF_UP)

    income_gap     = (total_income - target_income).quantize(_OUT)
    target_reached = income_gap >= Decimal("0")

    if not target_reached:
        warnings.append(
            f"Ціль не досягнута: бракує ₴{abs(income_gap):,.2f}."
        )

    return SimulationResult(
        mode="max_efficiency_reinvest",
        settlement_date=settlement_date,
        horizon_days=horizon_days,
        horizon_date=horizon_date,
        target_income=target_income,
        broker=broker,
        initial_allocations=initial_allocations,
        initial_investment=initial_investment.quantize(_OUT),
        chains=list(chains.values()),
        events=sorted(events, key=lambda e: e.event_date),
        total_coupon_income=total_coupon_income.quantize(_OUT),
        total_maturity_income=total_maturity_income.quantize(_OUT),
        residual_cash=residual_cash,
        total_income=total_income,
        real_yield=real_yield,
        target_reached=target_reached,
        income_gap=income_gap,
        warnings=warnings,
    )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _find_best_bond(
        bonds: list[Bond],
        buy_date: date,
        horizon_date: date,
        cash_available: Decimal,
) -> Optional[tuple[Bond, int, EntryPrice, Decimal]]:
    best_bond       = None
    best_units      = 0
    best_efficiency = Decimal("0")
    best_ep         = None
    best_income     = Decimal("0")

    for bond in bonds:
        if bond.maturity_date <= buy_date:
            continue

        ai    = accrued_interest(bond, buy_date)
        dirty = (FACE_VALUE + ai.amount).quantize(_OUT)

        if dirty <= Decimal("0"):
            continue

        units = int((cash_available / dirty).to_integral_value(rounding=ROUND_FLOOR))
        if units < 1:
            continue

        coupons = coupons_in_horizon(bond, buy_date, horizon_date)
        if not coupons:
            continue

        total_c = sum(c.amount for c in coupons)
        income  = (total_c - ai.amount).quantize(_PREC)

        if income <= Decimal("0"):
            continue

        eff = (income / dirty).quantize(_PREC)

        if eff > best_efficiency:
            best_efficiency = eff
            best_bond       = bond
            best_units      = units
            best_ep         = EntryPrice(
                dirty_price=dirty,
                accrued_interest=ai.amount.quantize(_OUT),
                clean_price=FACE_VALUE,
            )
            best_income = income

    if best_bond is None:
        return None

    return best_bond, best_units, best_ep, best_income


def _find_parent_chain(
        chains: dict[str, ReinvestmentChain],
) -> Optional[ReinvestmentChain]:
    if not chains:
        return None
    return max(chains.values(), key=lambda ch: ch.total_initial_invested)


def _month_start(d: date) -> date:
    return d.replace(day=1)


def _month_end(d: date) -> date:
    y = d.year + (d.month // 12)
    m = d.month % 12 + 1
    return date(y, m, 1) - timedelta(days=1)


def _next_month(d: date) -> date:
    y = d.year + (d.month // 12)
    m = d.month % 12 + 1
    return date(y, m, 1)


def _empty_result(
        settlement_date: date,
        horizon_days: int,
        horizon_date: date,
        target_income: Decimal,
        broker: Optional[str],
        warnings: list[str],
) -> SimulationResult:
    return SimulationResult(
        mode="max_efficiency_reinvest",
        settlement_date=settlement_date,
        horizon_days=horizon_days,
        horizon_date=horizon_date,
        target_income=target_income,
        broker=broker,
        initial_allocations=[],
        initial_investment=Decimal("0"),
        chains=[],
        events=[],
        total_coupon_income=Decimal("0"),
        total_maturity_income=Decimal("0"),
        residual_cash=Decimal("0"),
        total_income=Decimal("0"),
        real_yield=Decimal("0"),
        target_reached=False,
        income_gap=-target_income,
        warnings=warnings,
    )