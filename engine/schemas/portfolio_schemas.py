from pydantic import BaseModel

from engine.schemas.bond_schemas import BondResponse


class PositionCreate(BaseModel):
    bond_isin: str
    purchase_date: str
    quantity: int
    purchase_price_dirty: float      # dirty price per bond (value shown by broker)
    accrued_interest_paid: float = 0.0
    broker_fee: float = 0.0
    broker: str | None = None
    label: str | None = None


class PositionResponse(PositionCreate):
    id: str
    total_invested: float


class ReceivedPaymentCreate(BaseModel):
    payment_date: str
    amount: float
    kind: str = "coupon"  # "coupon" | "maturity"
    note: str | None = None


class ReceivedPaymentResponse(ReceivedPaymentCreate):
    id: str
