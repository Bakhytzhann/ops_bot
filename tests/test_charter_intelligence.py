import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr

from services import charter_intelligence as charter
from services.charter_recommendations import review_charter
from services.source_cache import SourceCache


@pytest.mark.asyncio
async def test_rfq_review_uses_pricing_without_forwarding_personal_data(monkeypatch):
    rfq = {
        "nid": 12,
        "id": "RFQ-12",
        "buyerInfo": {"email": "buyer@example.test"},
        "legs": [{"adep": {"code": {"icao": "UAAA"}}, "ades": {"icao": "UACC"}}],
        "realizations": [
            {
                "acft": {"registration": "UP-ABC", "acftType": {"icao": "E35L"}},
                "price": 5000,
                "currency": "USD",
                "pricing": {"cost": 4000, "currency": "USD", "pricingMargin": 20},
                "subcharter": {"crew": "Private name"},
                "legs": [
                    {
                        "adep": "UAAA",
                        "ades": "UACC",
                        "passengerList": {"isFerry": True, "private": "Private name"},
                    }
                ],
            }
        ],
    }
    call = AsyncMock(return_value={"result": [rfq]})
    monkeypatch.setattr(charter, "call_leon", call)
    data = await charter.load_charter_request(12)

    call.assert_awaited_once_with(
        "get-quote-requests-by-nid-list", {"quoteRequestNids": [12]}
    )
    rendered = charter.render_charter_request(data)
    assert "5,000.00 USD" in rendered
    assert "4,000.00 USD" in rendered
    assert "UAAA → UACC (перегон)" in rendered
    assert data["history"][0]["aircraft_type"] == "E35L"
    evidence = json.dumps(charter.charter_request_sources(data))
    assert "Private name" not in evidence
    assert "buyer@example.test" not in evidence


@pytest.mark.asyncio
async def test_foreflight_plan_requires_same_trip_and_same_planned_leg(monkeypatch):
    start = "2026-11-18T10:00:00Z"
    rfq = charter._request(
        {
            "nid": 12,
            "trip": {"tripNid": "27"},
            "legs": [{"adep": "UAAA", "ades": "UACC", "startDateTime": start}],
        }
    )
    leg = {
        "tripNid": 27,
        "flightNumber": "KAZ001",
        "departure": "UAAA",
        "arrival": "UACC",
        "departureUTC": start,
        "aircraft": "UP-ABC",
    }
    monkeypatch.setattr(charter.settings, "foreflight_api_key", SecretStr("test"))
    monkeypatch.setattr(charter.settings, "foreflight_vendor_id", SecretStr("test"))
    schedule = {"complete": True, "flights": [{**leg, "tripNid": 28}]}
    monkeypatch.setattr(
        charter, "get_flight_schedule", AsyncMock(return_value=schedule)
    )
    row = {
        "flightId": "test",
        "departure": "UAAA",
        "destination": "UACC",
        "departureTime": start,
        "aircraftRegistration": "UPABC",
    }
    match = AsyncMock(return_value=(row, "test"))
    monkeypatch.setattr(charter, "find_flight", match)
    performance = AsyncMock(
        return_value={
            "performance": {
                "times": {"timeToDestinationMinutes": 98},
                "fuel": {"fuelToDestination": 800, "unit": "Pound"},
            }
        }
    )

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            pass

        get_performance = performance

    monkeypatch.setattr(charter, "ForeFlightClient", Client)
    assert await charter._booked_leg(rfq) is None
    match.assert_not_awaited()

    schedule["flights"] = [leg]
    matched = await charter._booked_leg(rfq)
    assert matched == leg
    row["departureTime"] = "2026-11-18T10:10:00Z"
    assert await charter._foreflight_plan(matched) is None
    performance.assert_not_awaited()

    row["departureTime"] = start
    plan = await charter._foreflight_plan(matched)
    assert plan["ete_minutes"] == 98
    assert plan["fuel_to_destination"] == 800
    assert plan["fuel_unit"] == "POUND"


@pytest.mark.asyncio
async def test_checklist_excludes_other_flights_notes_and_files(monkeypatch):
    call = AsyncMock(
        return_value=[
            {
                "flightNid": 15,
                "label": "Catering",
                "status": [{"caption": "Confirmed"}],
                "comment": "Private comment",
                "files": ["https://example.test/file"],
            },
            {"flightNid": 16, "label": "Other flight"},
        ]
    )
    monkeypatch.setattr(charter, "call_leon", call)
    tool = charter.CHECKLIST_TOOLS["FLIGHT CARE"]
    assert await charter.load_flight_checklist(tool, 15) == {
        "count": 1,
        "items": [{"item": "CATERING", "statuses": ["CONFIRMED"]}],
    }
    call.assert_awaited_once_with(tool, {"flightNidList": [15]})


@pytest.mark.asyncio
async def test_charter_replaces_limitation_only_model_answer_with_actions(monkeypatch):
    from services import ai_analysis

    monkeypatch.setattr(ai_analysis, "cache", SourceCache())
    monkeypatch.setattr(ai_analysis.agent, "_inference_slots", asyncio.Semaphore(1))
    chat = AsyncMock(
        return_value=SimpleNamespace(
            message=SimpleNamespace(
                content=json.dumps(
                    {
                        "summary": "Анализ данных",
                        "items": [],
                        "limitations": ["Отсутствует цена", "Отсутствует цена"],
                    }
                )
            )
        )
    )
    monkeypatch.setattr(ai_analysis.agent.client, "chat", chat)
    result = await review_charter(
        {
            "LEON_SALES": {"variants": [{"quote_price": None}]},
            "LEON_JOURNEY_LOG": [{"last_synced_at": "2026-11-19", "fuel": {}}],
        },
        ("rfq", 12),
    )

    assert result.count("[LEON_SALES]") == 1
    assert result.count("[LEON_JOURNEY_LOG]") == 1
    assert "Ограничение" not in result
    assert "Коммерция — внесите цену и валюту в Leon" in result
    assert "привяжите счёт поставщика к рейсу" in result
    assert "запас для скидки" in result
    assert "выросла цена поставщика" in result
    assert "recommendation_ids" in chat.call_args.kwargs["format"]["properties"]
    assert "tools" not in chat.call_args.kwargs


@pytest.mark.asyncio
async def test_charter_deduplicates_rankings_and_keeps_real_units(monkeypatch):
    from services import ai_analysis

    monkeypatch.setattr(ai_analysis, "cache", SourceCache())
    monkeypatch.setattr(ai_analysis.agent, "_inference_slots", asyncio.Semaphore(1))
    chat = AsyncMock(
        return_value=SimpleNamespace(
            message=SimpleNamespace(
                content='{"recommendation_ids":["fuel","fuel","block"]}'
            )
        )
    )
    monkeypatch.setattr(ai_analysis.agent.client, "chat", chat)
    result = await review_charter(
        {
            "LEON_SALES": {
                "variants": [{"quote_price": 1000, "quote_currency": "USD"}]
            },
            "LEON_JOURNEY_LOG": {
                "actual_count": 5,
                "filter": {
                    "departure": "UAAA",
                    "arrival": "UACC",
                    "aircraft_type": "E35L",
                },
                "block_minutes": {"median": 80, "count": 5},
                "block_delta_minutes": {"median": 15, "count": 4},
                "fuel": {"KG": {"median": 510, "count": 5}},
            },
        },
        ("rfq", 15),
    )

    assert result.index("Сверить затраты на топливо") < result.index(
        "Уточнить время в следующей смете"
    )
    assert result.count("Сверить затраты на топливо") == 1
    assert "510 KG · n=5 · UAAA → UACC · E35L" in result
    assert "отклонения от плана +15 мин · n=4" in result
    assert "Цена и минимальная маржа" in result
