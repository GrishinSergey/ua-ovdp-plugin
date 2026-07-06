"""
OVDP Forecast — Broker Profit Calculation Strategies

Різні брокери рахують "очікуваний дохід" по-різному.
Щоб порівняти наші цифри з їх UI — підміняємо стратегію.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol, runtime_checkable

PRECISION = Decimal("0.01")


@runtime_checkable
class ProfitCalculationStrategy(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def description(self) -> str: ...

    def compute(
            self,
            clean_price: Decimal,
            accrued_interest: Decimal,
            quantity: int,
            coupons_total: Decimal,
            face_value: Decimal,
            include_capital_return: bool,
            capital_return_amount: Decimal,
    ) -> tuple[Decimal, Decimal]: ...


# ── Standard (наш метод) ──────────────────────────────────────────────────────

@dataclass(frozen=True)
class StandardStrategy:
    """
    Фінансово коректний метод.
    База вкладень = dirty price × qty (clean + НКД).
    """

    @property
    def name(self) -> str:
        return "Standard"

    @property
    def description(self) -> str:
        return "Dirty price база (clean + НКД). Фінансово коректний метод."

    def compute(
            self,
            clean_price: Decimal,
            accrued_interest: Decimal,
            quantity: int,
            coupons_total: Decimal,
            face_value: Decimal,
            include_capital_return: bool,
            capital_return_amount: Decimal,
    ) -> tuple[Decimal, Decimal]:
        dirty_price     = (clean_price + accrued_interest).quantize(PRECISION)
        actual_invested = (dirty_price * quantity).quantize(PRECISION)

        if include_capital_return:
            total_profit = (coupons_total + capital_return_amount - actual_invested).quantize(PRECISION)
        else:
            total_profit = coupons_total

        return actual_invested, total_profit


# ── Privat24 ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Privat24Strategy:
    """
    Метод Приват24.
    База вкладень = clean price × qty (без НКД).
    """

    @property
    def name(self) -> str:
        return "Приват24"

    @property
    def description(self) -> str:
        return "Clean price база (без НКД у витратах). Відповідає UI Приват24."

    def compute(
            self,
            clean_price: Decimal,
            accrued_interest: Decimal,
            quantity: int,
            coupons_total: Decimal,
            face_value: Decimal,
            include_capital_return: bool,
            capital_return_amount: Decimal,
    ) -> tuple[Decimal, Decimal]:
        actual_invested = (clean_price * quantity).quantize(PRECISION)

        if include_capital_return:
            total_profit = (coupons_total + capital_return_amount - actual_invested).quantize(PRECISION)
        else:
            total_profit = coupons_total

        return actual_invested, total_profit


# ── Фабрика ───────────────────────────────────────────────────────────────────

_REGISTRY: dict[str, ProfitCalculationStrategy] = {
    "standard":  StandardStrategy(),
    "privat24":  Privat24Strategy(),
    "приват24":  Privat24Strategy(),
}


def get_strategy(name: str) -> ProfitCalculationStrategy:
    key = name.lower().strip()
    if key not in _REGISTRY:
        available = list(_REGISTRY.keys())
        raise ValueError(f"Невідома стратегія '{name}'. Доступні: {available}")
    return _REGISTRY[key]


def available_strategies() -> list[str]:
    seen = set()
    result = []
    for s in _REGISTRY.values():
        if s.name not in seen:
            seen.add(s.name)
            result.append(s.name)
    return result