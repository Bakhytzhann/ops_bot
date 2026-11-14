"""Direct routes for operational requests, independent of LLM tool selection."""

import asyncio
import re
from datetime import UTC, datetime

from config.access import require_access
from integrations.awc.client import get_weather
from integrations.leon.operations import (
    get_crew_operations,
    get_flight_watch,
    get_flight_weather_leon,
    resolve_flight,
)
from services.operational_format import (
    format_awc_weather,
    format_crew_operations,
    format_watch,
)
from services.schedule_dates import parse_schedule_period
from services.weather_reports import awc_reports, format_reports, leon_reports

FLIGHT_PATTERN = re.compile(r"\b[A-Z]{2,3}\s*-?\s*\d{1,6}[A-Z]?\b", re.IGNORECASE)
WEATHER_PATTERN = re.compile(r"погод|metar|taf|метар|таф|\bawc\b", re.IGNORECASE)
WATCH_PATTERN = re.compile(
    r"flight\s*watch|флайт\s*в[оа]тч|флайтв[оа]тч|фактическ|\b(?:ctot|etd|eta|tobt|tsat)\b",
    re.IGNORECASE,
)
CREW_PATTERN = re.compile(
    r"\bftl\b|фтл|график(?!\s+(?:рейс|пол[её]т))|(?:roster|дежурств)", re.IGNORECASE
)


async def flight_weather(number: str, date: str, include_taf: bool = False) -> dict:
    require_access()
    flight = await resolve_flight(number, date)
    return await selected_flight_weather(flight, date, include_taf)


async def selected_flight_weather(
    flight: dict, date: str, include_taf: bool = False
) -> dict:
    """Use a verified exact flight NID; identical flight numbers may have many legs."""
    require_access()
    if type(flight.get("flightNid")) is not int or flight["flightNid"] <= 0:
        raise ValueError("Некорректный идентификатор выбранного рейса.")
    airports = [flight.get("departure"), flight.get("arrival")]
    codes = [c for c in airports if isinstance(c, str) and re.fullmatch(r"[A-Z]{4}", c)]

    async def awc():
        if not codes:
            raise ValueError("Leon не предоставил ICAO-коды маршрута для AWC")
        return await get_weather(codes, include_taf=include_taf)

    results = await asyncio.gather(
        get_flight_weather_leon(flight), awc(), return_exceptions=True
    )
    lines = [
        f"Погода по рейсу {flight['flightNumber']}: {flight['departure']} → {flight['arrival']}",
        f"Дата рейса: {date}. Ниже последние доступные сводки, не архив на дату рейса.",
    ]
    reports, errors = [], []
    for source, result in zip(("Leon MCP", "AWC"), results, strict=True):
        if isinstance(result, Exception):
            errors.append(f"{source}: ошибка получения ({type(result).__name__}).")
        else:
            if source == "Leon MCP":
                reports.extend(leon_reports(result, include_taf))
            else:
                awc_rows, awc_errors = awc_reports(result, include_taf)
                reports.extend(awc_rows)
                errors.extend(awc_errors)
    lines.append(format_reports(reports, errors))
    return {
        "answer": "\n\n".join(lines),
        "partial": bool(errors) or not any(r.get("raw") for r in reports),
        "reports": reports,
        "errors": errors,
        "fetched_at": datetime.now(UTC).isoformat(),
    }


def _crew_query(text):
    quoted = re.search(r'[«"]([^»"]+)[»"]', text)
    if quoted:
        return quoted.group(1).strip()
    explicit = re.search(r"\bкод\s+([A-Z0-9]{3,12})\b", text, re.IGNORECASE)
    if explicit:
        return explicit.group(1)
    text = re.sub(
        r"^.*?(?:ftl|фтл|roster|график|дежурств\w*)\s*",
        "",
        text,
        count=1,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"^(?:(?:экипажа|экипажей|пилота|сотрудника|для|нарушения|нарушений|отчет|отчёт)\s+)+",
        "",
        text,
        flags=re.IGNORECASE,
    )
    return re.split(
        r"\s+(?:с|по|за|на|сегодня|завтра|послезавтра)\b|\s+(?=20\d{2}-|\d{1,2}[./]|\d{1,2}\s)",
        text,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0].strip(" :,")


async def route_operation(question: str) -> str | None:
    weather = bool(WEATHER_PATTERN.search(question))
    watch = bool(WATCH_PATTERN.search(question))
    crew = bool(CREW_PATTERN.search(question))
    if not (weather or watch or crew):
        return None
    require_access()
    if weather or watch:
        include_taf = bool(re.search(r"\b(?:taf|таф)\b", question, re.IGNORECASE))
        match = FLIGHT_PATTERN.search(question)
        if not match:
            if weather:
                codes = re.findall(r"\b[A-Z]{4}\b", question.upper())
                codes = [c for c in codes if c not in {"TAFS"}]
                if codes:
                    return format_awc_weather(
                        await get_weather(codes, include_taf=include_taf), include_taf
                    )
            return "Укажите номер рейса и дату, например: Flight Watch ABC1265 на 2026-11-11."
        period = parse_schedule_period(question)
        if not period or period.start != period.end:
            return "Для рейса укажите одну дату (UTC), например 2026-11-11."
        number = re.sub(r"[\s-]", "", match.group()).upper()
        day = period.start.isoformat()
        parts = []
        if watch:
            parts.append(format_watch(await get_flight_watch(number, day)))
        if weather:
            parts.append((await flight_weather(number, day, include_taf))["answer"])
        return "\n\n".join(parts)

    period = parse_schedule_period(question)
    if not period:
        return "Укажите сотрудника и период: график «Иванов» с 16 по 30 ноября 2026; FTL код JKO на 2026-11-11."
    query = _crew_query(question)
    if not query:
        return "Укажите код или имя сотрудника, например: FTL «Иванов» на 2026-11-11."
    mode = (
        "violations"
        if re.search(r"наруш|violation", question, re.IGNORECASE)
        else "ftl"
        if re.search(r"\bftl\b|фтл", question, re.IGNORECASE)
        else "roster"
    )
    return format_crew_operations(
        await get_crew_operations(
            query, period.start.isoformat(), period.end.isoformat(), mode
        )
    )
