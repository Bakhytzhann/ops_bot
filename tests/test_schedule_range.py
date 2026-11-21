from unittest.mock import AsyncMock, call

import pytest

from config.access import current_user_id
from config.settings import settings
from integrations.leon import tools


@pytest.fixture
def leon(monkeypatch):
    monkeypatch.setattr(settings, "telegram_allowed_user_ids", "42")
    token = current_user_id.set(42)
    mock = AsyncMock()
    monkeypatch.setattr(tools, "call_leon", mock)
    yield mock
    current_user_id.reset(token)


@pytest.mark.asyncio
async def test_range_uses_one_time_window(leon):
    leon.return_value = {
        "flightList": [],
        "shouldRerunRequestForNextPageWithOffset": False,
    }
    result = await tools.get_flight_schedule("2026-11-16", "2026-11-30")
    assert result["complete"] is True
    assert result["end_date"] == "2026-11-30"
    leon.assert_awaited_once()
    assert leon.call_args.args[1]["timeRange"] == {
        "fromDateTimeUTC": "2026-11-16T00:00:00Z",
        "toDateTimeUTC": "2026-12-01T00:00:00Z",
    }


@pytest.mark.asyncio
async def test_explicit_next_page_overrides_short_batch(leon):
    leon.side_effect = [
        {
            "flightList": [{"flightNid": 1}],
            "shouldRerunRequestForNextPageWithOffset": True,
        },
        {
            "flightList": [{"flightNid": 2}],
            "shouldRerunRequestForNextPageWithOffset": False,
        },
    ]
    result = await tools.get_flight_schedule("2026-11-11")
    assert result["count"] == 2
    assert result["complete"] is True
    assert leon.await_args_list[1].args[1]["offset"] == 1


@pytest.mark.asyncio
async def test_page_limit_reports_incomplete(leon):
    leon.return_value = {
        "flightList": [{"flightNid": 1}],
        "shouldRerunRequestForNextPageWithOffset": True,
    }
    result = await tools.get_flight_schedule("2026-11-11")
    assert leon.await_count == 10
    assert result["complete"] is False


@pytest.mark.asyncio
async def test_invalid_range_never_calls_leon(leon):
    with pytest.raises(ValueError):
        await tools.get_flight_schedule("2026-11-01", "2026-12-02")
    leon.assert_not_awaited()


@pytest.mark.asyncio
async def test_history_joins_journey_by_id_and_keeps_plan_separate(leon):
    leon.side_effect = [
        {
            "flightsList": [
                {
                    "flightNid": 1,
                    "trip": {"tripNid": 27, "tripNumber": "TR-27"},
                    "endAirport": {"code": {"icao": "UACC"}},
                    "journeyLog": {
                        "endAirport": {
                            "code": {"icao": "UAAA"},
                            "country": "Kazakhstan",
                            "name": "Almaty",
                        },
                        "usedFuel": 345,
                        "fuelUnit": "KG",
                        "flightLog": "Techlog reference",
                    },
                },
                {"flightNid": 2},
                {"flightNid": 3},
            ]
        },
        {"result": [{"flightNid": 2, "journeyLog": None}]},
    ]
    result = await tools.get_flight_history("2026-11-11", "2026-11-12")
    first, second, third = result["flights"]
    assert first["tripNid"] == 27
    assert first["arrival"] == "UACC"
    assert first["journeyLog"] == {
        "endAirport": {"code": {"icao": "UAAA"}, "country": "Kazakhstan"},
        "usedFuel": 345,
        "fuelUnit": "KG",
    }
    assert second["journeyLog"] is None
    assert "journeyLog" not in third
    assert result["schedule_complete"] is True
    assert result["complete"] is False
    assert leon.await_args_list[1] == call(
        "get-flights-by-flight-nids", {"flightNids": [2, 3]}
    )


@pytest.mark.asyncio
async def test_history_batches_details_and_distinguishes_cancelled_notes(leon):
    leon.side_effect = [
        {"flightList": [{"flightNid": nid} for nid in range(1, 51)]},
        {"flightList": [{"flightNid": 51}]},
        [{"flightNid": nid, "journeyLog": None} for nid in range(1, 51)],
        {"flightNid": 51, "journeyLog": "Cancellation note"},
    ]
    result = await tools.get_flight_history("2026-11-11", "2026-11-12")
    assert result["complete"] is True
    assert result["flights"][-1]["journeyLog"] is None
    assert result["flights"][-1]["journeyLogCancelled"] is True
    assert "journeyLogCancelled" not in result["flights"][0]
    assert leon.await_args_list[2:] == [
        call("get-flights-by-flight-nids", {"flightNids": list(range(1, 51))}),
        call("get-flights-by-flight-nids", {"flightNids": [51]}),
    ]


@pytest.mark.asyncio
async def test_schedule_independent_of_telegram_identity(leon):
    leon.return_value = {
        "flightList": [],
        "shouldRerunRequestForNextPageWithOffset": False,
    }
    token = current_user_id.set(None)
    try:
        result = await tools.get_flight_schedule("2026-11-11", "2026-11-30")
        assert "complete" in result
    finally:
        current_user_id.reset(token)
    leon.assert_awaited()
