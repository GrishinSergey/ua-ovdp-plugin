"""
Portfolio Engine — Result Models
=================================
Output dataclasses для портфельного движка.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Optional

from engine.investment.domain.models import Bond


@dataclass(frozen=True)
class AllocationItem:
    bond: Bond
    units: int
    investment_per_unit: Decimal
    total_investment: Decimal
    income_per_unit: Decimal
    total_income: Decimal
    efficiency: Decimal
    accrued_interest_per_unit: Decimal
    coupon_dates_in_horizon: list[date]


@dataclass
class PortfolioResult:
    mode: str
    settlement_date: date
    horizon_days: int
    horizon_date: date
    target_income: Decimal
    broker: Optional[str]
    allocations: list[AllocationItem]
    total_investment: Decimal
    total_income: Decimal
    real_yield: Decimal
    target_reached: bool
    income_gap: Decimal
    warnings: list[str] = field(default_factory=list)


@dataclass
class MonthlyPortfolioResult(PortfolioResult):
    monthly_summary: list[Decimal] = field(default_factory=list)
    monthly_target: Decimal = Decimal("0")
    underfunded_months: list[int] = field(default_factory=list)


@dataclass
class SimulationEvent:
    event_date: date
    kind: str           # "coupon" | "maturity" | "reinvest" | "cash_carry"
    isin: str
    bond_name: str
    amount: Decimal
    units: int = 0
    cash_pool_after: Decimal = Decimal("0")
    note: str = ""


@dataclass
class ChainStep:
    bond: Bond
    units: int
    buy_date: date
    dirty_price_per_unit: Decimal
    total_invested: Decimal
    source: str                          # "initial" | "reinvest"
    reinvested_from_isin: Optional[str]
    horizon_at_buy: date
    expected_income: Decimal


@dataclass
class ReinvestmentChain:
    steps: list[ChainStep]

    @property
    def origin_isin(self) -> str:
        return self.steps[0].bond.isin if self.steps else ""

    @property
    def total_initial_invested(self) -> Decimal:
        return self.steps[0].total_invested if self.steps else Decimal("0")


@dataclass
class SimulationResult:
    mode: str
    settlement_date: date
    horizon_days: int
    horizon_date: date
    target_income: Decimal
    broker: Optional[str]

    initial_allocations: list[AllocationItem]
    initial_investment: Decimal

    chains: list[ReinvestmentChain]
    events: list[SimulationEvent]

    total_coupon_income: Decimal
    total_maturity_income: Decimal
    residual_cash: Decimal
    total_income: Decimal
    real_yield: Decimal

    target_reached: bool
    income_gap: Decimal

    warnings: list[str] = field(default_factory=list)