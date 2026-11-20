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
    "departureLocal": f"{DAY}T11:50:00",
    "arrivalLocal": f"{DAY}T13:55:00",
    "departureIATA": "ALA",
    "arrivalIATA": "NQZ",
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
            "performance": SourceBlock(
                "ForeFlight", status="ready", details={"eet_minutes": 101}
            ),
            "route": SourceBlock(
                "ForeFlight",
                status="ready",
                details={"route": "DCT TESTA", "alternate": "UAAA"},
            ),
            "overflight": SourceBlock(
                "ForeFlight",
                status="ready",
                details={"overflownCountries": [{"isoCode": "KZ"}]},
            ),
        }
    )
    handler = AsyncMock(
        return_value=SourceBlock(
            "Leon",
            details={
                "departure": {
                    "station": "UAAA",
                    "name": "Demo Handler",
                    "phone": "+10000000000",
                    "email": "handler@example.test",
                }
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
    observed = datetime.now(UTC).isoformat()
    leon_weather = AsyncMock(
        return_value=[
            {
                "flightNid": LEG["flightNid"],
                "airportAdep": {"code": {"icao": "UAAA"}},
                "metarAdep": {
                    "rawText": "UAAA 200600Z CAVOK",
                    "observationTime": observed,
                },
            }
        ]
    )
    weather = AsyncMock(
        side_effect=lambda airports, product: {
            "airports": airports,
            "products": [
                {
                    "product": product,
                    "data": [
                        {
                            "icaoId": "UAAA",
                            "rawOb": "UAAA 200600Z CAVOK",
                            "obsTime": observed,
                        }
                    ]
                    if product == "metar"
                    else [{"icaoId": "UACC", "rawTAF": "UACC 200500Z 2006/2106 CAVOK"}],
                }
            ],
        }
    )
    ff_leg = {
        "flightId": "ff-leg",
        "callSign": LEG["flightNumber"],
        "departure": LEG["departure"],
        "destination": LEG["arrival"],
        "aircraftRegistration": LEG["aircraft"],
    }
    ff_days = AsyncMock(
        return_value=[(ff_leg, datetime.fromisoformat(LEG["departureUTC"]))]
    )
    foreflight = AsyncMock()
    monkeypatch.setattr(flight_views, "ForeFlightClient", lambda: foreflight)
    monkeypatch.setattr(flight_views, "flights_for_day", ff_days)
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
    monkeypatch.setattr(flight_views, "get_flight_weather_leon", leon_weather)
    monkeypatch.setattr(flight_views, "get_weather", weather)
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


def test_schedule_card_and_lazy_preparation_use_exact_leg(sources, monkeypatch):
    monkeypatch.setattr(settings, "snapshot_schedule_ttl", 0)
    schedule, plan, crew, weather, briefing = sources
    headers = signed_headers()
    query = {"day": DAY}
    path = f"{BASE}/flights/121"
    with TestClient(create_app()) as client:
        result = client.get(
            f"{BASE}/schedule",
            params={**query, "end_day": "2026-11-26"},
            headers=headers,
        )
        assert [leg["id"] for leg in result.json()["flights"]] == [121, 122]
        query.update(week=DAY, version=result.json()["flights"][0]["version"])
        assert (
            client.get(f"{BASE}/flights/123", params=query, headers=headers).status_code
            == 404
        )
        plan.assert_not_called()

        assert (
            client.get(path, params=query, headers=headers).json()["flight"][
                "departure"
            ]
            == "UAAA"
        )
        assert (
            client.get(f"{path}/plan", params=query, headers=headers).json()[
                "ete_minutes"
            ]
            == 101
        )
        assert (
            client.get(f"{path}/handling", params=query, headers=headers).json()[
                "handlers"
            ][0]["name"]
            == "Demo Handler"
        )
        assert plan.await_count == 1
        assert plan.await_args.args == (LEG,)
        crew.assert_not_called()
        weather.assert_not_called()
        briefing.assert_not_called()

        for extra in ({"assignments_only": "true"}, {}):
            team = client.get(
                f"{path}/preparation/crew", params={**query, **extra}, headers=headers
            )
        assert team.json()["members"][0]["duty"]["metrics"][0]["actual"] == "06:20"
        assert "private@example.test" not in team.text
        crew.assert_awaited_once_with(121)
        briefing.assert_not_called()

        initial = client.get(f"{path}/weather", params=query, headers=headers).json()
        assert initial["reports"][0]["facts"]["VIS"] == "≥10 km (CAVOK)"
        assert weather.await_count == 1
        wx = client.get(
            f"{path}/preparation/weather", params=query, headers=headers
        ).json()
        assert wx["partial"]
        assert wx["reports"][0]["sources"] == ["Leon", "AWC"]
        assert [call.kwargs["product"] for call in weather.await_args_list] == [
            "metar",
            "taf",
        ]
        briefing.assert_not_called()

        notices = client.get(
            f"{path}/preparation/notams", params=query, headers=headers
        ).json()
        assert notices["recognized"] == ["UAAA"]
        assert notices["notams"][0]["status"] == "affects"
        assert notices["briefing_url"].startswith("https://")
        assert notices["departure_offset_minutes"] == 15
        assert briefing.await_count == 1
        assert briefing.await_args.kwargs["briefing_only"]
        assert flight_views.flights_for_day.await_count == 1
        assert flight_views.get_flight_weather_leon.await_count == 1
        schedule.assert_awaited_once_with(DAY, "2026-11-26")


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


def test_selection_rejects_changed_version_without_reloading_schedule(sources):
    schedule, plan, *_ = sources
    with TestClient(create_app()) as client:
        headers = signed_headers()
        week = {"day": DAY, "end_day": "2026-11-26"}
        initial = client.get(f"{BASE}/schedule", params=week, headers=headers)
        version = initial.json()["flights"][0]["version"]
        response = client.get(
            f"{BASE}/flights/121/plan",
            params={"day": DAY, "week": DAY, "version": "f" * 24},
            headers=headers,
        )
        assert version != "f" * 24
        assert response.status_code == 409
        plan.assert_not_called()
        schedule.assert_awaited_once()


@pytest.mark.asyncio
async def test_bot_menu_is_opt_in():
    bot = AsyncMock()
    await configure_mini_app(bot, "")
    bot.set_chat_menu_button.assert_not_called()
    await configure_mini_app(bot, "https://ops.example.test/")
    menu = bot.set_chat_menu_button.call_args.kwargs["menu_button"]
    assert menu.text == "OPS"
    assert menu.web_app.url == "https://ops.example.test/"
