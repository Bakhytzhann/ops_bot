import math
import re
from datetime import UTC, datetime, timedelta

from integrations.foreflight.api_client import (
    ForeFlightClient,
    ForeFlightDraftCleanupError,
    ForeFlightError,
    enroute_text,
)
from services.charter_routing import _candidates, _object, _routed_distance
from services.foreflight_operations import flight_rows
from services.snapshot_models import utc
from services.source_cache import SourceCache

POUND_TO_KG = 0.45359237
RANGE_WARNING = re.compile(
    r"insufficient (?:usable )?fuel|exceed(?:s|ed)? (?:the )?(?:usable fuel|"
    r"fuel capacity|max(?:imum)? (?:fuel|range))|outside (?:the )?aircraft range",
    re.IGNORECASE,
)
ARCHIVE_WINDOW = timedelta(days=30)
ARCHIVE_WINDOWS = 4
ARCHIVE_ROUTE_LIMIT = 3
_archive_cache = SourceCache(128)


def _number(value):
    if type(value) in (int, float) and math.isfinite(value) and value >= 0:
        return value
    return None


def _fuel_kg(fuel):
    burn = _number(fuel.get("flightFuel"))
    if not burn:
        flight = _number(fuel.get("fuelToDestination"))
        taxi = _number(fuel.get("taxiFuel"))
        burn = flight + taxi if flight and taxi is not None else None
    unit = str(fuel.get("unit") or "").upper()
    if burn and unit in {"KG", "KILOGRAM", "KILOGRAMS"}:
        return round(burn, 1)
    if burn and unit in {"LB", "LBS", "POUND", "POUNDS"}:
        return round(burn * POUND_TO_KG, 1)
    return None


def _provider_messages(response, performance, key):
    return list(
        dict.fromkeys(
            message.strip()
            for container in (response, performance)
            if isinstance(items := container.get(key), list)
            for item in items
            if isinstance(
                message := (
                    item if isinstance(item, str) else _object(item).get("message")
                ),
                str,
            )
            and message.strip()
        )
    )


def _weight_limits(weights):
    pairs = (
        ("rampWeight", "maxRampWeight", "ramp"),
        ("takeOffWeight", "maxTakeOffWeight", "takeoff"),
        ("zeroFuelWeight", "maxZeroFuelWeight", "zero-fuel"),
        ("landingWeight", "maxLandingWeight", "landing"),
    )
    return [
        f"ForeFlight {label} weight {actual:g} exceeds {limit:g} {weights.get('unit') or ''}.".strip()
        for actual_key, limit_key, label in pairs
        if (actual := _number(weights.get(actual_key))) is not None
        and (limit := _number(weights.get(limit_key)))
        and actual > limit
    ]


def route_performance(response, departure, arrival, expected_route):
    """Keep route-bound values. ForeFlight's totalTimeMinutes is fuel endurance."""
    response = _object(response)
    performance = _object(response.get("performance"))
    flight_data = _object(response.get("flightData"))
    route = enroute_text(
        _object(flight_data.get("routeToDestination")).get("route"), departure, arrival
    )
    distance = (
        _routed_distance(performance, route)
        if route and (expected_route is None or route == expected_route)
        else None
    )
    if distance is None:
        return None
    errors = _provider_messages(response, performance, "errors")
    warnings = _provider_messages(response, performance, "warnings")
    fuel = _object(performance.get("fuel"))
    required = _number(fuel.get("totalFuel"))
    capacity = _number(fuel.get("maxTotalFuel"))
    range_limit = bool(
        (required is not None and capacity and required > capacity)
        or any(RANGE_WARNING.search(message) for message in [*errors, *warnings])
    )
    if required is not None and capacity and required > capacity:
        errors.append(
            f"ForeFlight start fuel {required:g} exceeds usable fuel {capacity:g} "
            f"{fuel.get('unit') or ''}.".strip()
        )
    errors.extend(_weight_limits(_object(performance.get("weights"))))
    times = _object(performance.get("times"))
    flight = _number(times.get("timeToDestinationMinutes"))
    taxi = _number(times.get("taxiTimeMinutes"))
    return {
        "route": route,
        "distance_nm": distance,
        "airborne_minutes": flight or None,
        "block_minutes": math.ceil(flight + taxi)
        if flight and taxi is not None
        else None,
        "fuel_kg": _fuel_kg(fuel),
        "status": "range_limit"
        if range_limit
        else "blocked"
        if errors
        else "calculated",
        "issues": [*errors, *warnings],
    }


def _archived_flight(row, departure, arrival, start, end):
    if (row.get("departure"), row.get("destination")) != (departure, arrival):
        return False
    if not (when := utc(row.get("departureTime"))) or not start <= when < end:
        return False
    status = str(row.get("filingStatus") or "").casefold()
    if status:
        return status == "filed"
    logged = _object(row.get("flightLogTime"))
    off, on = utc(logged.get("timeOff")), utc(logged.get("timeOn"))
    return bool(off and on and off < on)


async def _flight_route(client, row, departure, arrival):
    if route := enroute_text(row.get("route"), departure, arrival):
        return route
    if row.get("route") or not row.get("flightId"):
        return None
    try:
        detail = _object(await client.get_flight(row["flightId"]))
    except ValueError:
        return None
    except ForeFlightError as exc:
        if exc.status_code in {400, 403, 404}:
            return None
        raise
    data = _object(detail.get("flightData"))
    if (data.get("departure"), data.get("destination")) == (departure, arrival):
        return enroute_text(
            _object(data.get("routeToDestination")).get("route"), departure, arrival
        )
    return None


async def _archived_candidates(client, departure, arrival, stamp):
    end = min(stamp, datetime.now(UTC)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )

    async def load():
        routes = {}
        upper = end
        for _ in range(ARCHIVE_WINDOWS):
            lower = upper - ARCHIVE_WINDOW
            try:
                rows = flight_rows(
                    await client.list_flights(
                        lower, upper, search=f"{departure} {arrival}"
                    )
                )
            except ForeFlightError as exc:
                if exc.status_code != 404:
                    raise
                rows = []
            matching = [
                row
                for row in rows
                if _archived_flight(row, departure, arrival, lower, upper)
            ]
            for row in sorted(
                matching, key=lambda row: utc(row["departureTime"]), reverse=True
            )[:6]:
                if route := await _flight_route(client, row, departure, arrival):
                    routes[route] = None
                if len(routes) == ARCHIVE_ROUTE_LIMIT:
                    break
            if routes:
                break
            upper = lower
        return tuple(routes.items())

    return await _archive_cache.get((departure, arrival, end.date()), 900, load)


def _best(results):
    return min(
        results,
        key=lambda row: (
            row["status"] != "calculated",
            row["airborne_minutes"] or math.inf,
            row["distance_nm"],
        ),
        default=None,
    )


async def charter_route_performance(
    departure,
    arrival,
    stamp,
    aircraft,
    passengers,
    *,
    saved_routes,
    allow_autoroute=False,
):
    candidates = _candidates(saved_routes, departure, arrival)
    async with ForeFlightClient() as client:

        async def calculate(route, flight_level, origin):
            try:
                result = await client.calculate_route_performance(
                    departure,
                    arrival,
                    stamp,
                    aircraft,
                    passengers,
                    route,
                    flight_level=flight_level,
                )
            except ForeFlightError as exc:
                if exc.status_code in {400, 404, 422}:
                    return None
                raise
            result = route_performance(result, departure, arrival, route)
            return {**result, "route_origin": origin} if result else None

        # Each alternative contributes an independent estimate for this same tail.
        # Prefer a usable company route, then the provider's documented constraint.
        valid = []
        evaluated = set()
        for route, level in sorted(candidates, key=lambda row: (row[0], row[1] or 0)):
            evaluated.add(route)
            if result := await calculate(route, level, "company_saved"):
                valid.append(result)
        if (best := _best(valid)) and best["status"] == "calculated":
            return best

        try:
            archived = await _archived_candidates(client, departure, arrival, stamp)
        except (ForeFlightError, ValueError):
            archived = ()
        for route, level in archived:
            if route not in evaluated and (
                result := await calculate(route, level, "foreflight_archive")
            ):
                valid.append(result)
        if (best := _best(valid)) and best["status"] == "calculated":
            return best

        if allow_autoroute:
            try:
                response = await client.calculate_autorouted_performance(
                    departure, arrival, stamp, aircraft, passengers
                )
            except ForeFlightDraftCleanupError as exc:
                return {
                    **(_best(valid) or {}),
                    "status": "route_pending",
                    "issues": [str(exc)],
                }
            except ForeFlightError as exc:
                if exc.status_code not in {400, 401, 403, 404, 422}:
                    raise
                response = None
            result = route_performance(response, departure, arrival, None)
            if result:
                valid.append({**result, "route_origin": "foreflight_autoroute"})
        return _best(valid)
