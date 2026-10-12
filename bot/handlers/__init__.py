"""Хендлеры бота: старт с кнопкой мини-приложения, оплата, билеты, контроль на входе, администрирование.

Выбор мест и оплата идут в мини-приложении (bot/web); бот принимает подтверждение оплаты
и присылает билеты с QR-кодами.
"""

from aiogram import Router
from aiogram.types import BotCommand

from bot.handlers import admin, payments, staff, start

COMMANDS = [
    BotCommand(command="start", description="Афиша и выбор мест"),
    BotCommand(command="tickets", description="Мои билеты с QR-кодами"),
    BotCommand(command="paysupport", description="Вопросы по оплате"),
]


def get_routers() -> list[Router]:
    return [payments.router, staff.router, admin.router, start.router]
