"""Сеансы и места."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bot.core.database import insert_or_ignore
from bot.models import SeatStatus, Show, ShowSeat


async def ensure_show(session: AsyncSession, *, event_code: str, title: str, description: str, hall_code: str,
                      starts_at: datetime, ends_at: datetime, seats: list[dict[str, object]]) -> Show:
    """Создаёт сеанс и его места, если их ещё нет; у существующего обновляет название и описание."""
    created = await insert_or_ignore(session, Show, {
        "event_code": event_code, "title": title, "description": description, "hall_code": hall_code,
        "starts_at": starts_at, "ends_at": ends_at, "active": True}, ["event_code", "starts_at"])
    show = await session.scalar(select(Show).where(Show.event_code == event_code, Show.starts_at == starts_at))
    if not created:
        show.title, show.description, show.active = title, description, True
    for seat in seats:  # {"seat_key", "row", "number", "zone", "price"}
        await insert_or_ignore(session, ShowSeat, {"show_id": show.id, "status": SeatStatus.FREE, **seat},
                               ["show_id", "seat_key"])
    await session.flush()
    return show


async def deactivate_missing(session: AsyncSession, keep_ids: set[int]) -> None:
    await session.execute(update(Show).where(Show.id.not_in(keep_ids)).values(active=False))


async def upcoming(session: AsyncSession, now: datetime, limit: int = 30) -> list[Show]:
    result = await session.scalars(
        select(Show).where(Show.active.is_(True), Show.starts_at > now).order_by(Show.starts_at).limit(limit)
    )
    return list(result)


async def get(session: AsyncSession, show_id: int) -> Show | None:
    return await session.get(Show, show_id)


async def seats(session: AsyncSession, show_id: int) -> list[ShowSeat]:
    result = await session.scalars(select(ShowSeat).where(ShowSeat.show_id == show_id)
                                   .execution_options(populate_existing=True))
    return list(result)


async def free_counts(session: AsyncSession, show_ids: list[int], now: datetime) -> dict[int, int]:
    if not show_ids:
        return {}
    available = (ShowSeat.status == SeatStatus.FREE) | ((ShowSeat.status == SeatStatus.HELD)
                                                        & (ShowSeat.held_until < now))
    result = await session.execute(
        select(ShowSeat.show_id, func.count()).where(ShowSeat.show_id.in_(show_ids), available)
        .group_by(ShowSeat.show_id)
    )
    return {show_id: count for show_id, count in result.all()}
