"""Fast ForeFlight API fields for Flight Card; never fetches briefing documents."""
import asyncio

from services.foreflight_operations import (
    ForeFlightClient,
    duration,
    find_flight,
    instant,
    normalized,
)
from services.snapshot_models import SourceBlock


async def load_fast_foreflight(flight):
    day = str(flight.get("departureUTC") or "")[:10]
    async with ForeFlightClient() as client:
        row, basis = await find_flight(client, flight["flightNumber"], day, flight)
        if (
            normalized(row.get("departure")) != normalized(flight.get("departure"))
            or normalized(row.get("destination")) != normalized(flight.get("arrival"))
            or normalized(row.get("aircraftRegistration"))
            != normalized(flight.get("aircraft"))
        ):
            raise ValueError("ForeFlight: маршрут или борт не совпадает с Leon.")
        a, b = instant(flight.get("departureUTC")), instant(row.get("departureTime"))
        if a is None or b is None or abs((b - a).total_seconds()) > 3600:
            raise ValueError("ForeFlight: время вылета не соответствует Leon.")
        detail, performance, overflight = await asyncio.gather(
            client.get_flight(row["flightId"]),
            client.get_performance(row["flightId"]),
            client.get_overflight(row["flightId"]),
            return_exceptions=True,
        )
    result = {}
    if isinstance(detail, dict):
        data = detail.get("flightData") or {}
        perf = (
            performance.get("performance", performance)
            if isinstance(performance, dict)
            else detail.get("performance") or {}
        )
        times = perf.get("times") or {}
        minutes = times.get("timeToDestinationMinutes")
        warning = bool(detail.get("errors") or perf.get("errors") or perf.get("warnings"))
        result["flighttime"] = SourceBlock(
            "ForeFlight API",
            "ETE " + duration(minutes),
            "partial" if warning else "ready",
            details={"eet_minutes": minutes, "basis": basis,
                     "has_ete": isinstance(minutes, (int, float))},
        )
        route = data.get("routeToDestination") or {}
        result["route"] = SourceBlock(
            "ForeFlight API",
            "RTE / ALTN\n"
            + "ROUTE: " + (route.get("route") or row.get("route") or "нет данных")
            + "\nALTN: " + (data.get("alternate") or "нет данных"),
            "partial" if warning else "ready",
            details={
                "route": route.get("route") or row.get("route"),
                "alternate": data.get("alternate"),
                "secondAlternate": data.get("secondAlternate"),
                "takeOffAlternate": data.get("takeOffAlternate"),
            },
        )
    else:
        result["flighttime"] = SourceBlock("ForeFlight API", "ETE: источник недоступен.")
    if isinstance(overflight, dict):
        result["overflight"] = SourceBlock(
            "ForeFlight API", "Overflights",
            "ready",
            details={"overflownCountries": overflight.get("overflownCountries")},
        )
        if "route" in result:
            result["route"].details["overflownCountries"] = overflight.get(
                "overflownCountries"
            )
    return result
