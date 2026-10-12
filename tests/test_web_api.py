"""API мини-приложения целиком: настоящий FastAPI, подпись initData Telegram, SQLite во временной папке."""

from __future__ import annotations

import json
import time
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient

from bot.config import Settings
from bot.core.telegram_webapp import sign_init_data
from bot.web.app import create_web_app

TOKEN = "123456:TEST-TOKEN"


def init_data(user_id: int) -> str:
    fields = {"auth_date": str(int(time.time())), "user": json.dumps({"id": user_id, "first_name": "Ann"})}
    fields["hash"] = sign_init_data(fields, TOKEN)
    return urlencode(fields)


def headers(user_id: int) -> dict[str, str]:
    return {"authorization": f"tma {init_data(user_id)}", "host": "seats.example"}


class FakeBot:
    def __init__(self) -> None:
        self.invoices: list[dict] = []

    async def create_invoice_link(self, **kwargs: object) -> str:
        self.invoices.append(kwargs)
        return "https://t.me/$invoice"

    @property
    def session(self):  # type: ignore[no-untyped-def]
        class _Session:
            async def close(self) -> None:
                return None
        return _Session()


@pytest.fixture
def client(tmp_path):
    settings = Settings(bot_token=TOKEN, ticket_secret="t" * 40, public_url="https://seats.example",
                        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'web.db').as_posix()}", staff_ids="5",
                        _env_file=None)
    app = create_web_app(settings)
    with TestClient(app, base_url="https://seats.example") as test_client:
        from bot.repositories import shows as shows_repo  # noqa: F401
        from bot.services.booking import sync_venue

        async def seed() -> None:
            async with app.state.session_factory() as session:
                await sync_venue(session, app.state.venue, settings.tz)
                await session.commit()

        test_client.portal.call(seed)
        app.state.bot = FakeBot()
        yield test_client


def test_requires_telegram_auth(client):
    assert client.get("/api/shows").status_code == 401
    forged = init_data(1).replace("Ann", "Bob")
    assert client.get("/api/shows", headers={"authorization": f"tma {forged}"}).status_code == 401


def test_full_purchase_flow(client):
    shows = client.get("/api/shows", headers=headers(1)).json()
    assert shows["venue"] and shows["shows"]
    show_id = shows["shows"][0]["id"]

    seat_map = client.get(f"/api/shows/{show_id}/seats", headers=headers(1)).json()
    assert seat_map["zones"]["A"]["price"] == 180 and seat_map["rows"][0]["seats"][0] is None

    hold = client.post(f"/api/shows/{show_id}/hold", json={"seats": ["r1-s1", "r1-s2"]}, headers=headers(1))
    assert hold.status_code == 200 and hold.json()["hold"]["total"] == 360
    hold_id = hold.json()["hold"]["id"]

    taken = client.post(f"/api/shows/{show_id}/hold", json={"seats": ["r1-s2"]}, headers=headers(2))
    assert taken.status_code == 409 and taken.json()["error"]["code"] == "seats_taken"

    other_view = client.get(f"/api/shows/{show_id}/seats", headers=headers(2)).json()
    states = {s["key"]: s["state"] for row in other_view["rows"] for s in row["seats"] if s}
    assert states["r1-s1"] == "taken"

    assert client.post(f"/api/holds/{hold_id}/invoice", headers=headers(2)).status_code == 409  # чужая бронь
    invoice = client.post(f"/api/holds/{hold_id}/invoice", headers=headers(1))
    assert invoice.json() == {"link": "https://t.me/$invoice"}
    sent = client.app.state.bot.invoices[0]
    assert sent["currency"] == "XTR" and sent["prices"][0].amount == 360

    assert client.delete(f"/api/holds/{hold_id}", headers=headers(2)).status_code == 404
    assert client.delete(f"/api/holds/{hold_id}", headers=headers(1)).json() == {"ok": True}


def test_validation_and_staff(client):
    show_id = client.get("/api/shows", headers=headers(1)).json()["shows"][0]["id"]
    extra = client.post(f"/api/shows/{show_id}/hold", json={"seats": ["r1-s1"], "price": 1}, headers=headers(1))
    assert extra.status_code == 422  # лишние поля запрещены
    too_many = client.post(f"/api/shows/{show_id}/hold", json={"seats": [f"r2-s{i}" for i in range(1, 9)]},
                           headers=headers(1))
    assert too_many.status_code == 422
    assert client.get("/api/shows/999999/seats", headers=headers(1)).status_code == 404

    me = client.get("/api/me", headers=headers(1)).json()
    assert me["is_staff"] is False and me["tickets"] == []
    assert client.post("/api/staff/check", json={"code": "ABCDEFGHJKLM"}, headers=headers(1)).status_code == 403
    check = client.post("/api/staff/check", json={"code": "ABCDEFGHJKLM"}, headers=headers(5))
    assert check.status_code == 200 and check.json()["ok"] is False


def test_static_app_and_headers(client):
    page = client.get("/app/", headers={"host": "seats.example"})
    assert page.status_code == 200 and "app.js" in page.text
    assert "frame-ancestors https://web.telegram.org" in page.headers["content-security-policy"]
    api = client.get("/api/shows", headers=headers(1))
    assert api.headers["cache-control"] == "no-store"
