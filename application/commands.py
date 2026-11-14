"""Parameter parsing shared by button and command adapters."""

import re

from application import operations
from services.crew_format import format_flight_crew
from services.operational_format import format_crew_operations
from services.operational_routes import FLIGHT_PATTERN, _crew_query
from services.schedule_dates import parse_schedule_period
from services.schedule_format import format_schedule
from services.snapshot import render_snapshot
from services.weather_reports import format_reports


def single_flight(arguments):
    match = FLIGHT_PATTERN.search(arguments)
    period = parse_schedule_period(arguments)
    if not match or not period or period.start != period.end:
        raise ValueError("Укажите один рейс и одну дату (UTC): ABC1265 на 2026-11-11.")
    return re.sub(r"[\s-]", "", match.group()).upper(), period.start.isoformat()


async def execute_operation(name, arguments):
    if name == "flights":
        period = parse_schedule_period(arguments)
        if not period:
            raise ValueError("Укажите дату или период расписания.")
        data = await operations.flight_schedule(
            period.start.isoformat(), period.end.isoformat()
        )
        return operations.OperationResult(format_schedule(data), data)
    if name in {"roster", "ftl", "violations"}:
        period = parse_schedule_period(arguments)
        crew = _crew_query(arguments)
        if not period or not crew:
            raise ValueError("Укажите сотрудника и период: «Иванов» на 2026-11-11.")
        data = await operations.crew_roster(
            crew, period.start.isoformat(), period.end.isoformat(), name
        )
        return operations.OperationResult(format_crew_operations(data), data)
    if name == "weather":
        taf = bool(re.search(r"\b(?:taf|таф)\b", arguments, re.IGNORECASE))
        if FLIGHT_PATTERN.search(arguments):
            number, day = single_flight(arguments)
            data = await operations.weather(number=number, day=day, include_taf=taf)
            text = data["answer"]
        else:
            airports = re.findall(r"\b[A-Z]{4}\b", arguments.upper())
            data = await operations.weather(airports=airports, include_taf=taf)
            text = format_reports(data["reports"], data["errors"])
        return operations.OperationResult(text, data)
    if name not in {
        "prepare",
        "crew",
        "duty",
        "notams",
        "route",
        "report",
        "flighttime",
        "briefing",
        "overflight",
        "compare",
    }:
        raise ValueError("Неизвестная операция. Откройте /menu.")
    number, day = single_flight(arguments)
    if name == "crew":
        data = await operations.flight_crew(number, day)
        return operations.OperationResult(
            format_flight_crew(number, day, data["crew"]), data
        )
    if name == "duty":
        block = await operations.duty_rest(number, day)
        return operations.OperationResult(block.text, block)
    if name == "prepare":
        snapshot = await operations.preparation_by_number(number, day)
        return operations.OperationResult(render_snapshot(snapshot), snapshot)
    if name == "notams":
        block = await operations.preparation_section(number, day, "notams")
        return operations.OperationResult(block.text, block)
    if name == "report":
        return await operations.bot_report(number, day)
    return await operations.foreflight(name, number, day)
