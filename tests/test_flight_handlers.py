"""Flight-specific ground handler contacts must never be guessed from airport favourites."""

from unittest.mock import AsyncMock

import pytest

from services import flight_handlers

FLIGHT = {
    "flightNid": 101,
    "flightNumber": "ABC101",
    "departure": "UAAA",
    "arrival": "UACC",
    "departureUTC": "2026-11-20T12:00:00Z",
    "arrivalUTC": "2026-11-20T14:00:00Z",
}


@pytest.mark.asyncio
async def test_flight_assigned_handling_matches_directory_id_only(monkeypatch):
    api = AsyncMock(
        side_effect=[
            [
                {
                    "flightNid": 101,
                    "handlingAdep": {
                        "handler": {"handlerNid": 10, "name": "ABC Handling"}
                    },
                    "handlingAdes": {
                        "handler": {"handlerNid": 20, "name": "Arrival Service"}
                    },
                }
            ],
            [
                {
                    "handlerNid": 99,
                    "name": "Favourite Agent",
                    "isFavorite": True,
                    "email": "wrong@example.test",
                },
                {
                    "handlerNid": 10,
                    "name": "ABC Handling",
                    "telephone": "+111",
                    "email": "ops@example.test",
                },
            ],
            [
                {
                    "handlerNid": 20,
                    "name": "Arrival Service",
                    "telephone": "+222",
                    "email": "arr@example.test",
                },
            ],
        ]
    )
    monkeypatch.setattr(flight_handlers, "call_leon", api)
    block = await flight_handlers.load_flight_handlers(FLIGHT)
    text = flight_handlers.format_handlers(block, FLIGHT)
    assert "ABC Handling" in text and "+111" in text and "ops@example.test" in text
    assert "Arrival Service" in text and "+222" in text and "arr@example.test" in text
    assert "Favourite Agent" not in text and "wrong@example.test" not in text


@pytest.mark.asyncio
async def test_airport_directory_never_becomes_assigned_agent(monkeypatch):
    api = AsyncMock(return_value=[{"flightNid": 101}])
    monkeypatch.setattr(flight_handlers, "call_leon", api)
    block = await flight_handlers.load_flight_handlers(FLIGHT)
    assert "не назначен" in flight_handlers.format_handlers(block, FLIGHT)
    api.assert_awaited_once_with("get-flights-by-flight-nids", {"flightNids": [101]})


@pytest.mark.asyncio
async def test_duplicate_directory_name_does_not_leak_contacts(monkeypatch):
    api = AsyncMock(
        side_effect=[
            [{"flightNid": 101, "handlingAdep": {"name": "Same Agent"}}],
            [
                {"name": "Same Agent", "telephone": "+111"},
                {"name": "Same Agent", "telephone": "+222"},
            ],
        ]
    )
    monkeypatch.setattr(flight_handlers, "call_leon", api)
    block = await flight_handlers.load_flight_handlers(FLIGHT)
    text = flight_handlers.format_handlers(block, FLIGHT)
    assert "Same Agent" in text and "+111" not in text and "+222" not in text
