"""Отправка билетов: на каждое место — картинка с QR-кодом и подписью."""

from __future__ import annotations

import asyncio
from html import escape
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.types import BufferedInputFile

from bot.core.clock import to_local
from bot.models import Show, Ticket
from bot.services.booking import seats_text
from bot.services.tickets import qr_payload, qr_png


async def send_tickets(bot: Bot, chat_id: int, tickets: list[tuple[Ticket, Show]], secret: str, tz: ZoneInfo) -> None:
    for ticket, show in tickets:
        png = await asyncio.to_thread(qr_png, qr_payload(ticket.code, secret))
        when = to_local(show.starts_at, tz).strftime("%d.%m.%Y в %H:%M")
        status = "\n⚠️ Уже использован" if ticket.used_at else ""
        await bot.send_photo(
            chat_id, BufferedInputFile(png, filename=f"ticket-{ticket.code}.png"),
            caption=f"🎟 <b>{escape(show.title)}</b>\n{when}\n{seats_text([ticket.seat_key]).capitalize()}\n"
                    f"Билет {ticket.code}{status}",
        )
