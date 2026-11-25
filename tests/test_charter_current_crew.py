from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from services import charter_current_crew as current

DAY = datetime(2026, 11, 24, tzinfo=UTC)
FLIGHT = {
    "flightNid": 23,
    "flightNumber": "OPS401",
    "departure": "UAAA",
    "arrival": "OMDW",
    "departureUTC": (DAY + timedelta(hours=11)).isoformat(),
    "arrivalUTC": (DAY + timedelta(hours=13)).isoformat(),
    "aircraft": "UP-TEST",
    "aircraftType": "GLF6",
}
FLEET = [{"registration": "UP-TEST", "type": "G650"}]
ASSIGNMENT = {
    "crew": [
        {
            "flightNid": 23,
            "crewMemberList": [
                {
                    "contact": {"name": "Alex", "surname": "Test"},
                    "position": {"name": "Captain"},
                    "login": {"loginNid": 317},
                }
            ],
        }
    ]
}


@pytest.fixture
def leon_duty(monkeypatch):
    calculation = {
        "dutyLength": 12600,
        "dutyLimitedLength": {"limitType": "MAX", "limit": 2147483647},
        "fdpLength": {"actual": 10800, "limitType": "MAX", "limit": 21600},
        "restAfterDuty": {"limitType": "MIN", "limit": 25200},
        "nextEOBT": (DAY + timedelta(hours=22)).isoformat(),
        "fdpSectors": [
            {
                "nid": 23,
                "isInFdp": True,
                "schedule": {
                    "date": DAY.isoformat(),
                    "startTime": "11:00",
                    "endTime": "13:00",
                    "cnl": False,
                },
                "reportOn": {"time": "10:00"},
                "reportOff": {"time": "13:30"},
            }
        ],
    }
    monkeypatch.setattr(current, "get_flight_crew", AsyncMock(return_value=ASSIGNMENT))
    monkeypatch.setattr(
        current,
        "call_leon",
        AsyncMock(
            return_value={
                "ftl": {
                    "ftlCalculationsForCrewMember": {
                        "calculations": [{"ftlCalculations": [calculation]}]
                    }
                }
            }
        ),
    )
    return calculation


@pytest.mark.asyncio
async def test_confirmed_movement_reports_current_crew_and_distinct_fdp(
    monkeypatch, leon_duty
):
    watch = {
        23: {
            "actual_out": DAY + timedelta(hours=11),
            "takeoff": DAY + timedelta(hours=11, minutes=5),
        }
    }
    monkeypatch.setattr(current, "_load_watch", AsyncMock(return_value=(watch, True)))
    report = await current.current_fleet_crew(
        FLEET, {"flights": [FLIGHT], "complete": True}, now=DAY + timedelta(hours=12)
    )
    row = report["aircraft"][0]
    assert row["position"] == {"current_station": None, "current_status": "airborne"}
    member = row["crew"]["members"][0]
    assert row["crew"]["status"] == "on_duty"
    assert member["name"] == "Alex Test"
    assert member["duty_elapsed_seconds"] == 2 * 3600
    assert member["duty_limit_seconds"] is None
    assert "duty_remaining_seconds" not in member
    assert member["fdp_remaining_seconds"] == 4 * 3600
    assert member["fdp_limit_utc"] == (DAY + timedelta(hours=16)).isoformat()


@pytest.mark.asyncio
async def test_rest_uses_actual_on_block_plus_leon_post_duty_and_min_rest(
    monkeypatch, leon_duty
):
    watch = {
        23: {
            "actual_out": DAY + timedelta(hours=11),
            "actual_in": DAY + timedelta(hours=13, minutes=20),
        }
    }
    monkeypatch.setattr(current, "_load_watch", AsyncMock(return_value=(watch, True)))
    schedule = {"flights": [FLIGHT], "complete": True}
    report = await current.current_fleet_crew(
        FLEET, schedule, now=DAY + timedelta(hours=14)
    )
    row = report["aircraft"][0]
    assert row["position"] == {"current_station": "OMDW", "current_status": "on_ground"}
    assert row["crew"]["status"] == "resting"
    assert (
        row["crew"]["members"][0]["rest_until_utc"]
        == (DAY + timedelta(hours=20, minutes=50)).isoformat()
    )

    report = await current.current_fleet_crew(
        FLEET, schedule, now=DAY + timedelta(hours=21)
    )
    assert report["aircraft"][0]["crew"]["members"] == []


@pytest.mark.asyncio
async def test_schedule_alone_does_not_attribute_crew_or_actual_station(monkeypatch):
    monkeypatch.setattr(current, "_load_watch", AsyncMock(return_value=({}, False)))
    crew = AsyncMock(return_value=ASSIGNMENT)
    monkeypatch.setattr(current, "get_flight_crew", crew)
    report = await current.current_fleet_crew(
        FLEET, {"flights": [FLIGHT], "complete": True}, now=DAY + timedelta(hours=12)
    )
    row = report["aircraft"][0]
    assert row["position"] == {"current_station": None, "current_status": "unconfirmed"}
    assert row["crew"]["members"] == []
    assert row["crew"]["reason"] == "incomplete_flight_watch"
    crew.assert_not_called()
