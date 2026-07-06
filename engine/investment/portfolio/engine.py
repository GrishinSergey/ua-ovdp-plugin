"""
Portfolio Engine — Public API
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Optional, Literal, Union

from engine.investment.domain.models import Bond
from engine.investment.portfolio.monthly_allocator import build_monthly_income_portfolio
from engine.investment.portfolio.optimizer import build_max_efficiency_portfolio
from engine.investment.portfolio.result_models import PortfolioResult, MonthlyPortfolioResult, SimulationResult
from engine.investment.portfolio.simulator import simulate_portfolio

EngineMode = Literal["max_efficiency", "monthly_income", "max_efficiency_reinvest"]


@dataclass
class EngineRequest:
    bonds: list[Bond]
    target_income: Decimal
    horizon_days: int
    settlement_date: date
    mode: EngineMode = "max_efficiency"
    broker: Optional[str] = None

    def validate(self) -> list[str]:
        errors = []
        if not self.bonds:
            errors.append("bonds: список порожній.")
        if self.target_income <= Decimal("0"):
            errors.append(f"target_income має бути > 0, отримано: {self.target_income}.")
        if self.horizon_days <= 0:
            errors.append(f"horizon_days має бути > 0, отримано: {self.horizon_days}.")
        if self.mode not in ("max_efficiency", "monthly_income", "max_efficiency_reinvest"):
            errors.append(f"mode: невідомий режим '{self.mode}'.")
        return errors


def build_portfolio(
        request: EngineRequest,
) -> Union[PortfolioResult, MonthlyPortfolioResult, SimulationResult]:
    errors = request.validate()
    if errors:
        raise ValueError("Невалідний запит:\n" + "\n".join(f"  - {e}" for e in errors))

    if request.mode == "max_efficiency":
        return build_max_efficiency_portfolio(
            bonds=request.bonds,
            target_income=request.target_income,
            settlement_date=request.settlement_date,
            horizon_days=request.horizon_days,
            broker=request.broker,
        )

    if request.mode == "monthly_income":
        return build_monthly_income_portfolio(
            bonds=request.bonds,
            target_income=request.target_income,
            settlement_date=request.settlement_date,
            horizon_days=request.horizon_days,
            broker=request.broker,
        )

    if request.mode == "max_efficiency_reinvest":
        return simulate_portfolio(
            bonds=request.bonds,
            target_income=request.target_income,
            settlement_date=request.settlement_date,
            horizon_days=request.horizon_days,
            broker=request.broker,
        )

    raise ValueError(f"Невідомий mode: {request.mode}")