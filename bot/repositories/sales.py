"""Брони, заказы и билеты. Все переходы статусов — атомарные UPDATE с проверкой текущего статуса."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import and_, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bot.models import Hold, HoldStatus, Order, OrderStatus, SeatStatus, Show, ShowSeat, Ticket


def _seat_available(now: datetime):  # type: ignore[no-untyped-def]
    return (ShowSeat.status == SeatStatus.FREE) | ((ShowSeat.status == SeatStatus.HELD) & (ShowSeat.held_until < now))


async def create_hold(session: AsyncSession, *, public_id: str, show_id: int, user_id: int, now: datetime,
                      expires_at: datetime) -> Hold:
    hold = Hold(public_id=public_id, show_id=show_id, user_id=user_id, seats_count=0, total=0,
                created_at=now, expires_at=expires_at)
    session.add(hold)
    await session.flush()
    return hold


async def take_seats(session: AsyncSession, hold: Hold, seat_keys: list[str], now: datetime) -> int:
    """Пытается занять места под бронь. Возвращает, сколько удалось (меньше запрошенного — кто-то успел раньше)."""
    result = await session.execute(
        update(ShowSeat)
        .where(ShowSeat.show_id == hold.show_id, ShowSeat.seat_key.in_(seat_keys), _seat_available(now))
        .values(status=SeatStatus.HELD, hold_id=hold.id, held_until=hold.expires_at)
        .execution_options(synchronize_session=False)
    )
    return result.rowcount or 0


async def finalize_hold(session: AsyncSession, hold_id: int) -> Hold:
    total = await session.scalar(select(func.coalesce(func.sum(ShowSeat.price), 0)).where(ShowSeat.hold_id == hold_id))
    count = await session.scalar(select(func.count()).select_from(ShowSeat).where(ShowSeat.hold_id == hold_id))
    await session.execute(update(Hold).where(Hold.id == hold_id).values(total=total, seats_count=count))
    return await session.get(Hold, hold_id, populate_existing=True)


async def by_public_id(session: AsyncSession, public_id: str) -> Hold | None:
    return await session.scalar(select(Hold).where(Hold.public_id == public_id)
                                .execution_options(populate_existing=True))


async def get_hold(session: AsyncSession, hold_id: int) -> Hold | None:
    return await session.get(Hold, hold_id, populate_existing=True)


async def active_hold(session: AsyncSession, user_id: int, show_id: int, now: datetime) -> Hold | None:
    return await session.scalar(
        select(Hold).where(Hold.user_id == user_id, Hold.show_id == show_id, Hold.status == HoldStatus.ACTIVE,
                           Hold.expires_at > now).order_by(Hold.id.desc()).limit(1)
    )


async def hold_seat_keys(session: AsyncSession, hold_id: int) -> list[str]:
    result = await session.scalars(select(ShowSeat.seat_key).where(ShowSeat.hold_id == hold_id)
                                   .order_by(ShowSeat.row, ShowSeat.number))
    return list(result)


async def release_hold(session: AsyncSession, hold_id: int, status: HoldStatus = HoldStatus.RELEASED) -> bool:
    result = await session.execute(update(Hold).where(Hold.id == hold_id, Hold.status == HoldStatus.ACTIVE)
                                   .values(status=status).execution_options(synchronize_session=False))
    if result.rowcount != 1:
        return False
    await session.execute(update(ShowSeat).where(ShowSeat.hold_id == hold_id, ShowSeat.status == SeatStatus.HELD)
                          .values(status=SeatStatus.FREE, hold_id=None, held_until=None)
                          .execution_options(synchronize_session=False))
    return True


async def release_user_holds(session: AsyncSession, user_id: int, show_id: int) -> None:
    ids = await session.scalars(select(Hold.id).where(Hold.user_id == user_id, Hold.show_id == show_id,
                                                      Hold.status == HoldStatus.ACTIVE))
    for hold_id in list(ids):
        await release_hold(session, hold_id)


async def extend_hold(session: AsyncSession, hold_id: int, until: datetime) -> None:
    """На время оплаты (после pre_checkout) бронь продлевается, чтобы не истечь посреди платежа."""
    await session.execute(update(Hold).where(Hold.id == hold_id, Hold.status == HoldStatus.ACTIVE)
                          .values(expires_at=until))
    await session.execute(update(ShowSeat).where(ShowSeat.hold_id == hold_id, ShowSeat.status == SeatStatus.HELD)
                          .values(held_until=until))


async def expire_holds(session: AsyncSession, now: datetime) -> int:
    expired = list(await session.scalars(select(Hold.id).where(Hold.status == HoldStatus.ACTIVE,
                                                              Hold.expires_at < now)))
    for hold_id in expired:
        await release_hold(session, hold_id, HoldStatus.EXPIRED)
    return len(expired)


async def create_order(session: AsyncSession, hold: Hold, now: datetime) -> Order:
    existing = await session.scalar(select(Order).where(Order.hold_id == hold.id))
    if existing is not None:
        return existing
    order = Order(hold_id=hold.id, user_id=hold.user_id, amount=hold.total, created_at=now)
    session.add(order)
    await session.flush()
    return order


async def get_order(session: AsyncSession, order_id: int) -> Order | None:
    return await session.get(Order, order_id, populate_existing=True)


async def order_by_charge(session: AsyncSession, charge_id: str) -> Order | None:
    return await session.scalar(select(Order).where(Order.telegram_charge_id == charge_id))


async def mark_paid(session: AsyncSession, order_id: int, charge_id: str, now: datetime) -> bool:
    result = await session.execute(
        update(Order).where(Order.id == order_id, Order.status == OrderStatus.PENDING)
        .values(status=OrderStatus.PAID, telegram_charge_id=charge_id, paid_at=now)
        .execution_options(synchronize_session=False)
    )
    return result.rowcount == 1


async def sell_hold(session: AsyncSession, hold_id: int) -> int:
    """Бронь → проданные места. Возвращает число проданных мест."""
    result = await session.execute(
        update(ShowSeat).where(ShowSeat.hold_id == hold_id, ShowSeat.status == SeatStatus.HELD)
        .values(status=SeatStatus.SOLD, held_until=None).execution_options(synchronize_session=False)
    )
    await session.execute(update(Hold).where(Hold.id == hold_id).values(status=HoldStatus.PAID))
    return result.rowcount or 0


async def add_ticket(session: AsyncSession, *, code: str, show_id: int, seat_key: str, user_id: int, order_id: int,
                     now: datetime) -> Ticket:
    ticket = Ticket(code=code, show_id=show_id, seat_key=seat_key, user_id=user_id, order_id=order_id, issued_at=now)
    session.add(ticket)
    await session.flush()
    return ticket


async def tickets_for_user(session: AsyncSession, user_id: int, now: datetime) -> list[tuple[Ticket, Show]]:
    result = await session.execute(
        select(Ticket, Show).join(Show, Show.id == Ticket.show_id)
        .where(Ticket.user_id == user_id, Show.ends_at > now).order_by(Show.starts_at, Ticket.seat_key)
    )
    return [(ticket, show) for ticket, show in result.all()]


async def tickets_for_order(session: AsyncSession, order_id: int) -> list[Ticket]:
    return list(await session.scalars(select(Ticket).where(Ticket.order_id == order_id).order_by(Ticket.id)))


async def ticket_by_code(session: AsyncSession, code: str) -> Ticket | None:
    return await session.scalar(select(Ticket).where(Ticket.code == code).execution_options(populate_existing=True))


async def mark_used(session: AsyncSession, ticket_id: int, staff_id: int, now: datetime) -> bool:
    result = await session.execute(
        update(Ticket).where(Ticket.id == ticket_id, Ticket.used_at.is_(None))
        .values(used_at=now, used_by=staff_id).execution_options(synchronize_session=False)
    )
    return result.rowcount == 1


async def refund_order(session: AsyncSession, order_id: int) -> bool:
    """Возврат: заказ → refunded, билеты удаляются, места снова свободны."""
    result = await session.execute(update(Order).where(Order.id == order_id, Order.status == OrderStatus.PAID)
                                   .values(status=OrderStatus.REFUNDED).execution_options(synchronize_session=False))
    if result.rowcount != 1:
        return False
    order = await get_order(session, order_id)
    tickets = await tickets_for_order(session, order_id)
    for ticket in tickets:
        await session.delete(ticket)
    await session.execute(update(ShowSeat).where(ShowSeat.hold_id == order.hold_id)
                          .values(status=SeatStatus.FREE, hold_id=None, held_until=None))
    await session.flush()
    return True


async def due_reminders(session: AsyncSession, start_from: datetime, start_to: datetime) -> list[tuple[Order, Show]]:
    result = await session.execute(
        select(Order, Show).join(Hold, Hold.id == Order.hold_id).join(Show, Show.id == Hold.show_id)
        .where(Order.status == OrderStatus.PAID, Order.reminded.is_(False),
               and_(Show.starts_at > start_from, Show.starts_at <= start_to))
    )
    return [(order, show) for order, show in result.all()]


async def claim_reminder(session: AsyncSession, order_id: int) -> bool:
    result = await session.execute(update(Order).where(Order.id == order_id, Order.reminded.is_(False))
                                   .values(reminded=True).execution_options(synchronize_session=False))
    return result.rowcount == 1


async def sales_by_show(session: AsyncSession, now: datetime) -> list[tuple[Show, int, int]]:
    """[(сеанс, продано мест, выручка в звёздах)] для будущих сеансов."""
    sold = func.count(ShowSeat.id).filter(ShowSeat.status == SeatStatus.SOLD)
    revenue = func.coalesce(func.sum(ShowSeat.price).filter(ShowSeat.status == SeatStatus.SOLD), 0)
    result = await session.execute(
        select(Show, sold, revenue).join(ShowSeat, ShowSeat.show_id == Show.id)
        .where(Show.ends_at > now).group_by(Show.id).order_by(Show.starts_at)
    )
    return [(show, int(count), int(total)) for show, count, total in result.all()]
