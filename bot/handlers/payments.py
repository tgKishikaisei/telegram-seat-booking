"""Оплата из мини-приложения: pre_checkout продлевает бронь, successful_payment выдаёт билеты.

Если деньги списались, а места удержать не удалось (редкий случай: бронь истекла посреди оплаты
и места заняли), звёзды возвращаются автоматически — зритель не платит за то, чего не получил.
"""

from __future__ import annotations

import contextlib
import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import Message, PreCheckoutQuery
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import Settings
from bot.core.clock import utcnow
from bot.gateways.ticket_sender import send_tickets
from bot.repositories import shows as shows_repo
from bot.services.payments import confirm_payment, validate_pre_checkout

router = Router(name="payments")
logger = logging.getLogger(__name__)


@router.pre_checkout_query()
async def on_pre_checkout(query: PreCheckoutQuery, session: AsyncSession, settings: Settings) -> None:
    error = await validate_pre_checkout(
        session, payload=query.invoice_payload, user_id=query.from_user.id, amount=query.total_amount,
        currency=query.currency, secret=settings.ticket_secret.get_secret_value(), now=utcnow(),
    )
    await session.commit()  # продление брони сохраняем до ответа Telegram
    await query.answer(ok=error is None, error_message=error)


@router.message(F.successful_payment)
async def on_paid(message: Message, session: AsyncSession, settings: Settings, bot: Bot) -> None:
    payment = message.successful_payment
    outcome = await confirm_payment(
        session, payload=payment.invoice_payload, user_id=message.from_user.id, amount=payment.total_amount,
        currency=payment.currency, charge_id=payment.telegram_payment_charge_id,
        secret=settings.ticket_secret.get_secret_value(), now=utcnow(),
    )
    if outcome.already_processed:
        await session.commit()
        await message.answer("Эта оплата уже учтена — билеты: /tickets")
        return
    if outcome.needs_refund:
        await session.rollback()  # места не продаём, заказ не трогаем
        try:
            await bot.refund_star_payment(message.from_user.id, payment.telegram_payment_charge_id)
            await message.answer("😔 Пока шла оплата, выбранные места заняли. Звёзды вернулись на ваш счёт — "
                                 "выберите другие места.")
        except TelegramAPIError:
            logger.exception("Автовозврат не выполнен", extra={"charge_id": payment.telegram_payment_charge_id})
            for admin_id in settings.admin_ids:
                with contextlib.suppress(TelegramAPIError):
                    await bot.send_message(admin_id, "⚠️ Нужен ручной возврат звёзд: пользователь "
                                                     f"<code>{message.from_user.id}</code>")
            await message.answer("Оплата прошла, но места не удалось закрепить. Касса вернёт звёзды вручную.")
        return
    await session.commit()
    show = await shows_repo.get(session, outcome.tickets[0].show_id)
    await message.answer(f"🎉 Оплата прошла! Билетов: {len(outcome.tickets)}. Покажите QR-коды на входе.")
    await send_tickets(bot, message.chat.id, [(ticket, show) for ticket in outcome.tickets],
                       settings.ticket_secret.get_secret_value(), settings.tz)
