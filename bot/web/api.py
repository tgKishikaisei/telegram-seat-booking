"""API мини-приложения. Каждый запрос подписан initData Telegram (заголовок Authorization: tma …):
кто делает запрос, сервер узнаёт из проверенной подписи, а не из тела запроса."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from aiogram.exceptions import TelegramAPIError
from aiogram.types import LabeledPrice
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import Settings
from bot.core.clock import utcnow
from bot.core.ratelimit import RateLimiter
from bot.core.telegram_webapp import WebAppAuth
from bot.core.web import AppError
from bot.repositories import sales as sales_repo
from bot.repositories import shows as shows_repo
from bot.services import booking, payments, tickets
from bot.services.booking import BookingError, SeatsTaken
from bot.services.payments import PaymentError

router = APIRouter(prefix="/api")


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def current_user(request: Request) -> WebAppAuth:
    return await request.app.state.auth(request)


async def rate_limited(request: Request, user: Annotated[WebAppAuth, Depends(current_user)]) -> WebAppAuth:
    limiter: RateLimiter = request.app.state.limiter
    if not await limiter.hit(f"api:{user.user.id}", 60, 60):
        raise AppError(429, "too_many_requests", "Слишком много запросов — подождите минуту")
    return user


Session = Annotated[AsyncSession, Depends(get_session)]
User = Annotated[WebAppAuth, Depends(rate_limited)]


class HoldRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    seats: list[Annotated[str, Field(max_length=16)]] = Field(min_length=1, max_length=booking.MAX_SEATS)


class CheckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(min_length=5, max_length=64)


def _settings(request: Request) -> Settings:
    return request.app.state.settings


@router.get("/shows")
async def list_shows(request: Request, session: Session, user: User) -> dict:
    settings, now = _settings(request), utcnow()
    upcoming = await shows_repo.upcoming(session, now)
    free = await shows_repo.free_counts(session, [show.id for show in upcoming], now)
    return {"venue": request.app.state.venue.venue,
            "shows": [booking.show_view(show, free.get(show.id, 0), settings.tz) for show in upcoming]}


@router.get("/shows/{show_id}/seats")
async def show_seats(show_id: int, request: Request, session: Session, user: User) -> dict:
    show = await shows_repo.get(session, show_id)
    if show is None or not show.active:
        raise AppError(404, "not_found", "Сеанс не найден")
    return await booking.seat_map(session, request.app.state.venue, show, user.user.id, utcnow())


@router.post("/shows/{show_id}/hold")
async def hold(show_id: int, body: HoldRequest, request: Request, session: Session, user: User) -> dict:
    limiter: RateLimiter = request.app.state.limiter
    if not await limiter.hit(f"hold:{user.user.id}", 15, 600):
        raise AppError(429, "too_many_holds", "Слишком много попыток брони — подождите несколько минут")
    try:
        created = await booking.hold_seats(session, show_id, user.user.id, body.seats, utcnow(),
                                           _settings(request).hold_minutes)
    except SeatsTaken as exc:
        await session.rollback()
        raise AppError(409, "seats_taken", str(exc)) from exc
    except BookingError as exc:
        raise AppError(400, "bad_request", str(exc)) from exc
    await session.commit()  # фиксируем бронь до ответа клиенту
    return {"hold": {"id": created.public_id, "total": created.total, "seats": body.seats,
                     "expires_at": created.expires_at.isoformat() + "Z"}}


@router.delete("/holds/{public_id}")
async def cancel(public_id: str, session: Session, user: User) -> dict:
    try:
        await booking.cancel_hold(session, public_id[:64], user.user.id)
    except BookingError as exc:
        raise AppError(404, "not_found", str(exc)) from exc
    await session.commit()
    return {"ok": True}


@router.post("/holds/{public_id}/invoice")
async def invoice(public_id: str, request: Request, session: Session, user: User) -> dict:
    settings = _settings(request)
    try:
        order = await payments.order_for_hold(session, public_id[:64], user.user.id, utcnow())
    except PaymentError as exc:
        raise AppError(409, "hold_expired", str(exc)) from exc
    hold_row = await sales_repo.get_hold(session, order.hold_id)
    show = await shows_repo.get(session, hold_row.show_id)
    seats = booking.seats_text(await sales_repo.hold_seat_keys(session, hold_row.id))
    await session.commit()  # заказ должен существовать до pre_checkout
    try:
        link = await request.app.state.bot.create_invoice_link(
            title=show.title[:32], description=f"{seats}"[:255],
            payload=payments.make_payload(order.id, settings.ticket_secret.get_secret_value()),
            currency=payments.STARS,
            prices=[LabeledPrice(label=f"Билеты: {hold_row.seats_count}", amount=order.amount)],
        )
    except TelegramAPIError as exc:
        raise AppError(502, "telegram_unavailable", "Telegram не создал счёт — попробуйте ещё раз") from exc
    return {"link": link}


@router.get("/me")
async def me(request: Request, session: Session, user: User) -> dict:
    settings = _settings(request)
    items = await sales_repo.tickets_for_user(session, user.user.id, utcnow())
    result = []
    for ticket, show in items:
        view = booking.show_view(show, 0, settings.tz)
        result.append({"title": show.title, "date": view["date"], "time": view["time"],
                       "seat": booking.seats_text([ticket.seat_key]), "used": ticket.used_at is not None})
    return {"name": user.user.first_name, "is_staff": settings.is_staff(user.user.id), "tickets": result}


@router.post("/staff/check")
async def staff_check(body: CheckRequest, request: Request, session: Session, user: User) -> dict:
    settings = _settings(request)
    if not settings.is_staff(user.user.id):
        raise AppError(403, "forbidden", "Проверять билеты могут только контролёры")
    result = await tickets.check_in(session, body.code, user.user.id, settings.ticket_secret.get_secret_value(),
                                    utcnow())
    await session.commit()
    return {"ok": result.ok, "message": result.message}
