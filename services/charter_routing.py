import asyncio
import math

from integrations.foreflight.api_client import (
    ForeFlightClient,
    ForeFlightError,
    enroute_text,
)


async def load_saved_charter_routes():
    async with ForeFlightClient() as client:
        response = await client.list_saved_routes()
    if not isinstance(response, dict) or "routes" not in response:
        raise ForeFlightError("ForeFlight returned an unexpected saved routes response")
    routes = response["routes"]
    if routes is None:
        return []
    if not isinstance(routes, list) or not all(isinstance(row, dict) for row in routes):
        raise ForeFlightError("ForeFlight returned an unexpected saved routes response")
    return routes


def _candidates(routes, departure, arrival):
    candidates = set()
    for row in routes:
        if (
            row.get("departure") != departure
            or row.get("destination") != arrival
            or row.get("designation") not in (None, "Primary")
        ):
            continue
        route = enroute_text(row.get("route"), departure, arrival)
        if route is not None:
            level = row.get("flightLevel")
            candidates.add((route, level if type(level) is int and level > 0 else None))
    return candidates


def _object(value):
    return value if isinstance(value, dict) else {}


def _positive_number(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def _routed_distance(performance, route):
    distances = _object(performance.get("distances"))
    distance = distances.get("destination")
    if not _positive_number(distance):
        return None
    gcd = distances.get("gcdDestination")
    points = _object(performance.get("destinationRouteInformation")).get("waypoints")
    if isinstance(points, list):
        if len(points) < 3:
            return None
        identifiers = {_object(point).get("identifier") for point in points[1:-1]}
        if identifiers.intersection(token.split("/")[0] for token in route.split()):
            return distance
    # An echoed route can accompany a direct calculation. Require corroboration
    # from the routed distance when the provider supplied no matching waypoint.
    if _positive_number(gcd) and distance > gcd + 0.1:
        return distance
    return None


async def find_charter_route(
    departure,
    arrival,
    departure_utc,
    aircraft,
    passengers,
    *,
    saved_routes,
):
    """Compare matching company routes for this departure and aircraft."""
    candidates = _candidates(saved_routes, departure, arrival)
    if not candidates:
        return None

    slots = asyncio.Semaphore(3)
    async with ForeFlightClient() as client:

        async def evaluate(route, flight_level):
            try:
                async with slots:
                    response = await client.calculate_route_performance(
                        departure,
                        arrival,
                        departure_utc,
                        aircraft,
                        passengers,
                        route,
                        flight_level=flight_level,
                    )
            except ForeFlightError as exc:
                if exc.status_code in {400, 404, 422}:
                    return None
                raise
            response = _object(response)
            performance = _object(response.get("performance"))
            if response.get("errors") or performance.get("errors"):
                return None
            flight_data = _object(response.get("flightData"))
            returned_route = _object(flight_data.get("routeToDestination")).get("route")
            if enroute_text(returned_route, departure, arrival) != route:
                return None
            ete = _object(performance.get("times")).get("timeToDestinationMinutes")
            distance = _routed_distance(performance, route)
            if _positive_number(ete) and distance is not None:
                return ete, distance, route
            return None

        results = await asyncio.gather(
            *(evaluate(route, level) for route, level in candidates),
            return_exceptions=True,
        )

    valid = [result for result in results if isinstance(result, tuple)]
    if valid:
        _, distance, route = min(valid)
        return {"route": route, "distance_nm": distance}
    if error := next((item for item in results if isinstance(item, Exception)), None):
        raise error
    return None
