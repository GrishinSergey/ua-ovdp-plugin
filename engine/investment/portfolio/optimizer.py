"""
Portfolio Engine — Max Efficiency Optimizer
============================================
Mode: "max_efficiency"

Алгоритм: жадібний (greedy) по метриці efficiency.
    efficiency = real_income / investment_cost
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP, ROUND_CEILING
from typing import Optional

from engine.investment.domain.models import Bond
from engine.investment.forecast.math_core import bond_horizon_result, BondHorizonResult
from engine.investment.portfolio.result_models import AllocationItem, PortfolioResult

_OUT = Decimal("0.01")
_PREC = Decimal("0.000001")


def build_max_efficiency_portfolio(
        bonds: list[Bond],
        target_income: Decimal,
        settlement_date: date,
        horizon_days: int,
        broker: Optional[str] = None,
) -> PortfolioResult:
    horizon_date = settlement_date + timedelta(days=horizon_days)
    warnings: list[str] = []

    candidates: list[BondHorizonResult] = []

    for bond in bonds:
        result = bond_horizon_result(bond, settlement_date, horizon_date, broker)
        if result.is_valid:
            candidates.append(result)
        else:
            warnings.append(
                f"{bond.isin}: виключено з розрахунку "
                f"(немає купонів у горизонті або real_income <= 0)."
            )

    if not candidates:
        return _empty_result(
            settlement_date, horizon_days, horizon_date, target_income, broker,
            warnings + ["Немає валідних бондів для побудови портфеля."]
        )

    candidates.sort(key=lambda r: r.efficiency, reverse=True)

    remaining_income = target_income
    allocations: list[AllocationItem] = []

    for candidate in candidates:
        if remaining_income <= Decimal("0"):
            break

        units_needed = _ceil_divide(remaining_income, candidate.real_income)

        total_income     = (candidate.real_income * units_needed).quantize(_OUT, rounding=ROUND_HALF_UP)
        total_investment = (candidate.entry.dirty_price * units_needed).quantize(_OUT, rounding=ROUND_HALF_UP)

        allocations.append(AllocationItem(
            bond=candidate.bond,
            units=units_needed,
            investment_per_unit=candidate.entry.dirty_price,
            total_investment=total_investment,
            income_per_unit=candidate.real_income.quantize(_OUT),
            total_income=total_income,
            efficiency=candidate.efficiency,
            accrued_interest_per_unit=candidate.entry.accrued_interest,
            coupon_dates_in_horizon=[c.payment_date for c in candidate.coupons_in_horizon],
        ))

        remaining_income -= total_income

    total_investment = sum((a.total_investment for a in allocations), Decimal("0"))
    total_income     = sum((a.total_income     for a in allocations), Decimal("0"))

    real_yield = Decimal("0")
    if total_investment > Decimal("0"):
        real_yield = (total_income / total_investment).quantize(_PREC, rounding=ROUND_HALF_UP)

    income_gap     = (total_income - target_income).quantize(_OUT)
    target_reached = income_gap >= Decimal("0")

    if not target_reached:
        warnings.append(
            f"Ціль не досягнута: бракує {abs(income_gap):.2f} UAH. "
            f"Недостатньо бондів або горизонт занадто короткий."
        )

    return PortfolioResult(
        mode="max_efficiency",
        settlement_date=settlement_date,
        horizon_days=horizon_days,
        horizon_date=horizon_date,
        target_income=target_income,
        broker=broker,
        allocations=allocations,
        total_investment=total_investment.quantize(_OUT),
        total_income=total_income.quantize(_OUT),
        real_yield=real_yield,
        target_reached=target_reached,
        income_gap=income_gap,
        warnings=warnings,
    )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _ceil_divide(numerator: Decimal, denominator: Decimal) -> int:
    if denominator <= Decimal("0"):
        return 1
    result = (numerator / denominator).quantize(Decimal("1"), rounding=ROUND_CEILING)
    return max(1, int(result))


def _empty_result(
        settlement_date: date,
        horizon_days: int,
        horizon_date: date,
        target_income: Decimal,
        broker: Optional[str],
        warnings: list[str],
) -> PortfolioResult:
    return PortfolioResult(
        mode="max_efficiency",
        settlement_date=settlement_date,
        horizon_days=horizon_days,
        horizon_date=horizon_date,
        target_income=target_income,
        broker=broker,
        allocations=[],
        total_investment=Decimal("0"),
        total_income=Decimal("0"),
        real_yield=Decimal("0"),
        target_reached=False,
        income_gap=-target_income,
        warnings=warnings,
    )