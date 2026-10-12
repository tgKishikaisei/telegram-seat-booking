"""Схема зала, бронь (в том числе гонки), оплата, билеты и вход."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.exc import OperationalError

from bot.models import HoldStatus, SeatStatus
from bot.repositories import sales as sales_repo
from bot.repositories import shows as shows_repo
from bot.services import booking, payments, tickets
from bot.services.booking import BookingError, SeatsTaken
from bot.services.venue import Venue, layout_of, load_venue, seats_of

TZ = ZoneInfo("Asia/Tashkent")
SECRET = "t" * 40
NOW = datetime(2026, 10, 20, 10, 0)  # UTC, до всех сеансов из venue.json
VENUE = load_venue()


async def first_show(session):
    await booking.sync_venue(session, VENUE, TZ)
    return (await shows_repo.upcoming(session, NOW))[0]


def test_hall_layout():
    hall = VENUE.halls["main"]
    seats = seats_of(hall)
    assert len(seats) == sum(len(row.replace("_", "")) for row in hall.rows)
    assert seats[0].key == "r1-s1" and seats[0].zone == "A" and seats[0].price == 180
    first_row = layout_of(hall)[0]
    assert first_row[:2] == [None, None] and first_row[2] == "r1-s1" and None in first_row[3:]


@pytest.mark.parametrize("rows", [["AAXA"], ["____"], ["a"]])
def test_bad_layouts_rejected(rows):
    data = VENUE.model_dump(mode="json")
    data["halls"]["main"]["rows"] = rows
    with pytest.raises(ValueError):
        Venue.model_validate(data)


async def test_sync_is_idempotent(session):
    first = await booking.sync_venue(session, VENUE, TZ)
    second = await booking.sync_venue(session, VENUE, TZ)
    assert first == second == sum(len(event.sessions) for event in VENUE.events)
    show = (await shows_repo.upcoming(session, NOW))[0]
    assert show.starts_at == datetime(2026, 10, 24, 14, 30)  # 19:30 по Ташкенту
    assert len(await shows_repo.seats(session, show.id)) == len(seats_of(VENUE.halls["main"]))


async def test_hold_rules(session):
    show = await first_show(session)
    with pytest.raises(BookingError):
        await booking.hold_seats(session, show.id, 1, [], NOW, 10)
    with pytest.raises(BookingError):
        await booking.hold_seats(session, show.id, 1, [f"r1-s{i}" for i in range(1, 9)], NOW, 10)
    with pytest.raises(BookingError):
        await booking.hold_seats(session, show.id, 1, ["'; DROP TABLE--"], NOW, 10)
    with pytest.raises(BookingError, match="закрыта"):
        await booking.hold_seats(session, show.id, 1, ["r1-s1"], show.starts_at - timedelta(minutes=5), 10)

    hold = await booking.hold_seats(session, show.id, 1, ["r1-s1", "r1-s2"], NOW, 10)
    assert (hold.seats_count, hold.total) == (2, 360)
    replaced = await booking.hold_seats(session, show.id, 1, ["r5-s1"], NOW, 10)  # новая бронь заменяет старую
    assert (await sales_repo.get_hold(session, hold.id)).status is HoldStatus.RELEASED
    assert replaced.total == 120


async def test_seat_map_hides_who_holds(session):
    show = await first_show(session)
    await booking.hold_seats(session, show.id, 1, ["r1-s1"], NOW, 10)
    mine = await booking.seat_map(session, VENUE, show, 1, NOW)
    other = await booking.seat_map(session, VENUE, show, 2, NOW)
    state = {s["key"]: s["state"] for row in other["rows"] for s in row["seats"] if s}
    assert state["r1-s1"] == "taken" and other["hold"] is None
    assert mine["hold"]["seats"] == ["r1-s1"]
    later = await booking.seat_map(session, VENUE, show, 2, NOW + timedelta(minutes=11))
    assert {s["key"]: s["state"] for row in later["rows"] for s in row["seats"] if s}["r1-s1"] == "free"


async def test_same_seat_race(file_session_factory):
    """Три зрителя одновременно хватают одни и те же места: бронь получает ровно один, и целиком."""
    async with file_session_factory() as session:
        show = await first_show(session)
        await session.commit()

    async def attempt(user_id: int) -> bool:
        async with file_session_factory() as session:
            try:
                await booking.hold_seats(session, show.id, user_id, ["r3-s4", "r3-s5"], NOW, 10)
                await session.commit()
                return True
            except (SeatsTaken, OperationalError):  # SQLite может ответить «база занята» — это тоже отказ
                await session.rollback()
                return False

    assert sorted(await asyncio.gather(attempt(1), attempt(2), attempt(3))) == [False, False, True]
    async with file_session_factory() as session:
        held = [s for s in await shows_repo.seats(session, show.id) if s.status is SeatStatus.HELD]
        assert sorted(s.seat_key for s in held) == ["r3-s4", "r3-s5"]  # частичных броней нет


async def test_payment_flow_and_tickets(session):
    show = await first_show(session)
    hold = await booking.hold_seats(session, show.id, 7, ["r2-s3", "r2-s4"], NOW, 10)
    order = await payments.order_for_hold(session, hold.public_id, 7, NOW)
    assert order.amount == 360
    with pytest.raises(payments.PaymentError):
        await payments.order_for_hold(session, hold.public_id, 8, NOW)  # чужая бронь
    payload = payments.make_payload(order.id, SECRET)

    async def precheck(**changes):
        params = {"payload": payload, "user_id": 7, "amount": 360, "currency": "XTR", "secret": SECRET, "now": NOW}
        return await payments.validate_pre_checkout(session, **{**params, **changes})

    assert await precheck(amount=1) is not None
    assert await precheck(user_id=8) is not None
    assert await precheck(now=NOW + timedelta(minutes=11)) is not None  # бронь истекла
    assert await precheck() is None
    assert (await sales_repo.get_hold(session, hold.id)).expires_at == NOW + payments.PAYMENT_GRACE  # продлена

    outcome = await payments.confirm_payment(session, payload=payload, user_id=7, amount=360, currency="XTR",
                                             charge_id="ch1", secret=SECRET, now=NOW)
    assert len(outcome.tickets) == 2 and not outcome.needs_refund
    again = await payments.confirm_payment(session, payload=payload, user_id=7, amount=360, currency="XTR",
                                           charge_id="ch1", secret=SECRET, now=NOW)
    assert again.already_processed
    statuses = {s.seat_key: s.status for s in await shows_repo.seats(session, show.id)}
    assert statuses["r2-s3"] is SeatStatus.SOLD
    with pytest.raises(SeatsTaken):  # проданное место не забронировать
        await booking.hold_seats(session, show.id, 9, ["r2-s3"], NOW, 10)


async def test_payment_after_seats_lost_requests_refund(session):
    show = await first_show(session)
    hold = await booking.hold_seats(session, show.id, 7, ["r4-s1"], NOW, 10)
    order = await payments.order_for_hold(session, hold.public_id, 7, NOW)
    await sales_repo.expire_holds(session, NOW + timedelta(minutes=20))
    await booking.hold_seats(session, show.id, 8, ["r4-s1"], NOW + timedelta(minutes=20), 10)  # место ушло
    outcome = await payments.confirm_payment(session, payload=payments.make_payload(order.id, SECRET), user_id=7,
                                             amount=order.amount, currency="XTR", charge_id="late",
                                             secret=SECRET, now=NOW + timedelta(minutes=21))
    assert outcome.needs_refund


async def test_check_in(session):
    show = await first_show(session)
    hold = await booking.hold_seats(session, show.id, 7, ["r6-s6"], NOW, 10)
    order = await payments.order_for_hold(session, hold.public_id, 7, NOW)
    outcome = await payments.confirm_payment(session, payload=payments.make_payload(order.id, SECRET), user_id=7,
                                             amount=order.amount, currency="XTR", charge_id="c", secret=SECRET, now=NOW)
    code = outcome.tickets[0].code
    payload = tickets.qr_payload(code, SECRET)
    entry = show.starts_at - timedelta(minutes=30)

    assert not (await tickets.check_in(session, payload, 99, SECRET, NOW)).ok  # слишком рано
    assert not (await tickets.check_in(session, f"{code}.deadbeef0000", 99, SECRET, entry)).ok  # подделка
    first = await tickets.check_in(session, payload, 99, SECRET, entry)
    assert first.ok and "ряд 6, место 6" in first.message
    assert "уже использован" in (await tickets.check_in(session, payload, 99, SECRET, entry)).message
    assert tickets.verify_payload(code.lower(), SECRET) == code  # ручной ввод кода контролёром
    assert tickets.verify_payload("SHORT", SECRET) is None
    assert tickets.qr_png(payload).startswith(b"\x89PNG")


async def test_refund_frees_seats(session):
    show = await first_show(session)
    hold = await booking.hold_seats(session, show.id, 7, ["r7-s1"], NOW, 10)
    order = await payments.order_for_hold(session, hold.public_id, 7, NOW)
    await payments.confirm_payment(session, payload=payments.make_payload(order.id, SECRET), user_id=7,
                                   amount=order.amount, currency="XTR", charge_id="r", secret=SECRET, now=NOW)
    assert await sales_repo.refund_order(session, order.id)
    assert not await sales_repo.refund_order(session, order.id)
    seat = next(s for s in await shows_repo.seats(session, show.id) if s.seat_key == "r7-s1")
    assert seat.status is SeatStatus.FREE and await sales_repo.tickets_for_order(session, order.id) == []
