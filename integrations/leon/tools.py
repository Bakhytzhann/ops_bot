import json
from datetime import UTC, datetime, timedelta

from config.access import require_access
from integrations.leon.mcp_client import leon_mcp
from services.schedule_dates import SchedulePeriod


def parse_result(result):
    """Convert MCP content into Python data."""
    if result.structured_content is not None:
        return result.structured_content

    blocks = []
    for item in result.content:
        if getattr(item, "type", None) != "text":
            continue

        try:
            blocks.append(json.loads(item.text))
        except json.JSONDecodeError:
            blocks.append(item.text)

    if len(blocks) == 1:
        return blocks[0]

    return blocks


def compact(data, max_chars=12000):
    """Limit data sent back to the local LLM."""
    serialized = json.dumps(
        data,
        ensure_ascii=False,
        default=str,
    )

    if len(serialized) > max_chars:
        return {
            "truncated": True,
            "message": "Result exceeds response limit",
            "preview": serialized[:max_chars],
        }

    return data


async def call_leon(name: str, arguments: dict):
    result = await leon_mcp.call_tool(name, arguments)
    return parse_result(result)


# -------------------------------------------------
# FLIGHT SCHEDULE
# -------------------------------------------------


def make_flight_search_arguments(
    start: datetime,
    end: datetime,
    offset: int,
):
    return {
        "timeRange": {
            "fromDateTimeUTC": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "toDateTimeUTC": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
        "flightStatus": None,
        "flightNumber": None,
        "tripNumber": None,
        "adepLocationNid": None,
        "adesLocationNid": None,
        "icaoType": None,
        "flightType": None,
        "journeyLogCompletionStatus": None,
        "requestedByNid": None,
        "aocNidList": None,
        "tagNidList": None,
        "aircraftNidList": None,
        "crewNidList": None,
        "ferry": None,
        "cancelled": None,
        "departureCountry": None,
        "arrivalCountry": None,
        "offset": offset,
        "limit": 50,
    }


async def get_flight_schedule(date: str, end_date: str | None = None) -> dict:
    require_access()

    day = datetime.strptime(date, "%Y-%m-%d").date()
    last_day = datetime.strptime(end_date or date, "%Y-%m-%d").date()
    SchedulePeriod(day, last_day)

    start = datetime.combine(
        day,
        datetime.min.time(),
        tzinfo=UTC,
    )
    end = datetime.combine(
        last_day + timedelta(days=1),
        datetime.min.time(),
        tzinfo=UTC,
    )

    flights = []
    complete = False
    offset = 0

    # Maximum 10 pages / 500 flights per request.
    for _page in range(10):
        arguments = make_flight_search_arguments(
            start,
            end,
            offset=offset,
        )

        data = await call_leon(
            "search-flights",
            arguments,
        )

        if not isinstance(data, dict):
            raise TypeError("Unexpected Leon schedule response")

        batch = data.get("flightList")

        if not isinstance(batch, list):
            raise TypeError("Leon flightList missing")

        for flight in batch:
            if not isinstance(flight, dict):
                continue

            departure = flight.get("startAirport") or {}
            arrival = flight.get("endAirport") or {}
            aircraft = flight.get("acft") or {}

            flights.append(
                {
                    "flightNid": flight.get("flightNid"),
                    "flightNumber": flight.get("flightNo"),
                    "departure": ((departure.get("code") or {}).get("icao")),
                    "arrival": ((arrival.get("code") or {}).get("icao")),
                    "departureIATA": ((departure.get("code") or {}).get("iata")),
                    "arrivalIATA": ((arrival.get("code") or {}).get("iata")),
                    "departureUTC": flight.get("startTimeUTC"),
                    "arrivalUTC": flight.get("endTimeUTC"),
                    "departureLocal": flight.get("startTimeLocal"),
                    "arrivalLocal": flight.get("endTimeLocal"),
                    "aircraft": aircraft.get("registration"),
                    "aircraftType": (aircraft.get("acftType") or {}).get("icao")
                    or flight.get("icaoType"),
                    "status": flight.get("status"),
                    "cancelled": flight.get("isCnl"),
                }
            )

        has_next = data.get("shouldRerunRequestForNextPageWithOffset")

        if has_next is False or (has_next is None and len(batch) < 50):
            complete = True
            break

        if not batch:
            break
        offset += len(batch)

    return {
        "source": "Leon MCP",
        "date": date,
        "end_date": last_day.isoformat(),
        "timezone": "UTC",
        "count": len(flights),
        "complete": complete,
        "flights": flights,
    }


# -------------------------------------------------
# FLIGHT CREW
# -------------------------------------------------


async def get_flight_crew(flight_nid: int) -> dict:
    require_access()

    data = await call_leon(
        "get-flights-crew",
        {"flightNidList": [flight_nid]},
    )

    return {
        "source": "Leon MCP",
        "flightNid": flight_nid,
        "crew": data,
        "fetched_at": datetime.now(UTC).isoformat(),
    }
