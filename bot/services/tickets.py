"""Билеты: случайный код + HMAC-подпись в QR и проверка на входе.

В QR-коде — «<код>.<подпись>». Подпись отсекает выдуманные коды без запроса в БД, а сам код
случайный (не номер по порядку), поэтому угадать чужой билет невозможно. Отметка «прошёл» —
атомарная: один билет нельзя пропустить дважды.
"""

from __future__ import annotations

import hashlib
import hmac
import io
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

import qrcode
from sqlalchemy.ext.asyncio import AsyncSession

from bot.repositories import sales as sales_repo
from bot.repositories import shows as shows_repo
from bot.services.booking import seats_text

CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 12
ENTRY_OPENS_BEFORE = timedelta(hours=3)


def new_code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))


def _signature(code: str, secret: str) -> str:
    return hmac.new(secret.encode(), f"ticket:{code}".encode(), hashlib.sha256).hexdigest()[:12]


def qr_payload(code: str, secret: str) -> str:
    return f"{code}.{_signature(code, secret)}"


def verify_payload(raw: str, secret: str) -> str | None:
    """Код билета из QR («КОД.подпись») или введённый контролёром вручную («КОД»).

    Подпись, если она есть, обязана сойтись — подделанный QR отсекается без запроса в БД.
    Ручной ввод без подписи доступен только контролёрам, а решает всё равно наличие билета в БД.
    """
    code, separator, signature = raw.strip().partition(".")
    code = code.upper()
    if len(code) != CODE_LENGTH or any(char not in CODE_ALPHABET for char in code):
        return None
    if separator and not hmac.compare_digest(_signature(code, secret), signature.lower()):
        return None
    return code


def qr_png(payload: str) -> bytes:
    image = qrcode.make(payload, box_size=10, border=2)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


@dataclass(frozen=True)
class CheckResult:
    ok: bool
    message: str


async def check_in(session: AsyncSession, raw: str, staff_id: int, secret: str, now: datetime) -> CheckResult:
    code = verify_payload(raw, secret)
    if code is None:
        return CheckResult(False, "❌ Недействительный код")
    ticket = await sales_repo.ticket_by_code(session, code)
    if ticket is None:
        return CheckResult(False, "❌ Билет не найден (возможно, возвращён)")
    show = await shows_repo.get(session, ticket.show_id)
    if now < show.starts_at - ENTRY_OPENS_BEFORE or now > show.ends_at:
        return CheckResult(False, f"⚠️ Билет на другой сеанс: {show.title}")
    seat = seats_text([ticket.seat_key])
    if not await sales_repo.mark_used(session, ticket.id, staff_id, now):
        return CheckResult(False, f"⚠️ Билет уже использован ({seat})")
    return CheckResult(True, f"✅ Проходите: {seat}")
