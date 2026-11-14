import json
from pathlib import Path
from unittest.mock import AsyncMock

import jsonschema
import pytest

from config.access import current_user_id
from config.settings import settings
from integrations.leon import operations
from services import operational_routes as routes
from services.operational_format import (
    format_awc_weather,
    format_crew_operations,
    format_watch,
)


@pytest.fixture
def access(monkeypatch):
    monkeypatch.setattr(settings, "telegram_allowed_user_ids", "42")
    token = current_user_id.set(42)
    yield
    current_user_id.reset(token)


@pytest.fixture
def flight():
    return {
        "flightNid": 123,
        "flightNumber": "ABC1265",
        "departure": "UTTT",
        "arrival": "UAAA",
        "departureUTC": "2026-11-11T12:00:00Z",
        "arrivalUTC": None,
    }


@pytest.mark.asyncio
async def test_flight_watch_resolves_real_nid(access, flight, monkeypatch):
    monkeypatch.setattr(
        operations,
        "get_flight_schedule",
        AsyncMock(return_value={"complete": True, "flights": [flight]}),
    )
    call = AsyncMock(
        return_value=[
            {"flightNid": 123, "flightWatch": {"toIso": "2026-11-11T12:10:00Z"}}
        ]
    )
    monkeypatch.setattr(operations, "call_leon", call)
    result = await operations.get_flight_watch("abc 1265", "2026-11-11")
    call.assert_awaited_once_with(
        "get-flight-watch-by-flight-nids", {"flightNids": [123]}
    )
    assert "12:10:00Z" in format_watch(result)
    assert "Leon MCP" in format_watch(result)


@pytest.mark.parametrize("complete,count", [(False, 1), (True, 0), (True, 2)])
@pytest.mark.asyncio
async def test_no_guessed_flight(access, flight, monkeypatch, complete, count):
    monkeypatch.setattr(
        operations,
        "get_flight_schedule",
        AsyncMock(return_value={"complete": complete, "flights": [flight] * count}),
    )
    call = AsyncMock()
    monkeypatch.setattr(operations, "call_leon", call)
    with pytest.raises(ValueError):
        await operations.get_flight_watch("ABC1265", "2026-11-11")
    call.assert_not_awaited()


@pytest.mark.parametrize(
    "mode,tool",
    [
        ("roster", "get-duty-list"),
        ("ftl", "get-crew-ftl-sheet"),
        ("violations", "get-crew-ftl-violations"),
    ],
)
@pytest.mark.asyncio
async def test_crew_arguments_match_live_schema(access, monkeypatch, mode, tool):
    monkeypatch.setattr(
        operations,
        "resolve_crew",
        AsyncMock(return_value={"loginNid": 456, "code": "JKO", "name": "Test"}),
    )
    call = AsyncMock(return_value={"calculations": []})
    monkeypatch.setattr(operations, "call_leon", call)
    await operations.get_crew_operations("JKO", "2026-11-01", "2026-11-30", mode)
    name, args = call.call_args.args
    assert name == tool
    schemas = json.loads(
        (Path(__file__).parent / "fixtures" / "leon_schemas.json").read_text(
            encoding="utf-8"
        )
    )
    jsonschema.validate(args, schemas[tool]["schema"])
    assert args["timeInterval"]["end"] == "2026-11-30T23:59:59Z"


@pytest.mark.asyncio
async def test_ambiguous_crew_not_selected(access, monkeypatch):
    call = AsyncMock(
        return_value=[
            {"login": {"loginNid": 1, "code": "AAA", "knownAs": "One"}},
            {"login": {"loginNid": 2, "code": "BBB", "knownAs": "Two"}},
        ]
    )
    monkeypatch.setattr(operations, "call_leon", call)
    with pytest.raises(ValueError, match="несколько"):
        await operations.resolve_crew("name")
    assert (await operations.resolve_crew("BBB"))["loginNid"] == 2


@pytest.mark.parametrize(
    "question",
    [
        "Flight Watch ABC1265 на 2026-11-11",
        "погода ABC1265 на 2026-11-11",
        "FTL JKO на 2026-11-11",
        "график пилота JKO на 2026-11-11",
    ],
)
@pytest.mark.asyncio
async def test_direct_routes_enforce_access(monkeypatch, question):
    weather = AsyncMock(return_value={"answer": "WX"})
    crew = AsyncMock(
        return_value={
            "crew": {"name": "Test", "code": "TEST"},
            "start": "2026-11-11",
            "end": "2026-11-11",
            "mode": "ftl",
            "data": [],
        }
    )
    monkeypatch.setattr(routes, "flight_weather", weather)
    monkeypatch.setattr(routes, "get_crew_operations", crew)
    token = current_user_id.set(None)
    try:
        answer = await routes.route_operation(question)
        if question.startswith("Flight Watch"):
            assert answer is None
        else:
            assert answer
            assert weather.await_count + crew.await_count == 1
    finally:
        current_user_id.reset(token)


@pytest.mark.asyncio
async def test_weather_keeps_successful_source(access, flight, monkeypatch):
    monkeypatch.setattr(routes, "resolve_flight", AsyncMock(return_value=flight))
    monkeypatch.setattr(
        routes, "get_flight_weather_leon", AsyncMock(side_effect=RuntimeError())
    )
    monkeypatch.setattr(
        routes,
        "get_weather",
        AsyncMock(
            return_value={
                "source": "AWC",
                "retrieved_at": "now",
                "airports": ["UTTT"],
                "products": [
                    {
                        "product": "metar",
                        "url": "https://aviationweather.gov/api/data/metar",
                        "data": [
                            {
                                "icaoId": "UTTT",
                                "rawOb": "UTTT 111200Z 00000KT CAVOK 15/05 Q1015",
                                "obsTime": 0,
                            }
                        ],
                    }
                ],
            }
        ),
    )
    answer = (await routes.flight_weather("ABC1265", "2026-11-11"))["answer"]
    assert "Leon MCP: ошибка" in answer
    assert "UTTT 111200Z 00000KT CAVOK 15/05 Q1015" in answer
    assert "Кратко" in answer
    assert "не архив" in answer


@pytest.mark.parametrize(
    "text,expected",
    [
        ("график «Иванов» с 16 по 30 ноября 2026", "Иванов"),
        ("FTL код JKO на 2026-11-11", "JKO"),
        ("график пилота JKO на 2026-11-11", "JKO"),
        ("нарушения FTL JKO с 16 по 30 ноября", "JKO"),
    ],
)
def test_crew_query(text, expected):
    assert routes._crew_query(text) == expected


def test_ftl_never_turns_unknown_offence_into_clearance():
    text = format_crew_operations(
        {
            "crew": {"name": "Test", "code": "JKO"},
            "start": "2026-11-11",
            "end": "2026-11-11",
            "mode": "ftl",
            "data": {
                "calculations": [
                    {
                        "date": "2026-11-11",
                        "ftlCalculations": [
                            {
                                "fdpLength": {
                                    "actual": 500,
                                    "limit": 480,
                                    "offence": True,
                                    "limitType": "MAX",
                                }
                            }
                        ],
                    }
                ]
            },
        }
    )
    assert "нарушение: ДА" in text
    assert "не решение" in text


def test_awc_no_report_is_explicit():
    text = format_awc_weather(
        {
            "source": "AWC",
            "retrieved_at": "now",
            "airports": ["UTTT"],
            "products": [
                {
                    "product": "metar",
                    "url": "https://aviationweather.gov/api/data/metar",
                    "data": [],
                }
            ],
        }
    )
    assert "UTTT" in text and "не предоставлена" in text


@pytest.mark.asyncio
async def test_live_crew_shape_uses_profile_login_not_crew_member_nid(
    access, monkeypatch
):
    call = AsyncMock(
        side_effect=[
            [
                {
                    "crewMemberNid": 999,
                    "login": "pilot-login",
                    "contact": {
                        "contactNid": 222,
                        "personCode": "JKO",
                        "knownAs": "Test",
                    },
                }
            ],
            [{"contactNid": 222, "login": {"loginNid": 456}}],
        ]
    )
    monkeypatch.setattr(operations, "call_leon", call)
    result = await operations.resolve_crew("JKO")
    assert result == {"loginNid": 456, "code": "JKO", "name": "Test"}
    assert call.await_args_list[1].args == (
        "get-contact-profiles-by-nid-list",
        {"contactsNids": [222]},
    )


@pytest.mark.parametrize(
    "mode,key",
    [
        ("ftl", "ftlCalculationsForCrewMember"),
        ("violations", "ftlViolationsForCrewMember"),
    ],
)
def test_live_ftl_envelope(mode, key):
    text = format_crew_operations(
        {
            "crew": {"name": "Test", "code": "JKO"},
            "start": "2026-11-11",
            "end": "2026-11-11",
            "mode": mode,
            "data": {
                "ftl": {
                    key: {
                        "calculations": [
                            {
                                "date": "2026-11-11",
                                "cumulativeLimits": {
                                    "cumulativeDuty": [
                                        {
                                            "label": "Duty",
                                            "actual": 500,
                                            "limit": 480,
                                            "limitType": "MAX",
                                            "offence": True,
                                        }
                                    ]
                                },
                            }
                        ]
                    }
                }
            },
        }
    )
    assert "Duty: факт 500 мин" in text
    assert "нарушение: ДА" in text


@pytest.mark.parametrize(
    "text,mode",
    [
        ("график «Иванов» с 16 по 30 ноября 2026", "roster"),
        ("FTL «Иванов» с 16 по 30 ноября 2026", "ftl"),
        ("нарушения FTL «Иванов» с 16 по 30 ноября 2026", "violations"),
    ],
)
@pytest.mark.asyncio
async def test_direct_crew_route(access, monkeypatch, text, mode):
    call = AsyncMock(
        return_value={
            "crew": {"name": "Test", "code": "JKO"},
            "start": "2026-11-16",
            "end": "2026-11-30",
            "mode": mode,
            "data": [] if mode == "roster" else {"calculations": []},
        }
    )
    monkeypatch.setattr(routes, "get_crew_operations", call)
    answer = await routes.route_operation(text)
    call.assert_awaited_once_with("Иванов", "2026-11-16", "2026-11-30", mode)
    assert "Источник: Leon MCP" in answer


@pytest.mark.asyncio
async def test_direct_watch_route(access, flight, monkeypatch):
    call = AsyncMock(return_value={"flight": flight, "data": []})
    monkeypatch.setattr(operations, "get_flight_watch", call)
    answer = await routes.route_operation("Flight Watch ABC1265 на 2026-11-11")
    call.assert_not_awaited()
    assert answer is None


@pytest.mark.asyncio
async def test_missing_flight_date_does_not_query_leon(access, monkeypatch):
    call = AsyncMock()
    monkeypatch.setattr(operations, "get_flight_watch", call)
    assert await routes.route_operation("Flight Watch ABC1265") is None
    call.assert_not_awaited()


def test_live_roster_fields_include_duty_definition_and_hide_identity():
    text = format_crew_operations(
        {
            "crew": {"name": "Test", "code": "JKO"},
            "start": "2026-11-01",
            "end": "2026-11-30",
            "mode": "roster",
            "data": [
                {
                    "startTimeIso": "2026-11-11T08:00:00Z",
                    "endTimeIso": "2026-11-11T16:00:00Z",
                    "login": {"loginNid": 987654321},
                    "definition": {
                        "name": "Standby",
                        "legend": "SBY",
                        "cdrType": "duty",
                    },
                }
            ],
        }
    )
    assert "Standby" in text
    assert "08:00:00Z" in text
    assert "987654321" not in text
