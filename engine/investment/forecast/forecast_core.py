"""
OVDP Forecast — Bond Comparison Engine
Порівнює 2-10 облігацій за заданий горизонт інвестування.
"""

from __future__ import annotations

from dataclasses import field, dataclass
from datetime import date
from decimal import Decimal
from typing import Optional

from engine.investment.forecast.strategy import ProfitCalculationStrategy, StandardStrategy
from engine.investment.forecast.finance import (
    calculate_accrued_interest,
    build_future_cashflows,
    calculate_ytm,
    CashflowItem,
    PRECISION,
)
from engine.investment.forecast.math_core import entry_price
from engine.investment.domain.models import Bond

_PRECISION = PRECISION


# ── Result dataclasses ────────────────────────────────────────────────────────

@dataclass
class BondForecastItem:
    bond: Bond
    horizon_date: date
    dirty_entry_price: Decimal
    accrued_interest_per_bond: Decimal
    actual_invested: Decimal
    coupon_cashflows: list[CashflowItem]
    coupons_total: Decimal
    coupons_per_bond: Decimal
    includes_maturity: bool
    maturity_amount: Decimal
    is_sold_at_horizon: bool
    sale_proceeds: Decimal
    total_profit: Decimal
    total_profit_per_bond: Decimal
    effective_return_for_horizon: float
    effective_annual_return: float
    ytm_to_maturity: Optional[float]
    strategy_name: str = "Standard"
    rank: int = 0


@dataclass
class BondComparisonResult:
    as_of: date
    quantity: int
    compare_by_full_period: bool
    horizon_date: Optional[date]
    horizon_months: Optional[int]
    max_allowed_horizon_months: int
    strategy: ProfitCalculationStrategy
    broker: Optional[str]
    items: list[BondForecastItem]
    warnings: list[str] = field(default_factory=list)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _add_months(d: date, months: int) -> date:
    month = d.month - 1 + months
    year  = d.year + month // 12
    month = month % 12 + 1
    day   = min(d.day, _days_in_month(year, month))
    return date(year, month, day)


def _days_in_month(year: int, month: int) -> int:
    if month == 12:
        return (date(year + 1, 1, 1) - date(year, 12, 1)).days
    return (date(year, month + 1, 1) - date(year, month, 1)).days


# ── Core ──────────────────────────────────────────────────────────────────────

def compare_bonds(
        bonds: list[Bond],
        quantity: int = 1,
        as_of: Optional[date] = None,
        horizon_months: Optional[int] = None,
        compare_by_full_period: bool = False,
        broker: Optional[str] = None,
        strategy: Optional[ProfitCalculationStrategy] = None,
) -> BondComparisonResult:
    if as_of is None:
        as_of = date.today()

    if strategy is None:
        strategy = StandardStrategy()

    if quantity < 1:
        raise ValueError(f"quantity має бути >= 1, отримано: {quantity}.")

    warnings: list[str] = []

    active_bonds: list[Bond] = []
    for b in bonds:
        if b.maturity_date <= as_of:
            warnings.append(f"{b.isin} ({b.name}): вже погашений, виключено з порівняння.")
        else:
            active_bonds.append(b)

    if len(active_bonds) < 2:
        raise ValueError(
            "Для порівняння потрібно мінімум 2 непогашені облігації. "
            f"Отримано активних: {len(active_bonds)}."
        )

    min_days_to_maturity = min((b.maturity_date - as_of).days for b in active_bonds)
    min_maturity_date    = min(b.maturity_date for b in active_bonds)
    max_allowed_months   = max(1, int(min_days_to_maturity * 12 / 365))

    if compare_by_full_period:
        shared_horizon_date   = None
        shared_horizon_months = None

        if horizon_months is not None:
            warnings.append(
                "compare_by_full_period=True: параметр horizon_months ігнорується."
            )
    else:
        if horizon_months is None:
            shared_horizon_date   = min_maturity_date
            shared_horizon_months = round(min_days_to_maturity * 12 / 365, 1)
        else:
            if horizon_months > max_allowed_months:
                warnings.append(
                    f"Горизонт {horizon_months} міс. перевищує максимально допустимий "
                    f"{max_allowed_months} міс. Автоматично зменшено."
                )
                horizon_months = max_allowed_months
            shared_horizon_date   = _add_months(as_of, horizon_months)
            shared_horizon_months = horizon_months

        safe: list[Bond] = []
        for b in active_bonds:
            if shared_horizon_date > b.maturity_date:
                warnings.append(
                    f"{b.isin}: горизонт {shared_horizon_date} >= maturity {b.maturity_date}. Бонд виключено."
                )
            else:
                safe.append(b)
        active_bonds = safe

        if len(active_bonds) < 2:
            raise ValueError("Після перевірки горизонту залишилось менше 2 бондів.")

    currencies = {b.currency for b in active_bonds}
    if len(currencies) > 1:
        warnings.append(
            f"Увага: бонди в різних валютах ({', '.join(sorted(currencies))}). "
            f"Порівняння без врахування валютного ризику."
        )

    if broker:
        # Exclude (don't silently reprice from a different broker) any bond this broker
        # doesn't actually offer — mixing broker-X price for one bond with a fallback price
        # for another would make a "compare as broker X" result misleading, not just imprecise.
        safe = []
        for b in active_bonds:
            if b.price_for_broker(broker) is None:
                warnings.append(f"{b.isin}: не пропонується брокером '{broker}', виключено з порівняння.")
            else:
                safe.append(b)
        active_bonds = safe

        if len(active_bonds) < 2:
            raise ValueError(
                f"Після фільтра по брокеру '{broker}' залишилось менше 2 непогашених бондів."
            )

    forecast_items: list[BondForecastItem] = []

    for bond in active_bonds:
        bond_horizon = bond.maturity_date if compare_by_full_period else shared_horizon_date
        sell = (not compare_by_full_period) and (bond.maturity_date > bond_horizon)
        item = _calculate_bond_forecast(
            bond=bond,
            quantity=quantity,
            as_of=as_of,
            horizon_date=bond_horizon,
            includes_maturity=compare_by_full_period,
            sell_at_horizon=sell,
            strategy=strategy,
            warnings=warnings,
            broker=broker,
        )
        if item is not None:
            forecast_items.append(item)

    if not forecast_items:
        warnings_text = "\n  ".join(warnings) if warnings else "немає деталей"
        raise ValueError(
            "Не вдалося розрахувати forecast для жодного бонду.\n"
            f"Причини:\n  {warnings_text}"
        )

    forecast_items.sort(
        key=lambda x: (x.effective_annual_return, -float(x.actual_invested)),
        reverse=True,
    )
    for rank, item in enumerate(forecast_items, start=1):
        item.rank = rank

    return BondComparisonResult(
        as_of=as_of,
        quantity=quantity,
        compare_by_full_period=compare_by_full_period,
        horizon_date=shared_horizon_date,
        horizon_months=shared_horizon_months,
        max_allowed_horizon_months=max_allowed_months,
        strategy=strategy,
        broker=broker,
        items=forecast_items,
        warnings=warnings,
    )


def _calculate_bond_forecast(
        bond: Bond,
        quantity: int,
        as_of: date,
        horizon_date: date,
        includes_maturity: bool,
        sell_at_horizon: bool,
        strategy: ProfitCalculationStrategy,
        warnings: list[str],
        broker: Optional[str] = None,
) -> Optional[BondForecastItem]:
    # broker given -> compare_bonds() already excluded any bond without a price_for_broker
    # match, so this is guaranteed non-None here. No broker -> same fallback as before
    # (entry_price() below falls back to face_value too; this check only decides whether
    # to warn about it).
    if (bond.price_for_broker(broker) if broker else bond.last_market_price) is None:
        warnings.append(
            f"{bond.isin}: ринкова ціна відсутня, використано номінал "
            f"({bond.face_value} {bond.currency})."
        )

    # entry_price() is the SINGLE canonical dirty<->clean conversion (see the module
    # docstring in math_core.py) — do not reimplement this arithmetic here. This used to
    # add accrued_interest_per_bond on top of bond.last_market_price/price_for_broker(),
    # which are already dirty (domain/models.py) — a double-counted NKD that inflated
    # dirty_price/actual_invested and produced skewed (sometimes absurdly negative)
    # ytm_to_maturity below.
    entry = entry_price(bond, as_of, broker)
    clean_price = entry.clean_price
    accrued_interest_per_bond = entry.accrued_interest
    dirty_price = entry.dirty_price

    if dirty_price <= Decimal("0"):
        warnings.append(f"{bond.isin}: некоректна ціна входу ({dirty_price}), пропущено.")
        return None

    all_future_cfs = build_future_cashflows(bond, quantity, as_of)

    coupon_cashflows = [
        cf for cf in all_future_cfs
        if cf.kind == "coupon" and cf.payment_date <= horizon_date
    ]

    coupons_total = sum(
        (cf.amount for cf in coupon_cashflows), Decimal("0")
    ).quantize(PRECISION)

    coupons_per_bond = (coupons_total / quantity).quantize(PRECISION)

    matures_on_horizon    = (bond.maturity_date == horizon_date)
    include_this_maturity = includes_maturity or matures_on_horizon

    maturity_amount = Decimal("0")
    if include_this_maturity:
        maturity_amount = (bond.face_value * quantity).quantize(PRECISION)

    is_sold_at_horizon = False
    sale_proceeds = Decimal("0")

    if sell_at_horizon and not include_this_maturity:
        is_sold_at_horizon = True
        ai_at_horizon = calculate_accrued_interest(bond, horizon_date, quantity=1)
        dirty_at_horizon = (clean_price + ai_at_horizon.amount_per_bond).quantize(PRECISION)
        sale_proceeds = (dirty_at_horizon * quantity).quantize(PRECISION)

    capital_return = (
        maturity_amount if include_this_maturity
        else sale_proceeds if is_sold_at_horizon
        else Decimal("0")
    )
    include_capital = include_this_maturity or is_sold_at_horizon

    actual_invested, total_profit = strategy.compute(
        clean_price=clean_price,
        accrued_interest=accrued_interest_per_bond,
        quantity=quantity,
        coupons_total=coupons_total,
        face_value=bond.face_value,
        include_capital_return=include_capital,
        capital_return_amount=capital_return,
    )

    total_profit_per_bond = (total_profit / quantity).quantize(PRECISION)

    horizon_days = (horizon_date - as_of).days
    if horizon_days <= 0:
        warnings.append(f"{bond.isin}: горизонт 0 днів, пропущено.")
        return None

    effective_return_for_horizon = round(
        float(total_profit) / float(actual_invested), 6
    )

    effective_annual_return = round(
        (1.0 + effective_return_for_horizon) ** (365.0 / horizon_days) - 1.0, 6
    )

    ytm_to_maturity = calculate_ytm(
        dirty_price_per_bond=float(dirty_price),
        future_cashflows=build_future_cashflows(bond, 1, as_of),
        settlement_date=as_of,
    )

    return BondForecastItem(
        bond=bond,
        horizon_date=horizon_date,
        strategy_name=strategy.name,
        dirty_entry_price=dirty_price,
        accrued_interest_per_bond=accrued_interest_per_bond,
        actual_invested=actual_invested,
        coupon_cashflows=coupon_cashflows,
        coupons_total=coupons_total,
        coupons_per_bond=coupons_per_bond,
        includes_maturity=include_this_maturity,
        maturity_amount=maturity_amount,
        is_sold_at_horizon=is_sold_at_horizon,
        sale_proceeds=sale_proceeds,
        total_profit=total_profit,
        total_profit_per_bond=total_profit_per_bond,
        effective_return_for_horizon=effective_return_for_horizon,
        effective_annual_return=effective_annual_return,
        ytm_to_maturity=ytm_to_maturity,
    )