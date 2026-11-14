from unittest.mock import AsyncMock

import pytest

from config.access import current_user_id
from config.settings import settings
from services import foreflight_operations as ops

FID = "abcdefab1234456789ababcdefabcdef"


class Client:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "telegram_allowed_user_ids", "42")
    token = current_user_id.set(42)
    c = Client()
    row = {
        "flightId": FID,
        "callSign": "ABC1265",
        "departure": "UTTT",
        "destination": "UAAA",
        "aircraftRegistration": "UP-TEST",
        "departureTime": "2026-11-11T12:00:00Z",
        "arrivalTime": "2026-11-11T13:30:00Z",
    }
    c.list_flights = AsyncMock(return_value={"flights": [row], "warnings": []})
    c.get_flight = AsyncMock(
        return_value={
            "flightData": {
                "departure": "UTTT",
                "destination": "UAAA",
                "aircraftRegistration": "UP-TEST",
                "scheduledTimeOfDeparture": "2026-11-11T12:00:00Z",
                "routeToDestination": {
                    "route": "TEST DCT ROUTE",
                    "altitude": {"altitude": 350, "unit": "FL"},
                },
                "alternate": "UACC",
                "flightRule": "IFR",
                "flightLogTime": {
                    "timeOff": "2026-11-11T12:10:00Z",
                    "timeOn": "2026-11-11T13:40:00Z",
                },
            },
            "performance": {
                "times": {
                    "timeToDestinationMinutes": 90,
                    "totalTimeMinutes": 240,
                    "taxiTimeMinutes": 10,
                }
            },
            "errors": [],
        }
    )
    c.get_document = AsyncMock(
        return_value={
            "url": "https://docs.example.test/briefing?signature=test",
            "timeGenerated": "2026-11-11T10:00:00Z",
        }
    )
    c.get_overflight = AsyncMock(
        return_value={
            "overflownCountries": [
                {
                    "isoCode": "KAZ",
                    "name": "Kazakhstan",
                    "entryTime": "12:10",
                    "exitTime": "13:00",
                }
            ],
            "overflownFirs": [
                {"identifier": "UAAA", "name": "Almaty", "airspaceType": "FIR"}
            ],
        }
    )
    monkeypatch.setattr(ops, "ForeFlightClient", lambda: c)
    yield c
    current_user_id.reset(token)


@pytest.mark.asyncio
async def test_flight_time_is_not_endurance(client):
    text = await ops.foreflight_operation("flighttime", "ABC1265", "2026-11-11")
    assert "ETE): 01:30" in text
    assert "взлёт–посадка): 01:30" in text
    assert "04:00" not in text
    client.get_flight.assert_awaited_once_with(FID)


@pytest.mark.asyncio
async def test_route_has_altitude_and_alternate(client):
    text = await ops.foreflight_operation("route", "ABC1265", "2026-11-11")
    assert "TEST DCT ROUTE" in text and "350 FL" in text and "UACC" in text


@pytest.mark.asyncio
async def test_briefing_preserves_source_link_and_time(client):
    text = await ops.foreflight_operation("briefing", "ABC1265", "2026-11-11")
    assert "https://docs.example.test/briefing?signature=test" in text
    assert "2026-11-11T10:00:00Z" in text
    client.get_document.assert_awaited_once_with(FID, "briefing")
    client.get_flight.assert_not_awaited()


@pytest.mark.asyncio
async def test_overflight_does_not_claim_permit(client):
    text = await ops.foreflight_operation("overflight", "ABC1265", "2026-11-11")
    assert "Kazakhstan" in text and "UAAA Almaty" in text
    assert "разрешений на пролёт здесь не проверяется" in text


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [
            {
                "callSign": "ABC1265",
                "flightId": FID,
                "departureTime": "2026-11-12T00:00:00Z",
            }
        ],
    ],
)
@pytest.mark.asyncio
async def test_missing_or_next_day_flight_never_fetches_documents(client, rows):
    client.list_flights.return_value = {"flights": rows}
    with pytest.raises(ValueError):
        await ops.foreflight_operation("briefing", "ABC1265", "2026-11-11")
    client.get_document.assert_not_awaited()


@pytest.mark.asyncio
async def test_ambiguous_callsign_is_not_guessed(client):
    client.list_flights.return_value["flights"] *= 2
    with pytest.raises(ValueError, match="несколько"):
        await ops.foreflight_operation("route", "ABC1265", "2026-11-11")
    client.get_flight.assert_not_awaited()


@pytest.mark.asyncio
async def test_compare_detects_schedule_difference(client, monkeypatch):
    monkeypatch.setattr(
        ops,
        "resolve_flight",
        AsyncMock(
            return_value={
                "departure": "UTTT",
                "arrival": "UAAA",
                "aircraft": "UPTEST",
                "departureUTC": "2026-11-11T11:45:00Z",
                "arrivalUTC": "2026-11-11T13:15:00Z",
            }
        ),
    )
    text = await ops.foreflight_operation("compare", "ABC1265", "2026-11-11")
    assert "РАСХОЖДЕНИЕ: +15 мин" in text
    assert "имеют разный смысл" in text
    assert "Борт: совпадает" in text


@pytest.mark.asyncio
async def test_compare_fallback_requires_all_identifiers(client):
    client.list_flights.return_value["flights"][0]["callSign"] = "OTHER"
    leon = {
        "departure": "UTTT",
        "arrival": "UAAA",
        "aircraft": "UPTEST",
        "departureUTC": "2026-11-11T11:45:00Z",
    }
    row, basis = await ops.find_flight(client, "ABC1265", "2026-11-11", leon)
    assert row["flightId"] == FID
    assert "60 минут" in basis
    leon["aircraft"] = "WRONG"
    with pytest.raises(ValueError):
        await ops.find_flight(client, "ABC1265", "2026-11-11", leon)


@pytest.mark.asyncio
async def test_operations_require_identity(client):
    token = current_user_id.set(None)
    try:
        text = await ops.foreflight_operation("route", "ABC1265", "2026-11-11")
        assert "ForeFlight" in text
    finally:
        current_user_id.reset(token)
    client.list_flights.assert_awaited()
