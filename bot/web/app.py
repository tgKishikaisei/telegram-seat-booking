"""Сборка веб-приложения: общие ресурсы (БД, Redis, Bot API) создаются при старте и закрываются при остановке."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from pathlib import Path

from aiogram import Bot
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from bot import models  # noqa: F401 — регистрирует таблицы
from bot.config import Settings
from bot.core import database
from bot.core import redis as redis_core
from bot.core.logging import setup_logging
from bot.core.ratelimit import RateLimiter
from bot.core.web import TelegramAuth, create_app
from bot.services.venue import load_venue
from bot.web.api import router as api_router

STATIC_DIR = Path(__file__).resolve().parent / "static"
PREFIX = "seats"


def create_web_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    setup_logging(settings.log_level, settings.secret_values(), settings.json_logs)
    engine = database.create_engine(settings.database_dsn, settings.db_pool_size)
    state: dict[str, object] = {}

    async def db_ready() -> bool:
        return await database.ping(engine)

    async def redis_ready() -> bool:
        return await redis_core.ping(state.get("redis"))  # type: ignore[arg-type]

    app = create_app(title="Seat booking", allowed_hosts=[settings.public_host, "localhost", "127.0.0.1"],
                     production=settings.is_production,
                     readiness_checks={"database": db_ready, "redis": redis_ready})

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        if not settings.is_production:
            await database.create_tables(engine)
        redis = await redis_core.create_redis(settings.redis_dsn)
        state["redis"] = redis
        bot = Bot(settings.bot_token.get_secret_value())
        app.state.settings = settings
        app.state.session_factory = database.create_session_factory(engine)
        app.state.limiter = RateLimiter(redis, PREFIX)
        app.state.bot = bot
        app.state.venue = load_venue()
        app.state.auth = TelegramAuth(settings.bot_token.get_secret_value(), settings.init_data_ttl_seconds)
        try:
            yield
        finally:
            await bot.session.close()
            if redis is not None:
                await redis.aclose()
            await engine.dispose()

    app.router.lifespan_context = lifespan
    app.include_router(api_router)
    app.mount("/app", StaticFiles(directory=STATIC_DIR, html=True), name="app")
    return app
