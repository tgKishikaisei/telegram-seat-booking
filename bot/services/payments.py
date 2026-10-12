"""Оплата брони звёздами Telegram: счёт → pre_checkout → successful_payment → билеты.

Мини-приложение открывает счёт через Telegram.WebApp.openInvoice — пользователь платит, не уходя
из схемы зала. Подтверждение приходит боту обычным сообщением successful_payment.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from bot.models import HoldStatus, Order, OrderStatus, Ticket
from bot.repositories import sales as sales_repo
from bot.services.tickets import new_code

logger = logging.getLogger(__name__)

STARS = "XTR"
PREFIX = "seat"
PAYMENT_GRACE = timedelta(minutes=5)  # на сколько продлить бронь, когда Telegram начал списание


class PaymentError(Exception):
    pass


def make_payload(order_id: int, secret: str) -> str:
    signature = hmac.new(secret.encode(), f"{PREFIX}:{order_id}".encode(), hashlib.sha256).hexdigest()[:32]
    return f"{PREFIX}:{order_id}:{signature}"


def parse_payload(payload: str, secret: str) -> int | None:
    parts = (payload or "").split(":")
    if len(payload or "") > 128 or len(parts) != 3 or parts[0] != PREFIX or not parts[1].isdigit():
        return None
    order_id = int(parts[1])
    return order_id if hmac.compare_digest(make_payload(order_id, secret), payload) else None


async def order_for_hold(session: AsyncSession, public_id: str, user_id: int, now: datetime) -> Order:
    hold = await sales_repo.by_public_id(session, public_id)
    if hold is None or hold.user_id != user_id:
        raise PaymentError("Бронь не найдена")
    if hold.status is not HoldStatus.ACTIVE or hold.expires_at <= now:
        raise PaymentError("Время брони истекло — выберите места заново")
    return await sales_repo.create_order(session, hold, now)


async def validate_pre_checkout(session: AsyncSession, *, payload: str, user_id: int, amount: int, currency: str,
                                secret: str, now: datetime) -> str | None:
    order_id = parse_payload(payload, secret)
    order = await sales_repo.get_order(session, order_id) if order_id is not None else None
    if order is None or order.user_id != user_id:
        return "Счёт не найден — выберите места заново."
    if order.status is not OrderStatus.PENDING:
        return "Этот счёт уже оплачен."
    if currency != STARS or amount != order.amount:
        return "Сумма изменилась — выберите места заново."
    hold = await sales_repo.get_hold(session, order.hold_id)
    if hold is None or hold.status is not HoldStatus.ACTIVE or hold.expires_at <= now:
        return "Время брони истекло — выберите места заново."
    await sales_repo.extend_hold(session, hold.id, now + PAYMENT_GRACE)
    return None


@dataclass
class PaymentOutcome:
    order: Order | None
    already_processed: bool = False
    tickets: list[Ticket] = field(default_factory=list)
    needs_refund: bool = False  # деньги списаны, а места уже не удержать — вернуть звёзды


async def confirm_payment(session: AsyncSession, *, payload: str, user_id: int, amount: int, currency: str,
                          charge_id: str, secret: str, now: datetime) -> PaymentOutcome:
    duplicate = await sales_repo.order_by_charge(session, charge_id)
    if duplicate is not None:
        return PaymentOutcome(duplicate, already_processed=True)
    order_id = parse_payload(payload, secret)
    order = await sales_repo.get_order(session, order_id) if order_id is not None else None
    if order is None or order.user_id != user_id or order.amount != amount or currency != STARS:
        logger.error("Платёж не совпал с заказом", extra={"order_id": order_id})
        return PaymentOutcome(None, needs_refund=True)
    if not await sales_repo.mark_paid(session, order.id, charge_id, now):
        return PaymentOutcome(order, already_processed=True)

    hold = await sales_repo.get_hold(session, order.hold_id)
    keys = await sales_repo.hold_seat_keys(session, hold.id)
    sold = await sales_repo.sell_hold(session, hold.id)
    if sold != hold.seats_count or not keys:
        logger.error("Места брони не удалось продать", extra={"order_id": order.id, "sold": sold})
        return PaymentOutcome(order, needs_refund=True)
    tickets = [await sales_repo.add_ticket(session, code=new_code(), show_id=hold.show_id, seat_key=key,
                                           user_id=user_id, order_id=order.id, now=now) for key in keys]
    return PaymentOutcome(await sales_repo.get_order(session, order.id), tickets=tickets)
