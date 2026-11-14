import ast
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from application import operations as ops
from application.commands import execute_operation
from services.snapshot_models import FlightSnapshot, SourceBlock

FLIGHT = {
    "flightNid": 1,
    "flightNumber": "ABC1265",
    "departureUTC": "2026-11-11T12:00:00Z",
    "departure": "UAAA",
    "arrival": "UACC",
}


@pytest.mark.asyncio
async def test_schedule_returns_source_data_without_identity(monkeypatch):
    result = {"flights": [FLIGHT], "complete": True}
    query = AsyncMock(return_value=result)
    monkeypatch.setattr(ops, "get_flight_schedule", query)
    assert await ops.flight_schedule("2026-11-11", "2026-11-12") is result
    query.assert_awaited_once_with("2026-11-11", "2026-11-12")


@pytest.mark.asyncio
async def test_preparation_preserves_snapshot_and_progress(monkeypatch):
    snapshot = FlightSnapshot(FLIGHT)
    load = AsyncMock(return_value=snapshot)
    progress = AsyncMock()
    monkeypatch.setattr(ops, "load_flight_snapshot", load)
    assert (
        await ops.prepare_flight(FLIGHT, refresh=True, on_update=progress) is snapshot
    )
    load.assert_awaited_once_with(FLIGHT, refresh=True, on_update=progress)


@pytest.mark.asyncio
async def test_crew_and_duty_reuse_resolved_flight_and_crew(monkeypatch):
    monkeypatch.setattr(ops, "resolve_flight", AsyncMock(return_value=FLIGHT))
    crew = {"crew": [{"flightNid": 1, "crewMemberList": []}]}
    query = AsyncMock(return_value=crew)
    monkeypatch.setattr(ops, "get_flight_crew", query)
    block = SourceBlock("Leon", "Duty source")
    duty = AsyncMock(return_value=block)
    monkeypatch.setattr(ops, "load_duty", duty)
    assert await ops.duty_rest("ABC1265", "2026-11-11") is block
    duty.assert_awaited_once_with(FLIGHT, crew["crew"])
    query.assert_awaited_once_with(1)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["roster", "ftl", "violations"])
async def test_crew_modes_preserve_structured_data(monkeypatch, mode):
    data = {"crew": {"name": "Test"}, "data": []}
    query = AsyncMock(return_value=data)
    monkeypatch.setattr(ops, "get_crew_operations", query)
    assert await ops.crew_roster("TEST", "2026-11-11", "2026-11-12", mode) is data
    query.assert_awaited_once_with("TEST", "2026-11-11", "2026-11-12", mode)


@pytest.mark.asyncio
async def test_weather_keeps_source_data_and_reports(monkeypatch):
    source = {
        "source": "AWC",
        "airports": ["UAAA"],
        "products": [
            {
                "product": "metar",
                "data": [{"icaoId": "UAAA", "rawOb": "UAAA 111200Z CAVOK"}],
            }
        ],
    }
    query = AsyncMock(return_value=source)
    monkeypatch.setattr(ops, "get_weather", query)
    result = await ops.weather(airports=["UAAA"], include_taf=True)
    assert result["products"] is source["products"]
    assert result["reports"][0]["raw"] == "UAAA 111200Z CAVOK"
    query.assert_awaited_once_with(["UAAA"], include_taf=True)


@pytest.mark.asyncio
async def test_flight_weather_reuses_combined_sources(monkeypatch):
    data = {"answer": "WX", "reports": [], "partial": True}
    query = AsyncMock(return_value=data)
    monkeypatch.setattr(ops, "flight_weather", query)
    assert await ops.weather(number="ABC1265", day="2026-11-11") is data
    query.assert_awaited_once_with("ABC1265", "2026-11-11", False)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode", ["route", "flighttime", "briefing", "overflight", "compare"]
)
async def test_foreflight_modes_use_existing_workflow(monkeypatch, mode):
    query = AsyncMock(return_value="ForeFlight result")
    monkeypatch.setattr(ops, "foreflight_operation", query)
    result = await execute_operation(mode, "ABC1265 на 2026-11-11")
    assert result.text == "ForeFlight result"
    query.assert_awaited_once_with(mode, "ABC1265", "2026-11-11")


@pytest.mark.asyncio
async def test_notam_and_report_preserve_snapshot(monkeypatch):
    snapshot = FlightSnapshot(
        FLIGHT,
        blocks={
            "notams": SourceBlock("ForeFlight briefing", "NOTAM source"),
            "route": SourceBlock("ForeFlight", "RTE"),
        },
    )
    monkeypatch.setattr(ops, "preparation_by_number", AsyncMock(return_value=snapshot))
    notams = await execute_operation("notams", "ABC1265 на 2026-11-11")
    assert notams.data is snapshot.blocks["notams"]
    assert notams.text == "NOTAM source"
    report = await execute_operation("report", "ABC1265 на 2026-11-11")
    assert report.data is snapshot
    assert "Bot Report" in report.text and "NOTAM source" in report.text


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["watch", "ai", "unknown"])
async def test_unsupported_operations_never_load_sources(monkeypatch, name):
    query = AsyncMock()
    monkeypatch.setattr(ops, "resolve_flight", query)
    with pytest.raises(ValueError):
        await execute_operation(name, "ABC1265 на 2026-11-11")
    query.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_date_does_not_fetch_preparation(monkeypatch):
    query = AsyncMock()
    monkeypatch.setattr(ops, "resolve_flight", query)
    with pytest.raises(ValueError):
        await execute_operation("prepare", "ABC1265")
    query.assert_not_awaited()


def test_application_services_integrations_do_not_import_telegram_context():
    for folder in ("application", "services", "integrations"):
        for path in Path(folder).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names = (
                    [node.module or ""]
                    if isinstance(node, ast.ImportFrom)
                    else [alias.name for alias in node.names]
                    if isinstance(node, ast.Import)
                    else []
                )
                assert not any(
                    name == "config.access" or name.split(".")[0] in {"bot", "telegram"}
                    for name in names
                ), str(path)
                if folder == "application":
                    assert not any(name.startswith("agent") for name in names), str(
                        path
                    )
