"""Зал и афиша из content/venue.json.

Схема зала записана строками: одна строка — один ряд (первый — ближе к сцене), буква — ценовая
зона места, «_» — проход. Например «__AAAAAA_AAAAAA__» — ряд из 12 мест партера с проходом.
Места нумеруются слева направо без учёта проходов, ключ места — «r<ряд>-s<место>».
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, model_validator

from bot.core.clock import to_utc

VENUE_FILE = Path(__file__).resolve().parent.parent / "content" / "venue.json"
GAP = "_"


class Zone(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    price: int = Field(gt=0, le=100_000)  # Telegram Stars


class Hall(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    zones: dict[str, Zone]
    rows: list[str] = Field(min_length=1, max_length=60)

    @model_validator(mode="after")
    def _check(self) -> Hall:
        if any(len(code) != 1 or not code.isalpha() or not code.isupper() for code in self.zones):
            raise ValueError("Код зоны — одна заглавная латинская буква")
        for number, row in enumerate(self.rows, start=1):
            if len(row) > 60 or not row.strip(GAP):
                raise ValueError(f"Ряд {number}: пустой или длиннее 60 символов")
            unknown = {char for char in row if char != GAP and char not in self.zones}
            if unknown:
                raise ValueError(f"Ряд {number}: неизвестные зоны {sorted(unknown)}")
        return self


class Event(BaseModel):
    code: str = Field(pattern=r"^[a-z0-9_]{1,32}$")
    title: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=500)
    hall: str
    duration_min: int = Field(default=120, gt=0, le=600)
    sessions: list[datetime] = Field(min_length=1)  # местное время зала


class Venue(BaseModel):
    venue: str = Field(min_length=1, max_length=100)
    halls: dict[str, Hall]
    events: list[Event]

    @model_validator(mode="after")
    def _check(self) -> Venue:
        codes = [event.code for event in self.events]
        if len(codes) != len(set(codes)):
            raise ValueError("Коды событий повторяются")
        for event in self.events:
            if event.hall not in self.halls:
                raise ValueError(f"Событие {event.code}: нет зала {event.hall}")
        return self


@dataclass(frozen=True)
class SeatSpec:
    key: str
    row: int
    number: int
    zone: str
    price: int


def seats_of(hall: Hall) -> list[SeatSpec]:
    seats: list[SeatSpec] = []
    for row_index, row in enumerate(hall.rows, start=1):
        number = 0
        for char in row:
            if char == GAP:
                continue
            number += 1
            seats.append(SeatSpec(f"r{row_index}-s{number}", row_index, number, char, hall.zones[char].price))
    return seats


def layout_of(hall: Hall) -> list[list[str | None]]:
    """Схема для отрисовки: ряды, в каждом — ключи мест и None на месте прохода."""
    layout: list[list[str | None]] = []
    for row_index, row in enumerate(hall.rows, start=1):
        number = 0
        cells: list[str | None] = []
        for char in row:
            if char == GAP:
                cells.append(None)
                continue
            number += 1
            cells.append(f"r{row_index}-s{number}")
        layout.append(cells)
    return layout


def load_venue(path: Path = VENUE_FILE) -> Venue:
    return Venue.model_validate(json.loads(path.read_text(encoding="utf-8")))


def session_start_utc(local: datetime, tz: ZoneInfo) -> datetime:
    return to_utc(local.replace(tzinfo=tz))
