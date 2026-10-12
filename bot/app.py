"""Сборка бота (процесс бота; веб-часть — отдельный процесс python -m bot.web):
настройки → БД и Redis → афиша из venue.json → кнопка меню → диспетчер → задачи → запуск."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from functools import partial

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import MenuButtonWebApp, WebAppInfo

from bot import models  # noqa: F401 — регистрирует таблицы
from bot.config import Settings
from bot.core.database import create_engine, create_session_factory, create_tables
from bot.core.errors import register_error_handler
from bot.core.heartbeat import run_heartbeat
from bot.core.logging import setup_logging
from bot.core.middlewares import DbSessionMiddleware, RequestIdMiddleware, ThrottlingMiddleware
from bot.core.ratelimit import RateLimiter
from bot.core.redis import create_fsm_storage, create_redis
from bot.core.scheduler import PeriodicJobs
from bot.handlers import COMMANDS, get_routers
from bot.jobs.booking import expire_holds, remind
from bot.services.booking import sync_venue
from bot.services.venue import load_venue

logger = logging.getLogger(__name__)

PREFIX = "seats"


async def main() -> None:
    settings = Settings()
    setup_logging(settings.log_level, settings.secret_values(), settings.json_logs)

    engine = create_engine(settings.database_dsn, settings.db_pool_size)
    if not settings.is_production:
        await create_tables(engine)  # в продакшене схему создаёт alembic upgrade head
    session_factory = create_session_factory(engine)
    async with session_factory() as session:
        count = await sync_venue(session, load_venue(), settings.tz)
        await session.commit()
    logger.info("Афиша загружена: сеансов %s", count)

    redis = await create_redis(settings.redis_dsn)
    limiter = RateLimiter(redis, PREFIX)
    bot = Bot(settings.bot_token.get_secret_value(), default=DefaultBotProperties(parse_mode=ParseMode.HTML))

    dispatcher = Dispatcher(storage=create_fsm_storage(redis, PREFIX), settings=settings, limiter=limiter)
    dispatcher.update.outer_middleware(RequestIdMiddleware())
    dispatcher.update.middleware(DbSessionMiddleware(session_factory))
    dispatcher.message.middleware(ThrottlingMiddleware(limiter, limit=6, period=3))
    dispatcher.include_routers(*get_routers())
    register_error_handler(dispatcher)

    jobs = PeriodicJobs(redis, PREFIX)
    jobs.add("expire_holds", 30, partial(expire_holds, session_factory))
    jobs.add("remind", 300, partial(remind, bot, session_factory, settings))
    heartbeat = asyncio.create_task(run_heartbeat())

    try:
        await bot.set_my_commands(COMMANDS)
        await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="Афиша",
                                                                    web_app=WebAppInfo(url=settings.webapp_url)))
        jobs.start()
        await dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types())
    finally:
        await jobs.stop()
        heartbeat.cancel()
        await bot.session.close()
        with contextlib.suppress(Exception):
            await dispatcher.storage.close()
        if redis is not None:
            with contextlib.suppress(Exception):
                await redis.aclose()
        await engine.dispose()
