"""
Portfolio Engine — Math Core
============================
Чиста математика для портфельного движка ОВДП.

Залежності:
    - models.Bond, models.CouponPayment (тільки структури даних)
    - stdlib: datetime, decimal

НЕ імпортує нічого з finance.py, forecast_core.py, strategy.py.

Day count convention: Actual/365 (методологія НБУ для ОВДП)
Номінал ОВДП: завжди 1000 UAH (FACE_VALUE)

Ключові принципи:
    1. BrokerPrice.price = dirty price (як показує Приват24 та інші)
    2. clean_price = dirty_price - accrued_interest
    3. investment_cost per unit = dirty_price (реальні витрати інвестора)
    4. real_income = coupons_in_horizon - accrued_interest_paid
    5. efficiency = real_income / investment_cost  (НЕ coupon_rate)

── КАНОНІЧНА реалізація dirty/clean/НКД — не дублювати ─────────────────────

accrued_interest() і entry_price() (нижче) — ЄДИНЕ місце в кодовій базі, де
dirty price конвертується в clean price (і навпаки). Bond.last_market_price
та BrokerPrice.price ЗАВЖДИ dirty (див. domain/models.py) — це вже вирване
з двозначності на рівні даних, помилка тут завжди в коді, не в даних.

Раніше forecast_core.py і finance.py незалежно одне від одного РЕІМПЛЕМЕНТУВАЛИ
цю конвертацію — і обидва зробили одну й ту саму помилку: трактували вже-dirty
bond.last_market_price/price_for_broker() як "clean" і додавали accrued_interest
ще раз (подвійний облік НКД → занижений actual_invested/дохід, завищена ціна
входу, помітно в ytm_to_maturity в compare_bonds і current_ytm/unrealized_pnl
в position_metrics). Обидва тепер викликають entry_price() з цього модуля
замість власної арифметики — саме так і мають чинити майбутні виклики:

    from engine.investment.forecast.math_core import entry_price
    ep = entry_price(bond, settlement_date, broker)   # broker=None -> last_market_price
    ep.dirty_price     # те, що реально платиш зараз
    ep.clean_price      # dirty - НКД
    ep.accrued_interest  # НКД компонент

Якщо колись знадобиться ще одне місце, що рахує dirty↔clean — використовуй
entry_price()/accrued_interest() звідси, а не пиши `price + accrued_interest`
чи `price - accrued_interest` руками. Якщо broker-конвенція колись зміниться
(наприклад, якийсь новий брокер почне віддавати clean price замість dirty),
міняти доведеться тільки тут.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from engine.investment.domain.models import Bond

# ── Константи ─────────────────────────────────────────────────────────────────

DAY_BASIS = Decimal("365")
PRECISION = Decimal("0.000001")   # внутрішня точність
OUT_PREC  = Decimal("0.01")       # точність виводу


# ── Result dataclasses ────────────────────────────────────────────────────────

@dataclass(frozen=True)
class AccruedInterest:
    """Результат розрахунку НКД на дату settlement_date."""
    amount: Decimal             # НКД на 1 облігацію
    days_accrued: int           # днів від останнього купону
    last_coupon_date: date      # опорна дата (або issue_date якщо купонів ще не було)
    next_coupon_date: Optional[date]  # None якщо більше купонів немає


@dataclass(frozen=True)
class EntryPrice:
    """Ціни входу в позицію на дату settlement_date."""
    dirty_price: Decimal        # те що платить інвестор (від брокера)
    accrued_interest: Decimal   # НКД компонент
    clean_price: Decimal        # dirty - НКД (розрахунковий)
    # dirty_price = clean_price + accrued_interest — завжди


@dataclass(frozen=True)
class CouponInHorizon:
    """Один купон що потрапляє в горизонт."""
    payment_date: date
    amount: Decimal             # сума на 1 облігацію


@dataclass(frozen=True)
class BondHorizonResult:
    """
    Повний розрахунок для однієї облігації за горизонт.
    Використовується оптимізатором для побудови портфеля.
    """
    bond: Bond

    # Ціна входу
    entry: EntryPrice

    # Купони в горизонті
    coupons_in_horizon: list[CouponInHorizon]
    total_coupon_amount: Decimal    # сума купонів на 1 шт

    # Реальний дохід (на 1 шт)
    real_income: Decimal            # total_coupon_amount - accrued_interest
    # real_income може бути від'ємним якщо горизонт < наступного купону

    # Метрика ефективності
    efficiency: Decimal             # real_income / entry.dirty_price

    # Місячний cashflow (індекс = місяць від settlement_date, 0-based)
    # monthly_cashflow[0] може бути від'ємним (НКД вираховується в місяць 0)
    monthly_cashflow: list[Decimal]

    # Службові
    settlement_date: date
    horizon_date: date
    is_valid: bool                  # False якщо облігація не має купонів у горизонті


# ── Core Math Functions ───────────────────────────────────────────────────────

def accrued_interest(bond: Bond, settlement_date: date) -> AccruedInterest:
    """
    Розраховує НКД на дату settlement_date.

    Формула: НКД = Номінал × Ставка × Днів_від_останнього_купону / 365

    Edge cases:
        - settlement_date == coupon_date → НКД = 0
        - settlement_date до першого купону → last_coupon_date = issue_date
        - settlement_date після всіх купонів → облігація погашена / невалідна

    Використовується напряму portfolio_service-стороною через entry_price() нижче
    (компонент .accrued_interest) — окремо викликати варто лише коли потрібне ЧИСТЕ
    НКД без ціни (наприклад monthly_cashflow_vector нижче). Для "скільки коштує
    зайти в позицію" — завжди entry_price(), не ця функція окремо.
    """
    if settlement_date >= bond.maturity_date:
        return AccruedInterest(
            amount=Decimal("0"),
            days_accrued=0,
            last_coupon_date=bond.maturity_date,
            next_coupon_date=None,
        )

    coupon_dates = sorted(c.payment_date for c in bond.coupon_schedule)

    past   = [d for d in coupon_dates if d <= settlement_date]
    future = [d for d in coupon_dates if d > settlement_date]

    last_coupon = max(past) if past else bond.issue_date
    next_coupon = min(future) if future else None

    days_accrued = (settlement_date - last_coupon).days

    amount = (
        bond.face_value * bond.coupon_rate * Decimal(days_accrued) / DAY_BASIS
    ).quantize(PRECISION, rounding=ROUND_HALF_UP)

    return AccruedInterest(
        amount=amount,
        days_accrued=days_accrued,
        last_coupon_date=last_coupon,
        next_coupon_date=next_coupon,
    )


def entry_price(bond: Bond, settlement_date: date, broker: Optional[str] = None) -> EntryPrice:
    """
    Розраховує ціну входу в позицію на дату settlement_date.

    BrokerPrice.price трактується як DIRTY price (як показує Приват24).
    clean_price витягується зворотньо: clean = dirty - НКД

    Пріоритет ціни:
        1. broker (якщо вказано і є в broker_prices)
        2. last_market_price
        3. face_value (fallback)

    Ціна ПРОЄКТУЄТЬСЯ на settlement_date, а не повертається як є. last_market_price/
    price_for_broker() відомі станом на bond.price_quote_date (дата скрейпу снепшоту, з
    якого цей Bond побудовано) — це майже ніколи не той самий день, що settlement_date
    (сьогодні, дата покупки заднім числом, майбутній горизонт). Тому: 1) знімаємо стабільну
    clean-ціну на price_quote_date (dirty_at_quote - НКД(price_quote_date)), 2) відновлюємо
    dirty на settlement_date (clean + НКД(settlement_date)). Раніше ця функція повертала
    dirty_at_quote як є, незалежно від settlement_date — це давало ЗАМОРОЖЕНУ ціну входу,
    тоді як accrued_interest окремо коректно рухався з settlement_date, і вони розходились
    (дало абсурдні/немонотонні ytm_to_maturity в compare_bonds для settlement_date, віддаленого
    від дати снепшоту).

    Якщо price_quote_date невідома (Bond побудовано не зі снепшоту — hand-built,
    analytics_service._to_domain_bond) АБО ціна — сам face_value fallback (немає реальної
    котирувальної дати, яку можна прив'язати), quote_date прирівнюється до settlement_date:
    НКД на обох кінцях тоді буквально той самий виклик з тими самими аргументами, і формула
    звужується точно до dirty_at_quote — той самий результат, що й до цього фікса.

    Канонічна крапка входу для будь-якого dirty↔clean перетворення в кодовій базі
    (див. попередження на початку файлу). Використовується з:
        - forecast_core._calculate_bond_forecast() (compare_bonds) — вхідна ціна
          позиції та прогноз ціни продажу на горизонті.
        - finance.calculate_position_metrics() (position_metrics/portfolio_metrics/
          simulate_reinvestment) — поточна ринкова dirty-ціна відкритої позиції.
        - portfolio/optimizer.py, monthly_allocator.py, simulator.py (build_target_portfolio)
          — через bond_horizon_result()/real_income() нижче, які вже викликають цю функцію.
    Якщо додаєш новий код, що рахує "скільки коштує зайти в облігацію зараз" —
    виклич цю функцію, не пиши формулу заново.
    """
    dirty_at_quote: Decimal
    quoted: bool  # True лише якщо dirty_at_quote — реальна ринкова ціна, не face_value fallback

    if broker and (bp := bond.price_for_broker(broker)) is not None:
        dirty_at_quote, quoted = bp, True
    elif bond.last_market_price is not None:
        dirty_at_quote, quoted = bond.last_market_price, True
    else:
        dirty_at_quote, quoted = bond.face_value, False

    quote_date = (
        bond.price_quote_date
        if quoted and bond.price_quote_date is not None
        else settlement_date
    )

    ai_quote = accrued_interest(bond, quote_date)
    clean = (dirty_at_quote - ai_quote.amount).quantize(PRECISION, rounding=ROUND_HALF_UP)
    ai_settlement = accrued_interest(bond, settlement_date)
    dirty = (clean + ai_settlement.amount).quantize(PRECISION, rounding=ROUND_HALF_UP)

    return EntryPrice(
        dirty_price=dirty.quantize(OUT_PREC),
        accrued_interest=ai_settlement.amount.quantize(OUT_PREC),
        clean_price=clean.quantize(OUT_PREC),
    )


def coupons_in_horizon(
        bond: Bond,
        settlement_date: date,
        horizon_date: date,
) -> list[CouponInHorizon]:
    """
    Повертає купони облігації що потрапляють у вікно (settlement_date, horizon_date].

    Умова включення: settlement_date < payment_date <= horizon_date
    Якщо maturity_date < horizon_date — купони після maturity не включаються (їх і немає).
    """
    effective_horizon = min(horizon_date, bond.maturity_date)

    result = []
    for c in bond.coupon_schedule:
        if settlement_date < c.payment_date <= effective_horizon:
            result.append(CouponInHorizon(
                payment_date=c.payment_date,
                amount=c.amount_per_bond,
            ))

    return sorted(result, key=lambda x: x.payment_date)


def real_income(
        bond: Bond,
        settlement_date: date,
        horizon_date: date,
        broker: Optional[str] = None,
) -> Decimal:
    """
    Реальний дохід на 1 облігацію за горизонт.

    real_income = sum(coupons in horizon) - accrued_interest_paid

    НКД вираховується один раз — при вході.
    """
    ep = entry_price(bond, settlement_date, broker)
    coupons = coupons_in_horizon(bond, settlement_date, horizon_date)

    total_coupons = sum((c.amount for c in coupons), Decimal("0"))
    income = (total_coupons - ep.accrued_interest).quantize(PRECISION, rounding=ROUND_HALF_UP)

    return income


def monthly_cashflow_vector(
        bond: Bond,
        settlement_date: date,
        horizon_date: date,
        broker: Optional[str] = None,
) -> list[Decimal]:
    """
    Будує вектор місячних cashflow для однієї облігації (на 1 шт).

    Індекс = порядковий номер місяця від settlement_date (0-based).
    Місяць 0 = місяць settlement_date.

    monthly_cashflow[0] -= accrued_interest  (НКД вираховується в місяць 0)
    Перший місяць може бути від'ємним якщо купон ще не скоро.
    """
    n_months = _months_between(settlement_date, horizon_date) + 1
    vector: list[Decimal] = [Decimal("0")] * n_months

    coupons = coupons_in_horizon(bond, settlement_date, horizon_date)

    for coupon in coupons:
        m_idx = _months_between(settlement_date, coupon.payment_date)
        if 0 <= m_idx < n_months:
            vector[m_idx] += coupon.amount

    ep = entry_price(bond, settlement_date, broker)
    vector[0] = (vector[0] - ep.accrued_interest).quantize(PRECISION, rounding=ROUND_HALF_UP)

    return vector


def bond_horizon_result(
        bond: Bond,
        settlement_date: date,
        horizon_date: date,
        broker: Optional[str] = None,
) -> BondHorizonResult:
    """
    Повний розрахунок для облігації за горизонт.
    Центральна функція — агрегує всі метрики для оптимізатора.
    """
    if bond.maturity_date <= settlement_date:
        return _invalid_result(bond, settlement_date, horizon_date, "maturity <= settlement")

    ep      = entry_price(bond, settlement_date, broker)
    coupons = coupons_in_horizon(bond, settlement_date, horizon_date)

    if not coupons:
        return _invalid_result(bond, settlement_date, horizon_date, "no coupons in horizon")

    total_coupons = sum((c.amount for c in coupons), Decimal("0"))
    income = (total_coupons - ep.accrued_interest).quantize(PRECISION, rounding=ROUND_HALF_UP)

    if income <= Decimal("0"):
        return _invalid_result(bond, settlement_date, horizon_date, "real_income <= 0")

    eff = (income / ep.dirty_price).quantize(PRECISION, rounding=ROUND_HALF_UP)

    monthly = monthly_cashflow_vector(bond, settlement_date, horizon_date, broker)

    return BondHorizonResult(
        bond=bond,
        entry=ep,
        coupons_in_horizon=coupons,
        total_coupon_amount=total_coupons.quantize(OUT_PREC),
        real_income=income.quantize(OUT_PREC),
        efficiency=eff,
        monthly_cashflow=monthly,
        settlement_date=settlement_date,
        horizon_date=horizon_date,
        is_valid=True,
    )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _months_between(d_from: date, d_to: date) -> int:
    return (d_to.year - d_from.year) * 12 + (d_to.month - d_from.month)


def _invalid_result(
        bond: Bond,
        settlement_date: date,
        horizon_date: date,
        reason: str,
) -> BondHorizonResult:
    ep = EntryPrice(
        dirty_price=bond.last_market_price or bond.face_value,
        accrued_interest=Decimal("0"),
        clean_price=bond.last_market_price or bond.face_value,
    )
    return BondHorizonResult(
        bond=bond,
        entry=ep,
        coupons_in_horizon=[],
        total_coupon_amount=Decimal("0"),
        real_income=Decimal("0"),
        efficiency=Decimal("0"),
        monthly_cashflow=[],
        settlement_date=settlement_date,
        horizon_date=horizon_date,
        is_valid=False,
    )