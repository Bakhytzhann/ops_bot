"""ForeFlight snapshot components with independently reported errors."""

import asyncio
from urllib.parse import urlsplit

from integrations.foreflight.briefing import download_briefing
from services.briefing_notams import counts, extract_notams
from services.foreflight_operations import (
    ForeFlightClient,
    duration,
    find_flight,
    instant,
    normalized,
)
from services.snapshot_models import SourceBlock, zulu


async def load_foreflight(flight, partial=None):
    day = flight["departureUTC"][:10]
    async with ForeFlightClient() as client:
        row, basis = await find_flight(client, flight["flightNumber"], day, flight)
        # A callsign alone must not silently attach another leg's briefing.
        for left, right in (
            (flight.get("departure"), row.get("departure")),
            (flight.get("arrival"), row.get("destination")),
        ):
            if not left or left != right:
                raise ValueError("ForeFlight route does not match selected Leon leg")
        departure = instant(flight.get("departureUTC"))
        ff_departure = instant(row.get("departureTime"))
        if not departure or not ff_departure:
            raise ValueError("ForeFlight departure does not match selected Leon leg")
        if not normalized(flight.get("aircraft")) or normalized(
            flight.get("aircraft")
        ) != normalized(row.get("aircraftRegistration")):
            raise ValueError("ForeFlight aircraft does not match selected Leon leg")
        difference = (ff_departure - departure).total_seconds() / 60
        match_warning = (
            f"Расхождение вылета Leon ↔ ForeFlight: {difference:+g} мин. Проверьте версии плана и брифинга."
            if difference
            else ""
        )
        detail, document, overflight = await asyncio.gather(
            client.get_flight(row["flightId"]),
            client.get_document(row["flightId"], "briefing"),
            client.get_overflight(row["flightId"]),
            return_exceptions=True,
        )
    briefing_url = document.get("url") if isinstance(document, dict) else None
    try:
        parsed = urlsplit(briefing_url) if isinstance(briefing_url, str) else None
        if (
            not parsed
            or parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or any(c.isspace() for c in briefing_url)
        ):
            briefing_url = None
    except ValueError:
        briefing_url = None
    blocks = {
        "briefing": SourceBlock(
            "ForeFlight",
            "Briefing",
            "ready" if briefing_url else "unavailable",
            details={
                "url": briefing_url,
                "generated": document.get("timeGenerated")
                if isinstance(document, dict)
                else None,
            },
        )
    }
    if isinstance(detail, dict):
        data = detail.get("flightData") or {}
        performance = detail.get("performance") or {}
        times = performance.get("times") or {}
        eet = duration(times.get("timeToDestinationMinutes")).split(" (")[0]
        has_ete = eet != "нет данных"
        lines = ["EET " + eet]
        errors = bool(
            detail.get("errors")
            or performance.get("errors")
            or performance.get("warnings")
        )
        if errors:
            lines.append("Расчёт содержит ошибки/предупреждения; проверьте Dispatch.")
        if match_warning:
            lines.append(match_warning)
        blocks["flighttime"] = SourceBlock(
            "ForeFlight",
            "\n".join(lines),
            "partial" if errors or match_warning or not has_ete else "ready",
            details={
                "basis": basis,
                "has_ete": has_ete,
                "eet": eet,
                "eet_minutes": times.get("timeToDestinationMinutes"),
            },
        )
        route = data.get("routeToDestination") or {}
        route_text = route.get("route") or row.get("route")
        blocks["route"] = SourceBlock(
            "ForeFlight",
            "\n".join(
                [
                    "ROUTE / ALTN · ForeFlight",
                    "ROUTE: " + (route_text or "нет данных"),
                    "ALTN: " + (data.get("alternate") or "нет данных"),
                    "ALTN 2: " + (data.get("secondAlternate") or "нет данных"),
                    "Take-off ALTN: " + (data.get("takeOffAlternate") or "нет данных"),
                    *([match_warning] if match_warning else []),
                ]
            ),
            "ready" if route_text and not errors and not match_warning else "partial",
            details={
                "route": route_text,
                "overflownCountries": overflight.get("overflownCountries")
                if isinstance(overflight, dict)
                else None,
                "alternate": data.get("alternate"),
                "secondAlternate": data.get("secondAlternate"),
                "takeOffAlternate": data.get("takeOffAlternate"),
            },
        )
    else:
        blocks["flighttime"] = SourceBlock(
            "ForeFlight", "Полётное время: источник недоступен."
        )
    if partial is not None:
        partial.update(blocks)
    try:
        if not isinstance(document, dict):
            raise TypeError("Briefing unavailable")
        text = await download_briefing(document["url"])
        stations = list(dict.fromkeys([flight["departure"], flight["arrival"]]))
        records, recognized = extract_notams(text, stations)
        block = SourceBlock(
            "ForeFlight briefing",
            status="partial",
            details={
                "notams": records,
                "recognized": recognized,
                "generated": document.get("timeGenerated"),
            },
        )
        lines = [
            "NOTAM · брифинг ForeFlight от " + zulu(document.get("timeGenerated")),
            "Список отфильтрован под рейс; полнота NOW и последующие отмены не проверены.",
        ]
        if match_warning:
            lines.append(match_warning)
        for station in stations:
            if station not in recognized:
                lines.append(station + ": раздел не распознан")
            else:
                rows = records[station]
                lines.append(
                    f"{station}: извлечено {len(rows)}; NOW в брифинге {counts(rows, block.fetched_at, block.fetched_at)}; период рейса {counts(rows, instant(flight.get('departureUTC')), instant(flight.get('arrivalUTC')))}"
                )
        block.text = "\n".join(lines)
        blocks["notams"] = block
    except Exception as exc:  # noqa: BLE001 -- partial source boundary; no signed URLs in errors
        blocks["notams"] = SourceBlock(
            "ForeFlight briefing",
            f"NOTAM: извлечение недоступно ({type(exc).__name__}); откройте /briefing.",
        )
    return blocks
