from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services import flight_foreflight_fast


@pytest.mark.asyncio
async def test_fast_foreflight_uses_performance_and_overflight_without_pdf(monkeypatch):
    flight = {
        "flightNid": 1,
        "flightNumber": "ABC126",
        "departure": "UAAA",
        "arrival": "UACC",
        "departureUTC": "2026-11-20T12:00:00Z",
        "aircraft": "UP-ABC",
    }
    client = SimpleNamespace(
        get_flight=AsyncMock(return_value={
            "flightData": {
                "routeToDestination": {"route": "DCT TEST"},
                "alternate": "UAAA",
            }
        }),
        get_performance=AsyncMock(return_value={
            "times": {"timeToDestinationMinutes": 80},
        }),
        get_overflight=AsyncMock(return_value={
            "overflownCountries": [{"name": "Kazakhstan"}],
        }),
        get_document=AsyncMock(),
    )

    class Client:
        async def __aenter__(self):
            return client

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(flight_foreflight_fast, "ForeFlightClient", Client)
    monkeypatch.setattr(
        flight_foreflight_fast, "find_flight",
        AsyncMock(return_value=(
            {
                "flightId": "flight-1",
                "departure": "UAAA",
                "destination": "UACC",
                "aircraftRegistration": "UP-ABC",
                "departureTime": "2026-11-20T12:00:00Z",
            },
            "matched",
        )),
    )
    blocks = await flight_foreflight_fast.load_fast_foreflight(flight)
    assert blocks["flighttime"].details["eet_minutes"] == 80
    assert blocks["route"].details["route"] == "DCT TEST"
    assert blocks["route"].details["overflownCountries"] == [
        {"name": "Kazakhstan"}
    ]
    client.get_performance.assert_awaited_once_with("flight-1")
    client.get_overflight.assert_awaited_once_with("flight-1")
    client.get_document.assert_not_awaited()
