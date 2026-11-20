"""Operational inputs and results, without caller identity."""

from dataclasses import dataclass

from integrations.awc.client import get_weather
from integrations.leon.operations import get_crew_operations, resolve_flight
from integrations.leon.tools import get_flight_crew, get_flight_schedule
from services.flight_duty import load_duty
from services.foreflight_operations import foreflight_operation
from services.operational_routes import flight_weather, selected_flight_weather
from services.snapshot import load_flight_snapshot, render_snapshot
from services.weather_reports import awc_reports


@dataclass
class OperationResult:
    text: str
    data: object = None


async def flight_schedule(date, end_date=None):
    return await get_flight_schedule(date, end_date)


async def prepare_flight(
    selected, *, refresh=False, on_update=None, from_sked=False
):
    options = {"refresh": refresh, "on_update": on_update}
    if from_sked:
        options["from_sked"] = True
    return await load_flight_snapshot(selected, **options)


async def preparation_by_number(number, day):
    return await prepare_flight(await resolve_flight(number, day))


async def flight_crew(number, day):
    flight = await resolve_flight(number, day)
    result = await get_flight_crew(flight["flightNid"])
    return {"flight": flight, **result}


async def crew_roster(crew, start_date, end_date, mode="roster"):
    return await get_crew_operations(crew, start_date, end_date, mode)


async def duty_rest(number, day):
    result = await flight_crew(number, day)
    return await load_duty(result["flight"], result["crew"])


async def weather(*, number=None, day=None, airports=None, include_taf=False):
    if number is not None:
        return await flight_weather(number, day, include_taf)
    result = await get_weather(airports or [], include_taf=include_taf)
    reports, errors = awc_reports(result, include_taf)
    return {**result, "reports": reports, "errors": errors}


async def selected_weather(flight, day, include_taf=False):
    return await selected_flight_weather(flight, day, include_taf)


async def preparation_section(number, day, section):
    if section not in {"notams", "route"}:
        raise ValueError("Unknown preparation section")
    snapshot = await preparation_by_number(number, day)
    return snapshot.blocks[section]


async def foreflight(mode, number, day):
    return OperationResult(await foreflight_operation(mode, number, day))


async def bot_report(number, day):
    snapshot = await preparation_by_number(number, day)
    return OperationResult(render_snapshot(snapshot, report=True), snapshot)
