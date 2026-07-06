from datetime import date
from decimal import Decimal

from engine.investment.domain.models import Bond, BrokerPrice, CouponPayment
from engine.investment.portfolio.engine import EngineRequest, build_portfolio
from engine.schemas.analytics_schemas import BondInput, EngineRequest as HttpEngineRequest
from engine.serialize import to_jsonable


def _to_domain_bond(bond_input: BondInput) -> Bond:
    coupon_rate = Decimal(str(bond_input.coupon_rate))

    # Parse only coupon-type entries; filter out maturity payments
    coupon_schedule = [
        CouponPayment(
            payment_date=date.fromisoformat(c.payment_date),
            amount_per_bond=Decimal(str(c.amount_per_bond)),
        )
        for c in bond_input.coupon_schedule
        if c.type == "купон"
    ]

    broker_prices = [
        BrokerPrice(broker=bp.broker, price=Decimal(str(bp.price)))
        for bp in bond_input.broker_prices
    ]

    issue_date = (
        date.fromisoformat(bond_input.issue_date)
        if bond_input.issue_date
        else date(2000, 1, 1)
    )

    return Bond(
        isin=bond_input.isin,
        name=bond_input.name or bond_input.isin,
        currency=bond_input.currency,
        face_value=Decimal("1000"),
        coupon_rate=coupon_rate,
        issue_date=issue_date,
        maturity_date=date.fromisoformat(bond_input.maturity_date),
        coupon_schedule=coupon_schedule,
        last_market_price=Decimal(str(bond_input.last_market_price)) if bond_input.last_market_price else None,
        broker_prices=broker_prices,
    )


def compute_analytics(request: HttpEngineRequest) -> dict:
    bonds = [_to_domain_bond(b) for b in request.bonds]

    engine_request = EngineRequest(
        bonds=bonds,
        target_income=Decimal(str(request.target_income)),
        horizon_days=request.horizon_days,
        settlement_date=date.fromisoformat(request.settlement_date),
        mode=request.mode,
        broker=request.broker,
    )

    result = build_portfolio(engine_request)
    return to_jsonable(result)
