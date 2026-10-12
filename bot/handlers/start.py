"""/start с кнопкой мини-приложения, /tickets, /paysupport."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandStart
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, WebAppInfo
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import Settings
from bot.core.clock import utcnow
from bot.gateways.ticket_sender import send_tickets
from bot.repositories import sales as sales_repo

router = Router(name="start")
router.message.filter(F.chat.type == ChatType.PRIVATE)


def app_keyboard(settings: Settings) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🎟 Выбрать места", web_app=WebAppInfo(url=settings.webapp_url)),
    ]])


@router.message(CommandStart())
async def on_start(message: Message, settings: Settings) -> None:
    await message.answer(
        "Добро пожаловать! Здесь можно выбрать места на схеме зала и купить билеты за звёзды Telegram.\n"
        "Билеты с QR-кодами придут сюда же — покажите их на входе.",
        reply_markup=app_keyboard(settings),
    )


@router.message(Command("tickets"))
async def on_tickets(message: Message, session: AsyncSession, settings: Settings) -> None:
    items = await sales_repo.tickets_for_user(session, message.from_user.id, utcnow())
    if not items:
        await message.answer("Билетов на будущие сеансы нет.", reply_markup=app_keyboard(settings))
        return
    await send_tickets(message.bot, message.chat.id, items[:20], settings.ticket_secret.get_secret_value(),
                       settings.tz)


@router.message(Command("paysupport"))
async def on_paysupport(message: Message) -> None:
    await message.answer("💬 Вопросы по оплате и возврату — напишите кассе зала и укажите ваш ID "
                         f"<code>{message.from_user.id}</code>. Возврат возможен не позднее чем за сутки до сеанса.")
