from pydantic import BaseModel


class BrokerPriceResponse(BaseModel):
    broker: str
    price: float


class CouponPaymentResponse(BaseModel):
    payment_date: str
    amount_per_bond: float
    type: str


class BondResponse(BaseModel):
    isin: str
    name: str | None = None
    currency: str = "UAH"
    face_value: float = 1000.0
    coupon_rate: float
    ytm: float | None = None
    maturity_date: str
    coupon_schedule: list[CouponPaymentResponse] = []
    broker_prices: list[BrokerPriceResponse] = []
    last_market_price: float | None = None
    days_to_maturity: int | None = None