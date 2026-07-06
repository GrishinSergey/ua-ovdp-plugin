from pydantic import BaseModel
from typing import Any


class BrokerPriceInput(BaseModel):
    broker: str
    price: float


class CouponPaymentInput(BaseModel):
    payment_date: str
    amount_per_bond: float
    type: str


class BondInput(BaseModel):
    isin: str
    name: str | None = None
    currency: str = "UAH"
    face_value: float = 1000.0
    coupon_rate: float          # decimal fraction — e.g. 0.1748
    ytm: float | None = None
    maturity_date: str
    coupon_schedule: list[CouponPaymentInput] = []
    broker_prices: list[BrokerPriceInput] = []
    last_market_price: float | None = None
    days_to_maturity: int | None = None
    issue_date: str | None = None


class EngineRequest(BaseModel):
    bonds: list[BondInput]
    target_income: float
    horizon_days: int
    settlement_date: str
    mode: str = "max_efficiency"
    broker: str | None = None


class EngineResponse(BaseModel):
    result: dict[str, Any]