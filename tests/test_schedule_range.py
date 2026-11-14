from unittest.mock import AsyncMock

import pytest

from config.access import current_user_id
from config.settings import settings
from integrations.leon import tools


@pytest.fixture
def leon(monkeypatch):
    # Replace authorization with a local test identity policy; never call Leon.
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
async def test_access_required(leon):
    token = current_user_id.set(None)
    try:
        with pytest.raises(PermissionError):
            await tools.get_flight_schedule("2026-11-11", "2026-11-30")
    finally:
        current_user_id.reset(token)
    leon.assert_not_awaited()
