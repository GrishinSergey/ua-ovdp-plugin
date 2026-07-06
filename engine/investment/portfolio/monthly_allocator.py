"""
Portfolio Engine — Monthly Income Allocator
============================================
Mode: "monthly_income"

Мета: забезпечити рівномірний дохід >= monthly_target в кожному місяці горизонту.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP, ROUND_CEILING
from typing import Optional

from engine.investment.domain.models import Bond
from engine.investment.forecast.math_core import bond_horizon_result, BondHorizonResult, _months_between
from engine.investment.portfolio.result_models import AllocationItem, MonthlyPortfolioResult

_OUT  = Decimal("0.01")
_PREC = Decimal("0.000001")

_MAX_ITERATIONS_PER_MONTH = 10_000


def build_monthly_income_portfolio(
        bonds: list[Bond],
        target_income: Decimal,
        settlement_date: date,
        horizon_days: int,
        broker: Optional[str] = None,
) -> MonthlyPortfolioResult:
    horizon_date = settlement_date + timedelta(days=horizon_days)
    warnings: list[str] = []

    candidates: list[BondHorizonResult] = []

    for bond in bonds:
        result = bond_horizon_result(bond, settlement_date, horizon_date, broker)
        if result.is_valid:
            candidates.append(result)
        else:
            warnings.append(
                f"{bond.isin}: виключено "
                f"(немає купонів у горизонті або real_income <= 0)."
            )

    if not candidates:
        return _empty_result(
            settlement_date, horizon_days, horizon_date, target_income, broker,
            warnings + ["Немає валідних бондів для побудови портфеля."]
        )

    n_months = _months_between(settlement_date, horizon_date) + 1
    monthly_target = (target_income / Decimal(n_months)).quantize(_OUT, rounding=ROUND_HALF_UP)

    month_bonds: dict[int, list[BondHorizonResult]] = {}
    for m in range(n_months):
        paying = [
            c for c in candidates
            if m < len(c.monthly_cashflow) and c.monthly_cashflow[m] > Decimal("0")
        ]
        paying.sort(key=lambda r: r.efficiency, reverse=True)
        month_bonds[m] = paying

    units: dict[str, int] = {c.bond.isin: 0 for c in candidates}
    monthly_income: list[Decimal] = [Decimal("0")] * n_months

    for m in range(n_months):
        if not month_bonds[m]:
            if m > 0:
                warnings.append(
                    f"Місяць {m}: жоден бонд не платить в цьому місяці. "
                    f"Покриття неможливе."
                )
            continue

        iterations = 0
        while monthly_income[m] < monthly_target:
            if iterations >= _MAX_ITERATIONS_PER_MONTH:
                warnings.append(
                    f"Місяць {m}: досягнуто ліміт ітерацій ({_MAX_ITERATIONS_PER_MONTH}). "
                    f"Покриття: {monthly_income[m]:.2f} / {monthly_target:.2f} UAH."
                )
                break

            best = month_bonds[m][0]
            isin = best.bond.isin
            units[isin] += 1

            for idx, cf_amount in enumerate(best.monthly_cashflow):
                if idx < n_months:
                    monthly_income[idx] += cf_amount

            iterations += 1

    candidate_by_isin: dict[str, BondHorizonResult] = {c.bond.isin: c for c in candidates}

    allocations: list[AllocationItem] = []
    for isin, n_units in units.items():
        if n_units == 0:
            continue

        c = candidate_by_isin[isin]
        total_income     = (c.real_income * n_units).quantize(_OUT, rounding=ROUND_HALF_UP)
        total_investment = (c.entry.dirty_price * n_units).quantize(_OUT, rounding=ROUND_HALF_UP)

        allocations.append(AllocationItem(
            bond=c.bond,
            units=n_units,
            investment_per_unit=c.entry.dirty_price,
            total_investment=total_investment,
            income_per_unit=c.real_income.quantize(_OUT),
            total_income=total_income,
            efficiency=c.efficiency,
            accrued_interest_per_unit=c.entry.accrued_interest,
            coupon_dates_in_horizon=[cp.payment_date for cp in c.coupons_in_horizon],
        ))

    allocations.sort(key=lambda a: a.efficiency, reverse=True)

    total_investment = sum((a.total_investment for a in allocations), Decimal("0"))
    total_income     = sum((a.total_income     for a in allocations), Decimal("0"))

    real_yield = Decimal("0")
    if total_investment > Decimal("0"):
        real_yield = (total_income / total_investment).quantize(_PREC, rounding=ROUND_HALF_UP)

    income_gap     = (total_income - target_income).quantize(_OUT)
    target_reached = income_gap >= Decimal("0")

    if not target_reached:
        warnings.append(
            f"Сумарна ціль не досягнута: бракує {abs(income_gap):.2f} UAH."
        )

    underfunded = [
        m for m in range(1, n_months)
        if monthly_income[m] < monthly_target
    ]
    if underfunded:
        warnings.append(
            f"Місяці без повного покриття: {underfunded}."
        )

    monthly_summary = [v.quantize(_OUT, rounding=ROUND_HALF_UP) for v in monthly_income]

    return MonthlyPortfolioResult(
        mode="monthly_income",
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
        monthly_summary=monthly_summary,
        monthly_target=monthly_target,
        underfunded_months=underfunded,
    )


def _empty_result(
        settlement_date: date,
        horizon_days: int,
        horizon_date: date,
        target_income: Decimal,
        broker: Optional[str],
        warnings: list[str],
) -> MonthlyPortfolioResult:
    return MonthlyPortfolioResult(
        mode="monthly_income",
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
        monthly_summary=[],
        monthly_target=Decimal("0"),
        underfunded_months=[],
    )