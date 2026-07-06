"""
Bridges scraper market data (bonds.json / market_history/<ts>.json shape) and this
project's own position-storage shape into engine/ domain objects.

Pure conversions only — no file I/O, no knowledge of market_history/ or CLAUDE_PROJECT_DIR.
server.py resolves paths/snapshots and hands plain dicts to these functions; that keeps
engine/ agnostic to where its input data comes from (same spirit as analytics_service's
own _to_domain_bond, which converts the HTTP-shaped BondInput rather than touching files).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Optional

from engine.investment.domain.models import Bond, BrokerPrice, CouponPayment, Position, ReceivedPayment

FACE_VALUE = Decimal("1000")  # OVDP nominal is always 1000 UAH/USD/EUR


def bond_from_snapshot(bond_dict: dict[str, Any]) -> Bond:
    """
    Convert one bond record from a scraper market_history snapshot into a domain Bond.

    Two fields don't exist in scraper output and are derived here:
      - issue_date: scraper only gives a 4-digit issue_year (or nothing) -> Jan 1 of that
        year, falling back to 2000-01-01 (same fallback analytics_service uses for a
        missing issue_date). Only matters as a pre-first-coupon reference point.
      - coupon_rate: scraper gives coupon_amount (absolute, per payment) but not the
        annualized rate BondInput expects -> derived as coupon_amount * frequency / face_value,
        using Bond.coupon_frequency (itself inferred from the gap between the first two
        scheduled coupon dates).
    """
    coupon_schedule = [
        CouponPayment(
            payment_date=date.fromisoformat(s["date"]),
            amount_per_bond=Decimal(str(s["amount"])),
        )
        for s in bond_dict.get("schedule", [])
        if s.get("type") == "купон"
    ]

    issue_year = bond_dict.get("issue_year")
    issue_date = date(int(issue_year), 1, 1) if issue_year else date(2000, 1, 1)

    bond = Bond(
        isin=bond_dict["isin"],
        name=bond_dict.get("name") or bond_dict["isin"],
        currency=bond_dict.get("currency") or "UAH",
        face_value=FACE_VALUE,
        coupon_rate=Decimal("0"),  # placeholder; derived below once coupon_schedule is set
        issue_date=issue_date,
        maturity_date=date.fromisoformat(bond_dict["maturity_date"]),
        coupon_schedule=coupon_schedule,
    )

    coupon_amount = bond_dict.get("coupon_amount")
    if coupon_amount is not None:
        bond.coupon_rate = (
            Decimal(str(coupon_amount)) * bond.coupon_frequency / FACE_VALUE
        ).quantize(Decimal("0.0001"))

    broker_prices = [
        BrokerPrice(broker=w["broker"], price=Decimal(str(w["price"])))
        for w in bond_dict.get("where_to_buy", [])
        if w.get("price") is not None
    ]
    bond.broker_prices = broker_prices
    if broker_prices:
        bond.last_market_price = min(bp.price for bp in broker_prices)

    return bond


def freeze_bond(bond: Bond) -> dict[str, Any]:
    """Bond -> the storage shape embedded in a position record at add_position time.
    Only the contractual facts (schedule/maturity/etc.) -- never price, which is always
    re-resolved live from the current market_history snapshot when needed."""
    return {
        "name": bond.name,
        "currency": bond.currency,
        "face_value": float(bond.face_value),
        "coupon_rate": float(bond.coupon_rate),
        "issue_date": bond.issue_date.isoformat(),
        "maturity_date": bond.maturity_date.isoformat(),
        "coupon_schedule": [
            {"payment_date": cp.payment_date.isoformat(), "amount_per_bond": float(cp.amount_per_bond)}
            for cp in bond.coupon_schedule
        ],
    }


def bond_from_frozen(isin: str, frozen: dict[str, Any]) -> Bond:
    """Reverse of freeze_bond -- reconstructs a Bond from a stored position's frozen facts."""
    return Bond(
        isin=isin,
        name=frozen["name"],
        currency=frozen["currency"],
        face_value=Decimal(str(frozen["face_value"])),
        coupon_rate=Decimal(str(frozen["coupon_rate"])),
        issue_date=date.fromisoformat(frozen["issue_date"]),
        maturity_date=date.fromisoformat(frozen["maturity_date"]),
        coupon_schedule=[
            CouponPayment(
                payment_date=date.fromisoformat(c["payment_date"]),
                amount_per_bond=Decimal(str(c["amount_per_bond"])),
            )
            for c in frozen["coupon_schedule"]
        ],
    )


def position_from_record(
        record: dict[str, Any],
        as_of: date,
        live_price: Optional[Decimal] = None,
) -> Position:
    """
    Persisted position record -> domain Position, ready for calculate_position_metrics.

    received_payments is NOT read from storage (nothing logs it) -- it's derived here from
    the bond's own frozen schedule: every coupon/maturity dated between purchase_date and
    as_of is treated as received at its scheduled amount. This is a deliberate simplification
    for OVDP specifically (state-guaranteed, effectively never actually deviates from
    schedule) traded for zero manual logging. If real payments ever diverge from schedule,
    this is the function to revisit.
    """
    bond = bond_from_frozen(record["isin"], record["bond_snapshot"])
    if live_price is not None:
        bond.last_market_price = live_price

    purchase_date = date.fromisoformat(record["purchase_date"])
    quantity = int(record["quantity"])

    received: list[ReceivedPayment] = [
        ReceivedPayment(payment_date=cp.payment_date, amount=cp.amount_per_bond * quantity, kind="coupon")
        for cp in bond.coupon_schedule
        if purchase_date < cp.payment_date <= as_of
    ]
    if purchase_date < bond.maturity_date <= as_of:
        received.append(ReceivedPayment(
            payment_date=bond.maturity_date, amount=bond.face_value * quantity, kind="maturity",
        ))

    return Position(
        bond=bond,
        purchase_date=purchase_date,
        quantity=quantity,
        purchase_price_dirty=Decimal(str(record["purchase_price_dirty"])),
        broker_fee=Decimal(str(record.get("broker_fee", 0))),
        accrued_interest_paid=Decimal(str(record.get("accrued_interest_paid", 0))),
        label=record.get("label"),
        received_payments=received,
    )
