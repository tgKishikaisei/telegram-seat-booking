"""Администратор: продажи по сеансам и возврат заказа (звёзды обратно, места свободны)."""

from __future__ import annotations

import logging
from html import escape

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import BaseFilter, Command, CommandObject
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import Settings
from bot.core.clock import to_local, utcnow
from bot.models import OrderStatus
from bot.repositories import sales as sales_repo

router = Router(name="admin")
logger = logging.getLogger(__name__)


class IsAdmin(BaseFilter):
    async def __call__(self, message: Message, settings: Settings) -> bool:
        return message.from_user is not None and message.from_user.id in settings.admin_ids


router.message.filter(F.chat.type == ChatType.PRIVATE, IsAdmin())


@router.message(Command("sales"))
async def on_sales(message: Message, session: AsyncSession, settings: Settings) -> None:
    rows = await sales_repo.sales_by_show(session, utcnow())
    if not rows:
        await message.answer("Будущих сеансов нет.")
        return
    lines = ["🎭 <b>Продажи</b>"]
    for show, sold, revenue in rows:
        when = to_local(show.starts_at, settings.tz).strftime("%d.%m %H:%M")
        lines.append(f"{when} · {escape(show.title)} — продано {sold}, {revenue} ⭐")
    await message.answer("\n".join(lines))


@router.message(Command("refund"))
async def on_refund(message: Message, command: CommandObject, session: AsyncSession, bot: Bot) -> None:
    raw = (command.args or "").strip()
    order = await sales_repo.get_order(session, int(raw)) if raw.isdigit() else None
    if order is None or order.status is not OrderStatus.PAID or not order.telegram_charge_id:
        await message.answer("Формат: /refund НОМЕР_ЗАКАЗА (только оплаченный)")
        return
    try:
        await bot.refund_star_payment(order.user_id, order.telegram_charge_id)
    except TelegramAPIError as exc:
        await message.answer(f"Telegram не выполнил возврат: {escape(exc.message)}")
        return
    await sales_repo.refund_order(session, order.id)
    await session.commit()
    logger.info("Возврат заказа", extra={"order_id": order.id, "admin": message.from_user.id})
    await message.answer(f"↩️ Заказ №{order.id}: звёзды возвращены, места снова в продаже, билеты аннулированы.")
