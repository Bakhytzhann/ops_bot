import asyncio
from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from services import journey_history as history

START = date(2026, 10, 1)
END = date(2026, 10, 2)
SYNC = datetime(2026, 10, 5, tzinfo=UTC)


def flight(nid, **fields):
    return {
        "flightNid": nid,
        "departureUTC": "2026-10-01T22:55:00Z",
        "arrivalUTC": "2026-10-02T00:00:00Z",
        "departure": "UAAA",
        "arrival": "UACC",
        "aircraft": "UP-TEST",
        "aircraftType": "GLF6",
        **fields,
    }


def logged_flight(nid, **log_fields):
    return flight(
        nid,
        journeyLog={
            "startAirport": {"code": {"icao": "UAAA"}},
            "endAirport": {"code": {"icao": "UASK"}},
            "bloffUTC": "2026-10-01T23:00:00",
            "atd": int(datetime(2026, 10, 1, 23, 10, tzinfo=UTC).timestamp()),
            "ata": int(datetime(2026, 10, 2, 0, 5, tzinfo=UTC).timestamp()),
            "blonUTC": "2026-10-02T00:10:00Z",
            "usedFuel": 1000,
            "fuelUnit": "KG",
            "refuel": 250,
            "refuelUnit": "L",
            "distance": 345,
            "flightLog": "Private remarks must stay in Leon",
            **log_fields,
        },
    )


def test_actual_route_time_and_fuel_are_reported_with_coverage(tmp_path):
    path = tmp_path / "history.sqlite"
    repository = history.JourneyHistory(path)
    flights = [
        logged_flight(1, acft={"registration": "UP-TEST"}),
        logged_flight(2, usedFuel=100, fuelUnit="LB", refuel=0, refuelUnit="USGAL"),
        logged_flight(3, usedFuel=100, fuelUnit="L", refuel=100, refuelUnit=None),
        logged_flight(
            4, usedFuel=100, fuelUnit=None, acft={"registration": "UP-OTHER"}
        ),
        logged_flight(5, isClosed=False),
        flight(6, journeyLog=None),
        flight(7, cancelled=True),
        flight(8),
    ]
    repository.ingest(
        START, END, {"complete": True, "flights": flights}, fetched_at=SYNC
    )
    summary = repository.summary(START, END)
    assert not summary["complete"]
    assert repository.complete_days(START, END) == {END}
    assert summary["actual_count"] == 4
    assert summary["known_log_count"] == 5
    assert summary["missing_log_count"] == 1
    assert summary["cancelled_count"] == 1
    assert summary["unknown_log_count"] == 1
    assert summary["routes"] == [{"departure": "UAAA", "arrival": "UASK", "count": 4}]
    assert summary["route_aircraft_count"] == 1
    assert summary["block_minutes"]["mean"] == 70
    assert summary["planned_block_minutes"]["mean"] == 65
    assert summary["planned_block_minutes"]["count"] == 7
    assert summary["airborne_minutes"]["mean"] == 55
    assert summary["block_delta_minutes"]["mean"] == 5
    assert summary["distance_nm"]["mean"] == 345
    assert summary["fuel"]["KG"]["total"] == 1045.36
    assert summary["fuel"]["L"]["total"] == 100
    assert summary["fuel_missing_unit_count"] == 1
    assert summary["fuel_uplift"]["L"]["total"] == 500
    assert summary["fuel_uplift"]["USGAL"]["total"] == 0
    assert repository.summary(START, END, arrival="UACC")["actual_count"] == 0
    assert repository.summary(START, END, arrival="UASK")["actual_count"] == 4
    assert repository.summary(START, END, aircraft_type="GLF6")["actual_count"] == 1
    assert b"Private remarks" not in path.read_bytes()


def test_resync_upserts_corrected_log_and_retains_facts_after_partial_read(tmp_path):
    repository = history.JourneyHistory(tmp_path / "history.sqlite")
    repository.ingest(
        START,
        END,
        {"complete": True, "flights": [logged_flight(1), logged_flight(2)]},
        fetched_at=SYNC,
    )
    repository.ingest(
        START,
        END,
        {"complete": False, "flights": [flight(1)]},
        fetched_at=SYNC + timedelta(seconds=1),
    )
    partial = repository.summary(START, END)
    assert partial["actual_count"] == 2
    assert not partial["complete"]
    repository.ingest(
        START,
        END,
        {"complete": True, "flights": [logged_flight(1, usedFuel=500)]},
        fetched_at=SYNC + timedelta(seconds=2),
    )
    corrected = repository.summary(START, END)
    assert corrected["complete"]
    assert corrected["actual_count"] == 1
    assert corrected["fuel"]["KG"]["total"] == 500
    repository.ingest(
        START,
        END,
        {"complete": True, "flights": [logged_flight(1), logged_flight(2)]},
        fetched_at=SYNC,
    )
    assert repository.summary(START, END) == corrected
    repository.ingest(
        START,
        END,
        {"complete": True, "flights": [flight(1, journeyLog=None)]},
        fetched_at=SYNC + timedelta(seconds=3),
    )
    assert repository.summary(START, END)["missing_log_count"] == 1
    assert not repository.summary(START, END)["fuel"]


@pytest.mark.asyncio
async def test_refresh_partitions_period_into_inclusive_api_windows(monkeypatch):
    fetch = AsyncMock(return_value={"complete": True, "flights": []})
    monkeypatch.setattr(history, "get_flight_history", fetch)
    summary = await history.refresh_journey_history(date(2020, 7, 1), date(2020, 9, 28))
    assert [call.args for call in fetch.await_args_list] == [
        ("2020-07-01", "2020-07-31"),
        ("2020-08-01", "2020-08-31"),
        ("2020-09-01", "2020-09-28"),
    ]
    assert summary["complete"]
    assert summary["covered_days"] == 90
    assert summary["scheduled_count"] == 0


@pytest.mark.asyncio
async def test_insights_backfill_year_skips_cached_days_and_reuses_results(monkeypatch):
    start, end = date(2020, 1, 1), date(2020, 12, 31)
    cached_start, cached_end = date(2020, 4, 1), date(2020, 4, 30)
    result = {"complete": True, "flights": []}
    history.journey_history.ingest(cached_start, cached_end, result, fetched_at=SYNC)
    fetch = AsyncMock(return_value=result)
    monkeypatch.setattr(history, "get_flight_history", fetch)

    summary = await history.load_journey_insights(start, end)

    fetched_days = set()
    for call in fetch.await_args_list:
        first, last = map(date.fromisoformat, call.args)
        count = (last - first).days + 1
        assert 1 <= count <= 31
        assert last < cached_start or first > cached_end
        fetched_days.update(first + timedelta(days=i) for i in range(count))
    assert len(fetched_days) == 336
    assert summary["covered_days"] == 366
    assert not summary["source_failed"]
    fetch.reset_mock()
    assert await history.load_journey_insights(start, end) == summary
    fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_insights_keeps_cached_data_on_leon_failure(monkeypatch):
    history.journey_history.ingest(
        START, START, {"complete": True, "flights": [logged_flight(1)]}, fetched_at=SYNC
    )
    fetch = AsyncMock(side_effect=OSError("network unavailable"))
    monkeypatch.setattr(history, "get_flight_history", fetch)

    summary = await history.load_journey_insights(START, END)

    fetch.assert_awaited_once_with(END.isoformat(), END.isoformat())
    assert summary["source_failed"]
    assert not summary["complete"]
    assert summary["covered_days"] == 1
    assert summary["actual_count"] == 1

    fetch.reset_mock()
    refreshed = await history.load_journey_insights(START, END, refresh=True)
    fetch.assert_awaited_once_with(START.isoformat(), END.isoformat())
    assert refreshed == summary


@pytest.mark.asyncio
async def test_insights_refreshes_only_stale_recent_day(monkeypatch):
    now = datetime.now(UTC)
    yesterday = now.date() - timedelta(days=1)
    before = yesterday - timedelta(days=1)
    old = before - timedelta(days=1)
    result = {"complete": True, "flights": []}
    stale = now - history.RECENT_CACHE_TTL - timedelta(minutes=1)
    history.journey_history.ingest(old, before, result, fetched_at=stale)
    history.journey_history.ingest(yesterday, yesterday, result, fetched_at=now)
    fetch = AsyncMock(return_value=result)
    monkeypatch.setattr(history, "get_flight_history", fetch)

    summary = await history.load_journey_insights(old, yesterday)

    fetch.assert_awaited_once_with(before.isoformat(), before.isoformat())
    assert summary["covered_days"] == 3
    fetch.reset_mock()
    await history.load_journey_insights(old, yesterday)
    fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_insights_shares_identical_concurrent_imports(monkeypatch):
    release = asyncio.Event()

    async def get_history(start, end):
        await release.wait()
        return {"complete": True, "flights": []}

    fetch = AsyncMock(side_effect=get_history)
    monkeypatch.setattr(history, "get_flight_history", fetch)

    async with asyncio.TaskGroup() as tasks:
        first = tasks.create_task(history.load_journey_insights(START, END))
        second = tasks.create_task(history.load_journey_insights(START, END))
        release.set()

    assert first.result() == second.result()
    assert first.result()["complete"]
    fetch.assert_awaited_once_with(START.isoformat(), END.isoformat())
