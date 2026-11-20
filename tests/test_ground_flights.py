import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from services import ground_flights
from services.source_cache import SourceCache

NOW = datetime(2026, 11, 18, 12, tzinfo=UTC)


def leg(nid, origin, destination, start, end, *, tail="UP-ABC", status="CONFIRMED"):
    return {
        "flightNid": nid,
        "flightNumber": f"ABC{nid}",
        "departure": origin,
        "arrival": destination,
        "departureUTC": f"2026-11-18T{start}:00Z",
        "arrivalUTC": f"2026-11-18T{end}:00Z",
        "aircraft": tail,
        "aircraftType": "E35L",
        "status": status,
    }


@pytest.fixture
def leon(monkeypatch):
    monkeypatch.setattr(ground_flights, "cache", SourceCache())
    schedule = AsyncMock()
    watch = AsyncMock()
    monkeypatch.setattr(ground_flights, "get_flight_schedule", schedule)
    monkeypatch.setattr(ground_flights, "call_leon", watch)
    return schedule, watch


@pytest.mark.asyncio
async def test_ground_status_only_uses_leon_schedule_not_flight_watch(leon):
    schedule, watch = leon
    schedule.return_value = {
        "complete": True,
        "flights": [
            leg(1, "UAAA", "UACC", "08:00", "10:30"),
            leg(2, "UACC", "UAAA", "12:30", "14:00", tail="UPABC"),
            leg(3, "UUWW", "UAAA", "01:00", "03:00", tail="UP-BBB"),
            leg(4, "UAAA", "UUWW", "11:00", "11:40", tail="UP-CCC"),
            leg(5, "UAAA", "UUWW", "17:20", "19:20", tail="UP-DDD"),
            leg(6, "UAAA", "UUWW", "12:30", "13:40", status="OPTION"),
        ],
    }
    board = await ground_flights.load_ground_flights(now=NOW)
    cards = {card["key"]: card for card in board["cards"]}
    assert board["schedule_complete"] and board["watch_complete"]
    assert cards["UACC:o:2"]["inbound_id"] == 1
    assert cards["UACC:o:2"]["actual_in"] is None
    assert cards["UACC:o:2"]["estimated_out"] is None
    assert cards["UACC:o:2"]["planned_out"].hour == 12
    assert cards["UAAA:o:4"]["status"] == "active"
    assert cards["UAAA:o:5"]["status"] == "active"
    arrival = cards["UAAA:i:2"]
    assert arrival["phase"] == "arrival"
    assert arrival["estimated_landing"] is None
    assert "UACC:i:1" not in cards
    assert not any(card["outbound_id"] == 6 for card in cards.values())
    watch.assert_not_awaited()
    await ground_flights.load_ground_flights(now=NOW)
    schedule.assert_awaited_once()


@pytest.mark.asyncio
async def test_incomplete_schedule_does_not_fetch_flight_watch(leon):
    schedule, watch = leon
    schedule.return_value = {
        "complete": False,
        "flights": [
            leg(1, "UAAA", "UACC", "08:00", "10:30"),
            leg(2, "UACC", "UAAA", "12:30", "14:00"),
        ],
    }
    board = await ground_flights.load_ground_flights(now=NOW)
    cards = {card["key"]: card for card in board["cards"]}
    assert not board["schedule_complete"]
    assert board["watch_complete"]
    assert cards["UACC:o:2"]["inbound_id"] is None
    assert cards["UACC:o:2"]["time_basis"] == "planned"
    assert cards["UACC:i:1"]["status"] == "active"
    watch.assert_not_awaited()


@pytest.mark.asyncio
async def test_watch_batches_share_latency_and_keep_successful_results(leon):
    _, watch = leon
    second_started = asyncio.Event()
    legs = [
        {"id": nid, "planned_in": NOW, "planned_out": NOW}
        for nid in range(1, ground_flights.WATCH_BATCH_SIZE + 2)
    ]

    async def source(_tool, arguments):
        if 1 in arguments["flightNids"]:
            await second_started.wait()
            return [
                {
                    "flightNid": 1,
                    "flightWatch": {"blonIso": "2026-11-18T12:00:00Z"},
                }
            ]
        second_started.set()
        raise RuntimeError("Other batch failed")

    watch.side_effect = source
    result, complete = await asyncio.wait_for(
        ground_flights._load_watch(legs, NOW), timeout=2
    )
    assert result == {1: {"actual_in": NOW}}
    assert not complete
