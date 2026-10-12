"""Афиша, схема зала и временная бронь мест.

Бронь: зритель выбирает до MAX_SEATS мест → они закрепляются за ним на HOLD_MINUTES одним
атомарным UPDATE. Если хотя бы одно место успел занять кто-то другой, бронь целиком отменяется
(транзакция откатывается) — никаких «половинчатых» броней.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from bot.core.clock import to_local
from bot.models import Hold, HoldStatus, SeatStatus, Show
from bot.repositories import sales as sales_repo
from bot.repositories import shows as shows_repo
from bot.services.venue import Venue, layout_of, seats_of, session_start_utc

MAX_SEATS = 6
SEAT_KEY_RE = re.compile(r"^r\d{1,2}-s\d{1,3}$")
SALES_CLOSE_BEFORE = timedelta(minutes=15)


class BookingError(Exception):
    """Текст показывается зрителю."""


class SeatsTaken(BookingError):
    """Часть мест уже заняли — транзакцию нужно откатить."""


async def sync_venue(session: AsyncSession, venue: Venue, tz: ZoneInfo) -> int:
    """Сеансы из venue.json → БД (с местами). Исчезнувшие из файла сеансы скрываются."""
    keep: set[int] = set()
    for event in venue.events:
        hall = venue.halls[event.hall]
        seats = [{"seat_key": s.key, "row": s.row, "number": s.number, "zone": s.zone, "price": s.price}
                 for s in seats_of(hall)]
        for local_start in event.sessions:
            starts = session_start_utc(local_start, tz)
            show = await shows_repo.ensure_show(
                session, event_code=event.code, title=event.title, description=event.description,
                hall_code=event.hall, starts_at=starts, ends_at=starts + timedelta(minutes=event.duration_min),
                seats=seats)
            keep.add(show.id)
    await shows_repo.deactivate_missing(session, keep)
    return len(keep)


def show_view(show: Show, free: int, tz: ZoneInfo) -> dict[str, object]:
    local = to_local(show.starts_at, tz)
    return {"id": show.id, "title": show.title, "description": show.description, "starts_at": local.isoformat(),
            "date": local.strftime("%d.%m"), "time": local.strftime("%H:%M"), "free": free}


async def seat_map(session: AsyncSession, venue: Venue, show: Show, user_id: int, now: datetime) -> dict[str, object]:
    """Схема для мини-приложения. Чужие брони и проданные места — просто «занято»: кто их держит, не раскрываем."""
    hall = venue.halls[show.hall_code]
    my_hold = await sales_repo.active_hold(session, user_id, show.id, now)
    states: dict[str, str] = {}
    for seat in await shows_repo.seats(session, show.id):
        held_live = seat.status is SeatStatus.HELD and seat.held_until is not None and seat.held_until > now
        if my_hold is not None and seat.hold_id == my_hold.id and held_live:
            states[seat.seat_key] = "mine"
        elif seat.status is SeatStatus.SOLD or held_live:
            states[seat.seat_key] = "taken"
        else:
            states[seat.seat_key] = "free"
    zones = {code: {"name": zone.name, "price": zone.price} for code, zone in hall.zones.items()}
    rows = []
    for index, cells in enumerate(layout_of(hall), start=1):
        zone_row = hall.rows[index - 1].replace("_", "")
        seats = []
        position = 0
        for key in cells:
            if key is None:
                seats.append(None)
                continue
            seats.append({"key": key, "n": position + 1, "zone": zone_row[position], "state": states.get(key, "taken")})
            position += 1
        rows.append({"label": str(index), "seats": seats})
    hold = None
    if my_hold is not None:
        hold = {"id": my_hold.public_id, "seats": await sales_repo.hold_seat_keys(session, my_hold.id),
                "total": my_hold.total, "expires_at": my_hold.expires_at.isoformat() + "Z"}
    return {"show": {"id": show.id, "title": show.title}, "hall": hall.name, "zones": zones, "rows": rows,
            "max_seats": MAX_SEATS, "hold": hold}


async def hold_seats(session: AsyncSession, show_id: int, user_id: int, seat_keys: list[str], now: datetime,
                     hold_minutes: int) -> Hold:
    show = await shows_repo.get(session, show_id)
    if show is None or not show.active:
        raise BookingError("Сеанс не найден")
    if show.starts_at - now < SALES_CLOSE_BEFORE:
        raise BookingError("Продажа на этот сеанс закрыта")
    unique = list(dict.fromkeys(seat_keys))
    if not 1 <= len(unique) <= MAX_SEATS or any(not SEAT_KEY_RE.match(key) for key in unique):
        raise BookingError(f"Выберите от 1 до {MAX_SEATS} мест")

    await sales_repo.release_user_holds(session, user_id, show_id)  # новая бронь заменяет прежнюю
    hold = await sales_repo.create_hold(session, public_id=secrets.token_hex(16), show_id=show_id, user_id=user_id,
                                        now=now, expires_at=now + timedelta(minutes=hold_minutes))
    if await sales_repo.take_seats(session, hold, unique, now) != len(unique):
        raise SeatsTaken("Часть мест только что заняли — обновите схему и выберите другие")
    return await sales_repo.finalize_hold(session, hold.id)


async def cancel_hold(session: AsyncSession, public_id: str, user_id: int) -> None:
    hold = await sales_repo.by_public_id(session, public_id)
    if hold is None or hold.user_id != user_id or hold.status is not HoldStatus.ACTIVE:
        raise BookingError("Бронь не найдена")
    await sales_repo.release_hold(session, hold.id)


@dataclass(frozen=True)
class SeatLabel:
    row: int
    number: int


def parse_seat_key(key: str) -> SeatLabel:
    row, number = key.removeprefix("r").split("-s")
    return SeatLabel(int(row), int(number))


def seats_text(keys: list[str]) -> str:
    labels = [parse_seat_key(key) for key in keys]
    return ", ".join(f"ряд {label.row}, место {label.number}" for label in labels)
