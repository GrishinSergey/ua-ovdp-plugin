"""
Спільні доменні моделі для ОВДП:
- Bond, BrokerPrice, CouponPayment
- Position, ReceivedPayment, Portfolio

Ці ж моделі використовуються в усіх фічах:
- пасивний портфель (`portfolio`)
- порівняння бондів (`forecast`)
- портфельний движок (`portfolio_engine`)
"""

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Optional


@dataclass
class CouponPayment:
    """Одна виплата по купону або погашення (довідник з розкладу)."""

    payment_date: date
    amount_per_bond: Decimal  # сума на 1 облігацію

    def __repr__(self) -> str:
        return f"CouponPayment({self.payment_date}, {self.amount_per_bond})"


@dataclass
class ReceivedPayment:
    """
    Фактично отримана виплата по позиції — вводиться вручну
    після того, як гроші прийшли на рахунок.

    kind: "coupon" | "maturity"
    amount: вся сума на позицію (не на 1 шт)
    """

    payment_date: date
    amount: Decimal
    kind: str = "coupon"
    note: Optional[str] = None


@dataclass
class BrokerPrice:
    """Ціна облігації у конкретного брокера/банку."""

    broker: str
    price: Decimal  # dirty price


@dataclass
class Bond:
    isin: str
    name: str
    currency: str
    face_value: Decimal
    coupon_rate: Decimal
    issue_date: date
    maturity_date: date
    coupon_schedule: list[CouponPayment] = field(default_factory=list)
    last_market_price: Optional[Decimal] = None  # dirty price (загальна / за замовчуванням)
    broker_prices: list[BrokerPrice] = field(default_factory=list)  # ціни по брокерах
    price_quote_date: Optional[date] = None
    # Календарна дата, на яку last_market_price/broker_prices були актуальні (дата скрейпу
    # снепшоту, з якого побудовано цей Bond) — НЕ дата settlement_date, з якою цей Bond
    # можуть використати пізніше. math_core.entry_price() використовує цю дату як якір, щоб
    # коректно спроєктувати ціну на довільний settlement_date (а не просто повернути сирий
    # скрейплений dirty price, що коректний лише коли settlement_date ≈ ця дата). None —
    # коли Bond побудовано не зі снепшоту (hand-built у тестах, analytics_service._to_domain_bond)
    # — entry_price() тоді деградує до трактування ціни як актуальної на сам settlement_date.

    def price_for_broker(self, broker: str) -> Optional[Decimal]:
        """Повертає dirty price для конкретного брокера або None якщо не знайдено."""
        for bp in self.broker_prices:
            if bp.broker.lower() == broker.lower():
                return bp.price
        return None

    def available_brokers(self) -> list[str]:
        """Список брокерів де доступна ця облігація."""
        return [bp.broker for bp in self.broker_prices]

    @property
    def coupon_frequency(self) -> int:
        if len(self.coupon_schedule) < 2:
            return 2
        dates = sorted(c.payment_date for c in self.coupon_schedule)
        gap = (dates[1] - dates[0]).days
        if gap <= 95:
            return 4
        if gap <= 185:
            return 2
        return 1

    @property
    def typical_coupon_amount(self) -> Decimal:
        if self.coupon_schedule:
            return self.coupon_schedule[0].amount_per_bond
        return (
            self.face_value * self.coupon_rate / Decimal(self.coupon_frequency)
        ).quantize(Decimal("0.01"))


@dataclass
class Position:
    bond: Bond
    purchase_date: date
    quantity: int
    purchase_price_dirty: Decimal  # dirty price per bond as shown by broker (Privat24 etc.)
    broker_fee: Decimal = field(default_factory=lambda: Decimal("0"))
    accrued_interest_paid: Decimal = field(default_factory=lambda: Decimal("0"))
    label: Optional[str] = None
    received_payments: list[ReceivedPayment] = field(default_factory=list)

    @property
    def total_invested(self) -> Decimal:
        # dirty price already includes НКД — do not add again
        return self.purchase_price_dirty * self.quantity + self.broker_fee


@dataclass
class Portfolio:
    name: str
    positions: list[Position] = field(default_factory=list)

    def add(self, position: Position) -> None:
        self.positions.append(position)


__all__ = [
    "CouponPayment",
    "ReceivedPayment",
    "BrokerPrice",
    "Bond",
    "Position",
    "Portfolio",
]