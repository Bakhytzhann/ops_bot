from datetime import datetime

from services.snapshot_models import utc

MONTHS = (
    "JAN",
    "FEB",
    "MAR",
    "APR",
    "MAY",
    "JUN",
    "JUL",
    "AUG",
    "SEP",
    "OCT",
    "NOV",
    "DEC",
)


def flight_stamp(flight, endpoint, preferences=None):
    local = (preferences or {}).get("time") == "LT"
    value = flight.get(endpoint + ("Local" if local else "UTC"))
    try:
        return datetime.fromisoformat(value) if local else utc(value)
    except (TypeError, ValueError):
        return None


def flight_date(flight, preferences=None):
    stamp = flight_stamp(flight, "departure", preferences)
    return f"{stamp.day:02d}{MONTHS[stamp.month - 1]}" if stamp else "нет данных"


def flight_reference(flight, preferences=None):
    return f"FLT {flight.get('flightNumber') or 'нет данных'} · {flight_date(flight, preferences)}"


def airport(flight, endpoint, preferences=None):
    preferences = preferences or {}
    icao = flight.get(endpoint)
    if preferences.get("codes") == "IATA":
        return flight.get(endpoint + "IATA") or (
            f"{icao} (ICAO)" if icao else "нет данных"
        )
    return icao or "нет данных"


def flight_time(flight, endpoint, preferences=None, *, full=False):
    local = (preferences or {}).get("time") == "LT"
    stamp = flight_stamp(flight, endpoint, preferences)
    if stamp is None:
        return "нет данных"
    return stamp.strftime("%d %b %Y %H%M" if full else "%H%M") + (
        " LT" if local else "z"
    )


def flight_header(flight, preferences=None):
    dep, arr = (
        flight_stamp(flight, endpoint, preferences)
        for endpoint in ("departure", "arrival")
    )
    day_offset = (arr.date() - dep.date()).days if dep and arr else 0
    arrival = flight_time(flight, "arrival", preferences)
    if day_offset:
        arrival += f" ({day_offset:+d}d)"
    return "\n".join(
        [
            flight_reference(flight, preferences),
            f"DEP/ARR {airport(flight, 'departure', preferences)} → {airport(flight, 'arrival', preferences)}",
            f"STD/STA {flight_time(flight, 'departure', preferences)} / {arrival}",
            f"A/C {flight.get('aircraftType') or 'нет данных'} · REG {flight.get('aircraft') or 'нет данных'}",
        ]
    )
