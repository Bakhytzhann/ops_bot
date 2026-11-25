from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from services import charter_crew, flight_duty

FERRY = {
    "flightNid": 7,
    "flightNumber": "OPS701",
    "departure": "UAAA",
    "arrival": "UACC",
    "departureUTC": "2026-11-23T12:00:00+05:00",
    "arrivalUTC": "2026-11-23T08:30:00Z",
    "ferry": True,
}
MEMBER = {
    "login": {"loginNid": 123},
    "contact": {"name": "Private name"},
    "position": {"name": "Captain"},
}


@pytest.mark.asyncio
@pytest.mark.parametrize("flight", [None, {**FERRY, "cancelled": True}])
async def test_hypothetical_or_cancelled_flight_requires_confirmation(
    monkeypatch, flight
):
    get_crew = AsyncMock()
    monkeypatch.setattr(charter_crew, "get_flight_crew", get_crew)

    assert await charter_crew.charter_crew(flight) == {
        "status": "confirmation_required",
        "flight": None,
        "members": [],
    }
    get_crew.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("ferry", [True, False])
async def test_ftl_uses_the_matching_leon_flight_and_omits_personal_details(
    monkeypatch, ferry
):
    get_crew = AsyncMock(
        return_value={
            "crew": [
                {"flightNid": 8, "crewMemberList": [MEMBER]},
                {"flightNid": 7, "crewMemberList": [MEMBER]},
            ]
        }
    )
    monkeypatch.setattr(charter_crew, "get_flight_crew", get_crew)

    async def call_leon(tool, arguments):
        if tool == "get-duty-list":
            return []
        assert tool == "get-crew-ftl-sheet"
        assert arguments["crewMember"] == 123
        calculation = {
            "fdpSectors": [{"nid": 7}],
            "fdpLength": {"actual": 36000, "limit": 39600},
            "restBeforeDuty": {"actual": 43200, "limit": 36000},
            "restAfterDuty": {
                "actual": 28800,
                "limit": 36000,
                "offence": True,
                "label": "Rest After",
            },
            "nextEOBT": "2026-11-24T09:00:00Z",
        }
        return {
            "ftl": {
                "ftlCalculationsForCrewMember": {
                    "calculations": [{"ftlCalculations": [calculation]}]
                }
            }
        }

    monkeypatch.setattr(flight_duty, "call_leon", call_leon)
    result = await charter_crew.charter_crew({**FERRY, "ferry": ferry})

    get_crew.assert_awaited_once_with(7)
    assert result == {
        "status": "available",
        "flight": {
            "id": 7,
            "number": "OPS701",
            "departure": "UAAA",
            "arrival": "UACC",
            "departure_utc": datetime(2026, 11, 23, 7, tzinfo=UTC),
            "arrival_utc": datetime(2026, 11, 23, 8, 30, tzinfo=UTC),
        },
        "members": [
            {
                "role": "Captain",
                "fdp_remaining": "01:00",
                "rest_before": {"actual": "12:00", "limit": "10:00"},
                "rest_after": {"actual": "08:00", "limit": "10:00"},
                "next_eobt": datetime(2026, 11, 24, 9, tzinfo=UTC),
                "violations": ["Rest After"],
            }
        ],
    }


@pytest.mark.asyncio
async def test_scheduled_ferry_without_crew_requires_confirmation(monkeypatch):
    get_crew = AsyncMock(
        return_value={"crew": [{"flightNid": 7, "crewMemberList": []}]}
    )
    load_duty = AsyncMock()
    monkeypatch.setattr(charter_crew, "get_flight_crew", get_crew)
    monkeypatch.setattr(charter_crew, "load_duty", load_duty)

    result = await charter_crew.charter_crew(FERRY)

    assert result["status"] == "confirmation_required"
    assert result["flight"]["id"] == 7
    assert result["members"] == []
    load_duty.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_leon_ftl_is_unavailable_even_with_assigned_crew(monkeypatch):
    get_crew = AsyncMock(
        return_value={"crew": [{"flightNid": 7, "crewMemberList": [MEMBER]}]}
    )
    monkeypatch.setattr(charter_crew, "get_flight_crew", get_crew)
    monkeypatch.setattr(flight_duty, "call_leon", AsyncMock(side_effect=TimeoutError()))

    result = await charter_crew.charter_crew(FERRY)

    assert result["status"] == "unavailable"
    assert result["flight"]["id"] == 7
    assert result["members"][0]["role"] == "Captain"
    assert result["members"][0]["fdp_remaining"] is None


@pytest.mark.asyncio
async def test_leon_crew_source_failure_preserves_ferry_context(monkeypatch):
    get_crew = AsyncMock(return_value={"crew": {"errors": ["Unavailable"]}})
    monkeypatch.setattr(charter_crew, "get_flight_crew", get_crew)

    result = await charter_crew.charter_crew(FERRY)

    assert result["status"] == "unavailable"
    assert result["flight"]["id"] == 7
    assert result["members"] == []
