"""Контроль на входе без камеры: /check КОД (код напечатан под QR). Только STAFF_IDS и ADMIN_IDS."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import BaseFilter, Command, CommandObject
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import Settings
from bot.core.clock import utcnow
from bot.services.tickets import check_in

router = Router(name="staff")


class IsStaff(BaseFilter):
    async def __call__(self, message: Message, settings: Settings) -> bool:
        return message.from_user is not None and settings.is_staff(message.from_user.id)


router.message.filter(F.chat.type == ChatType.PRIVATE, IsStaff())


@router.message(Command("check"))
async def on_check(message: Message, command: CommandObject, session: AsyncSession, settings: Settings) -> None:
    raw = (command.args or "").strip()
    if not raw:
        await message.answer("Формат: /check КОД — или сканируйте QR в мини-приложении («Проверка билетов»).")
        return
    result = await check_in(session, raw, message.from_user.id, settings.ticket_secret.get_secret_value(), utcnow())
    await message.answer(result.message)
