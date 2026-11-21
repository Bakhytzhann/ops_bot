import json
from datetime import UTC, datetime, timedelta

from httpx import HTTPError

from integrations.leon.mcp_client import leon_mcp
from services.schedule_dates import SchedulePeriod

DETAIL_BATCH_SIZE = 50
JOURNEY_FIELDS = (
    "acftTypeId",
    "atd",
    "ata",
    "startTime",
    "endTime",
    "bloffUTC",
    "blonUTC",
    "toTimeUTC",
    "ldgTimeUTC",
    "blockFuel",
    "usedFuel",
    "remainingFuel",
    "taxiFuel",
    "refuel",
    "fuelUnit",
    "refuelUnit",
    "distance",
    "landingCount",
    "isCnl",
    "isClosed",
)


def parse_result(result):
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


def make_flight_search_arguments(
    start: datetime,
    end: datetime,
    offset: int,
    ferry: bool | None = None,
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
        "ferry": ferry,
        "cancelled": None,
        "departureCountry": None,
        "arrivalCountry": None,
        "offset": offset,
        "limit": 50,
    }


async def get_flight_schedule(
    date: str,
    end_date: str | None = None,
    *,
    ferry: bool | None = None,
    _include_journey_logs: bool = False,
) -> dict:

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

    for _page in range(10):
        arguments = make_flight_search_arguments(
            start,
            end,
            offset=offset,
            ferry=ferry,
        )

        data = await call_leon(
            "search-flights",
            arguments,
        )

        if not isinstance(data, dict):
            raise TypeError("Unexpected Leon schedule response")

        batch = data.get("flightList", data.get("flightsList"))

        if not isinstance(batch, list):
            raise TypeError("Leon flightList missing")

        for flight in batch:
            if not isinstance(flight, dict):
                continue

            departure = flight.get("startAirport") or {}
            arrival = flight.get("endAirport") or {}
            aircraft = flight.get("acft") or {}
            trip = flight.get("trip")
            trip = trip if isinstance(trip, dict) else {}

            flights.append(
                {
                    "flightNid": flight.get("flightNid"),
                    "flightNumber": flight.get("flightNo"),
                    "tripNid": trip.get("tripNid"),
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
                    "ferry": (
                        ferry
                        if type(ferry) is bool
                        else flight.get("isFerry")
                        if type(flight.get("isFerry")) is bool
                        else flight.get("ferry")
                        if type(flight.get("ferry")) is bool
                        else None
                    ),
                }
            )
            if _include_journey_logs:
                _attach_journey_log(flights[-1], flight)

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


def _attach_journey_log(flight: dict, source: dict) -> None:
    if "journeyLog" not in source:
        return
    log = source["journeyLog"]
    if log is None or isinstance(log, str):
        flight["journeyLog"] = None
        if isinstance(log, str):
            flight["journeyLogCancelled"] = True
        return
    if not isinstance(log, dict):
        return

    fields = {
        key: value
        for key in JOURNEY_FIELDS
        if (value := log.get(key)) is not None
        and isinstance(value, (str, int, float, bool))
    }
    for key in ("startAirport", "endAirport"):
        airport = log.get(key)
        if not isinstance(airport, dict):
            continue
        code = airport.get("code")
        code = code if isinstance(code, dict) else airport
        fields[key] = {
            "code": {
                name: value
                for name in ("icao", "iata")
                if isinstance(value := code.get(name), str)
            },
            "country": airport.get("country")
            if isinstance(airport.get("country"), str)
            else None,
        }
    aircraft = log.get("acft")
    if isinstance(aircraft, dict):
        registration = aircraft.get("registration")
        aircraft_type = aircraft.get("acftType")
        fields["acft"] = {
            "registration": registration if isinstance(registration, str) else None,
            "acftType": {
                "icao": aircraft_type["icao"]
                if isinstance(aircraft_type, dict)
                and isinstance(aircraft_type.get("icao"), str)
                else None,
            },
        }
    if fields:
        flight["journeyLog"] = fields


async def get_flight_history(start_date: str, end_date: str) -> dict:
    schedule = await get_flight_schedule(
        start_date, end_date, _include_journey_logs=True
    )
    missing = {}
    for flight in schedule["flights"]:
        if "journeyLog" in flight:
            continue
        nid = flight["flightNid"]
        if type(nid) is int and nid > 0:
            missing.setdefault(nid, []).append(flight)

    nids = list(missing)
    for offset in range(0, len(nids), DETAIL_BATCH_SIZE):
        batch = nids[offset : offset + DETAIL_BATCH_SIZE]
        try:
            data = await call_leon("get-flights-by-flight-nids", {"flightNids": batch})
        except (HTTPError, OSError, RuntimeError, TimeoutError):
            break
        if isinstance(data, dict):
            if "flightNid" in data:
                data = [data]
            else:
                data = data.get(
                    "result", data.get("flightList", data.get("flightsList"))
                )
        if not isinstance(data, list) or any(
            not isinstance(row, dict) or row.get("error") or row.get("errors")
            for row in data
        ):
            break

        detail_by_nid = {
            row["flightNid"]: row for row in data if type(row.get("flightNid")) is int
        }
        for nid in batch:
            if nid not in detail_by_nid:
                continue
            for flight in missing[nid]:
                _attach_journey_log(flight, detail_by_nid[nid])

    schedule["schedule_complete"] = schedule["complete"]
    schedule["complete"] = schedule["complete"] and all(
        "journeyLog" in flight for flight in schedule["flights"]
    )
    return schedule


async def get_flight_crew(flight_nid: int) -> dict:

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
