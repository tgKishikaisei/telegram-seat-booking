"""Фоновые задачи: истёкшие брони освобождают места; напоминание о сеансе за несколько часов."""

from __future__ import annotations

import logging
from datetime import timedelta
from html import escape

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.config import Settings
from bot.core.clock import to_local, utcnow
from bot.repositories import sales as sales_repo

logger = logging.getLogger(__name__)


async def expire_holds(session_factory: async_sessionmaker[AsyncSession]) -> None:
    async with session_factory() as session:
        count = await sales_repo.expire_holds(session, utcnow())
        await session.commit()
    if count:
        logger.info("Истекло броней: %s", count)


async def remind(bot: Bot, session_factory: async_sessionmaker[AsyncSession], settings: Settings) -> None:
    now = utcnow()
    async with session_factory() as session:
        for order, show in await sales_repo.due_reminders(session, now, now + timedelta(hours=settings.reminder_hours)):
            if not await sales_repo.claim_reminder(session, order.id):
                continue
            await session.commit()
            when = to_local(show.starts_at, settings.tz).strftime("%H:%M")
            try:
                await bot.send_message(order.user_id, f"⏰ Сегодня в {when} — «{escape(show.title)}». "
                                                      "Билеты с QR-кодами: /tickets")
            except TelegramAPIError:
                logger.info("Напоминание не доставлено", extra={"order_id": order.id})
