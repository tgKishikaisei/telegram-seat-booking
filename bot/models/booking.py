from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Enum, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from bot.core.clock import utcnow
from bot.core.database import Base


def _enum(kind: type[enum.Enum]) -> Enum:
    return Enum(kind, native_enum=False, length=16, values_callable=lambda members: [m.value for m in members])


class SeatStatus(enum.StrEnum):
    FREE = "free"
    HELD = "held"  # временная бронь на время оплаты
    SOLD = "sold"


class HoldStatus(enum.StrEnum):
    ACTIVE = "active"
    PAID = "paid"
    EXPIRED = "expired"
    RELEASED = "released"


class OrderStatus(enum.StrEnum):
    PENDING = "pending"
    PAID = "paid"
    REFUNDED = "refunded"


class Show(Base):
    """Сеанс: событие из афиши в конкретное время. Создаётся из venue.json при запуске."""

    __tablename__ = "shows"
    __table_args__ = (UniqueConstraint("event_code", "starts_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    event_code: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(String(100))
    description: Mapped[str] = mapped_column(String(500), default="")
    hall_code: Mapped[str] = mapped_column(String(32))
    starts_at: Mapped[datetime] = mapped_column(DateTime, index=True)  # UTC
    ends_at: Mapped[datetime] = mapped_column(DateTime)
    active: Mapped[bool] = mapped_column(default=True)


class ShowSeat(Base):
    """Место на конкретном сеансе. Статус меняется только атомарным UPDATE … WHERE status = …,
    поэтому одно место нельзя продать дважды, даже если двое нажмут «оплатить» одновременно."""

    __tablename__ = "show_seats"
    __table_args__ = (
        UniqueConstraint("show_id", "seat_key"),
        Index("ix_show_seats_status_held_until", "status", "held_until"),
        CheckConstraint("price > 0", name="price_positive"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    show_id: Mapped[int] = mapped_column(ForeignKey("shows.id", ondelete="CASCADE"), index=True)
    seat_key: Mapped[str] = mapped_column(String(16))
    row: Mapped[int]
    number: Mapped[int]
    zone: Mapped[str] = mapped_column(String(1))
    price: Mapped[int]
    status: Mapped[SeatStatus] = mapped_column(_enum(SeatStatus), default=SeatStatus.FREE)
    hold_id: Mapped[int | None] = mapped_column(ForeignKey("holds.id", ondelete="SET NULL"), index=True)
    held_until: Mapped[datetime | None] = mapped_column(DateTime)


class Hold(Base):
    """Временная бронь: места держатся за зрителем HOLD_MINUTES, пока он платит.
    public_id — случайный идентификатор для API (номера по порядку не раскрываем)."""

    __tablename__ = "holds"
    __table_args__ = (Index("ix_holds_status_expires", "status", "expires_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    public_id: Mapped[str] = mapped_column(String(32), unique=True)
    show_id: Mapped[int] = mapped_column(ForeignKey("shows.id", ondelete="CASCADE"))
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    seats_count: Mapped[int]
    total: Mapped[int]
    status: Mapped[HoldStatus] = mapped_column(_enum(HoldStatus), default=HoldStatus.ACTIVE)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (CheckConstraint("amount > 0", name="amount_positive"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    hold_id: Mapped[int] = mapped_column(ForeignKey("holds.id", ondelete="RESTRICT"), unique=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    amount: Mapped[int]
    status: Mapped[OrderStatus] = mapped_column(_enum(OrderStatus), default=OrderStatus.PENDING)
    telegram_charge_id: Mapped[str | None] = mapped_column(String(255), unique=True)
    reminded: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime)


class Ticket(Base):
    """Билет на одно место. code — случайный; в QR-коде он подписан HMAC (см. services/tickets.py)."""

    __tablename__ = "tickets"
    __table_args__ = (UniqueConstraint("show_id", "seat_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(16), unique=True)
    show_id: Mapped[int] = mapped_column(ForeignKey("shows.id", ondelete="CASCADE"))
    seat_key: Mapped[str] = mapped_column(String(16))
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    issued_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    used_at: Mapped[datetime | None] = mapped_column(DateTime)
    used_by: Mapped[int | None] = mapped_column(BigInteger)
