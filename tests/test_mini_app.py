import hmac
import json
from datetime import UTC, datetime
from time import time
from unittest.mock import AsyncMock
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from api.main import create_app
from application import flight_views
from bot.access import access_store
from bot.miniapp import configure_mini_app
from config.settings import settings
from services.snapshot_models import Notam, SourceBlock

TOKEN = "123456:mini-app-test-token"
DAY = "2026-11-20"
BASE = "/api/v1"
LEG = {
    "flightNid": 121,
    "flightNumber": "OPS201",
    "departure": "UAAA",
    "arrival": "UACC",
    "departureUTC": f"{DAY}T06:50:00Z",
    "arrivalUTC": f"{DAY}T08:55:00Z",
    "aircraft": "DEMO-01",
    "aircraftType": "E35L",
}


def init_data(user_id=42, *, age=0, **fields):
    data = {
        "auth_date": str(int(time()) - age),
        "user": json.dumps({"id": user_id}, separators=(",", ":")),
        "signature": "signed-launch-metadata",
        **fields,
    }
    check = "\n".join(f"{key}={value}" for key, value in sorted(data.items()))
    key = hmac.digest(b"WebAppData", TOKEN.encode(), "sha256")
    return urlencode({**data, "hash": hmac.digest(key, check.encode(), "sha256").hex()})


def signed_headers(user_id=42, **fields):
    return {"Authorization": "tma " + init_data(user_id, **fields)}


@pytest.fixture
def sources(monkeypatch):
    monkeypatch.setattr(settings, "telegram_bot_token", SecretStr(TOKEN))
    schedule = AsyncMock(
        return_value={
            "complete": True,
            "flights": [LEG, {**LEG, "flightNid": 122, "departure": "UTTT"}],
        }
    )
    plan = AsyncMock(
        return_value={
            "flighttime": SourceBlock(
                "ForeFlight", status="ready", details={"eet_minutes": 101}
            ),
            "route": SourceBlock(
                "ForeFlight",
                status="ready",
                details={"route": "DCT TESTA", "alternate": "UAAA"},
            ),
        }
    )
    handler = AsyncMock(
        return_value=SourceBlock(
            "Leon",
            details={
                "departure": {"station": "UAAA", "name": "Demo Handler", "phone": "+0"}
            },
        )
    )
    crew = AsyncMock(
        return_value={
            "crew": [
                {
                    "flightNid": 121,
                    "crewMemberList": [
                        {
                            "contact": {
                                "knownAs": "Test Captain",
                                "email": "private@example.test",
                            },
                            "position": {"name": "PIC"},
                        }
                    ],
                }
            ]
        }
    )
    duty = AsyncMock(
        return_value=SourceBlock(
            "Leon",
            details={
                "assignments": [
                    {
                        "metrics": [
                            {
                                "name": "FDP",
                                "actual": "06:20",
                                "limit": "09:00",
                                "offence": False,
                            }
                        ],
                    }
                ]
            },
        )
    )
    weather = AsyncMock(
        return_value={
            "reports": [
                {
                    "station": "UAAA",
                    "product": "METAR",
                    "raw": "UAAA 200600Z CAVOK",
                    "source": "Leon MCP",
                },
                {
                    "station": "UAAA",
                    "product": "METAR",
                    "raw": "UAAA 200600Z CAVOK",
                    "source": "AWC",
                },
                {
                    "station": "UACC",
                    "product": "TAF",
                    "raw": "UACC 200500Z 2006/2106 CAVOK",
                    "source": "AWC",
                },
            ]
        }
    )
    briefing = AsyncMock(
        return_value={
            "briefing": SourceBlock(
                "ForeFlight",
                details={
                    "url": "https://example.test/briefing.pdf?signature=test",
                    "generated": f"{DAY}T06:02:00Z",
                    "departure_offset_minutes": 15,
                },
            ),
            "notams": SourceBlock(
                "ForeFlight",
                details={
                    "recognized": {"UAAA"},
                    "notams": {
                        "UAAA": [
                            Notam(
                                "A0123/26",
                                "UAAA",
                                "N",
                                None,
                                "A0123/26 NOTAMN",
                                datetime(2026, 11, 20, 6, tzinfo=UTC),
                                datetime(2026, 11, 20, 9, tzinfo=UTC),
                            )
                        ]
                    },
                },
            ),
        }
    )
    monkeypatch.setattr(flight_views.operations, "flight_schedule", schedule)
    monkeypatch.setattr(flight_views, "load_fast_foreflight", plan)
    monkeypatch.setattr(flight_views, "load_flight_handlers", handler)
    monkeypatch.setattr(flight_views, "get_flight_crew", crew)
    monkeypatch.setattr(flight_views, "load_duty", duty)
    monkeypatch.setattr(flight_views.operations, "selected_weather", weather)
    monkeypatch.setattr(flight_views, "load_foreflight", briefing)
    return schedule, plan, crew, weather, briefing


def test_telegram_access_rejects_unverified_expired_group_and_revoked_launches(sources):
    schedule, *_ = sources
    path = f"{BASE}/schedule?day={DAY}"
    invalid = signed_headers()
    invalid["Authorization"] += "&auth_date=0"
    tampered = signed_headers()
    tampered["Authorization"] = tampered["Authorization"].replace("%3A42", "%3A43")
    with TestClient(create_app()) as client:
        for headers in (
            {},
            invalid,
            tampered,
            signed_headers(age=3601),
            signed_headers(age=-60),
        ):
            response = client.get(path, headers=headers)
            assert response.status_code == 401
            assert response.headers["Cache-Control"] == "no-store"
        assert (
            client.get(path, headers=signed_headers(chat_type="supergroup")).status_code
            == 403
        )
        assert client.get(path, headers=signed_headers(user_id=44)).status_code == 403
        schedule.assert_not_called()

        assert client.get(path, headers=signed_headers()).status_code == 200
        assert access_store.revoke_from_desktop(42)
        assert client.get(path, headers=signed_headers()).status_code == 403
        schedule.assert_awaited_once()


def test_schedule_card_and_lazy_preparation_use_exact_leg(sources):
    schedule, plan, crew, weather, briefing = sources
    headers = signed_headers()
    query = {"day": DAY}
    path = f"{BASE}/flights/121"
    with TestClient(create_app()) as client:
        result = client.get(f"{BASE}/schedule", params=query, headers=headers)
        assert [leg["id"] for leg in result.json()["flights"]] == [121, 122]
        assert (
            client.get(f"{BASE}/flights/123", params=query, headers=headers).status_code
            == 404
        )
        plan.assert_not_called()

        card = client.get(path, params=query, headers=headers).json()
        assert card["flight"]["departure"] == "UAAA"
        assert card["ete_minutes"] == 101
        assert card["handling"][0]["name"] == "Demo Handler"
        plan.assert_awaited_once_with(LEG)
        crew.assert_not_called()
        weather.assert_not_called()
        briefing.assert_not_called()

        team = client.get(f"{path}/preparation/crew", params=query, headers=headers)
        assert team.json()["members"][0]["duty"]["metrics"][0]["actual"] == "06:20"
        assert "private@example.test" not in team.text
        crew.assert_awaited_once_with(121)
        briefing.assert_not_called()

        wx = client.get(
            f"{path}/preparation/weather", params=query, headers=headers
        ).json()
        assert wx["partial"]
        assert wx["reports"][0]["sources"] == ["Leon", "AWC"]
        weather.assert_awaited_once_with(LEG, DAY, include_taf=True)
        briefing.assert_not_called()

        notices = client.get(
            f"{path}/preparation/notams", params=query, headers=headers
        ).json()
        assert notices["recognized"] == ["UAAA"]
        assert notices["notams"][0]["status"] == "affects"
        assert notices["briefing_url"].startswith("https://")
        assert notices["departure_offset_minutes"] == 15
        briefing.assert_awaited_once_with(LEG)
        schedule.assert_awaited_once_with(DAY)


def test_source_failures_keep_schedule_and_card_distinct(sources):
    schedule, plan, *_ = sources
    plan.side_effect = RuntimeError("sensitive upstream detail")
    with TestClient(create_app()) as client:
        card = client.get(
            f"{BASE}/flights/121", params={"day": DAY}, headers=signed_headers()
        )
        assert card.status_code == 200
        assert card.json()["plan_status"] == "unavailable"
        assert "sensitive" not in card.text
        schedule.side_effect = TimeoutError("upstream")
        response = client.get(
            f"{BASE}/schedule", params={"day": "2026-11-21"}, headers=signed_headers()
        )
        assert response.status_code == 502
        assert response.json()["detail"] == "Leon schedule unavailable. Try again."


@pytest.mark.asyncio
async def test_bot_menu_is_opt_in():
    bot = AsyncMock()
    await configure_mini_app(bot, "")
    bot.set_chat_menu_button.assert_not_called()
    await configure_mini_app(bot, "https://ops.example.test/")
    menu = bot.set_chat_menu_button.call_args.kwargs["menu_button"]
    assert menu.text == "SKED"
    assert menu.web_app.url == "https://ops.example.test/"
