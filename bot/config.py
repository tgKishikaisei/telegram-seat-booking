"""Настройки бронирования мест. Значения — из окружения, /run/secrets и .env (см. .env.example)."""

from __future__ import annotations

from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from pydantic import Field, SecretStr, field_validator

from bot.core.config_types import IdSet, TimezoneName
from bot.core.settings import BaseBotSettings


class Settings(BaseBotSettings):
    ticket_secret: SecretStr = Field(min_length=32)   # подпись счетов и QR-кодов билетов
    public_url: str                                   # https-адрес мини-приложения (домен из Caddy)
    timezone: TimezoneName = "Asia/Tashkent"
    hold_minutes: int = Field(default=10, ge=3, le=30)
    staff_ids: IdSet = frozenset()                    # контролёры на входе (плюс ADMIN_IDS)
    init_data_ttl_seconds: int = Field(default=6 * 3600, ge=300, le=86400)
    reminder_hours: int = Field(default=3, ge=1, le=48)

    @field_validator("public_url")
    @classmethod
    def _https(cls, value: str) -> str:
        value = value.rstrip("/")
        parts = urlsplit(value)
        if parts.scheme != "https" or not parts.hostname:
            raise ValueError("PUBLIC_URL — адрес https://… (Telegram открывает мини-приложения только по https)")
        return value

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def webapp_url(self) -> str:
        return f"{self.public_url}/app/"

    @property
    def public_host(self) -> str:
        return urlsplit(self.public_url).hostname or ""

    def is_staff(self, user_id: int) -> bool:
        return user_id in self.staff_ids or user_id in self.admin_ids
