import json
from datetime import UTC, datetime

import httpx
import pytest

from integrations.foreflight.api_client import ForeFlightClient, ForeFlightError
from services import charter_routing
from tests.test_foreflight import config as config

DEPARTURE = datetime(2026, 11, 24, 10, tzinfo=UTC)


def saved(route, **updates):
    return {
        "departure": "UAAA",
        "destination": "UACC",
        "route": route,
        "designation": "Primary",
        **updates,
    }


def performance(route, ete, distance, *, waypoints=None, gcd=430):
    return {
        "flightData": {"routeToDestination": {"route": route}},
        "performance": {
            "times": {"timeToDestinationMinutes": ete},
            "distances": {"destination": distance, "gcdDestination": gcd},
            "destinationRouteInformation": {"waypoints": waypoints},
        },
    }


def intercept(monkeypatch, config, respond):
    monkeypatch.setattr(
        charter_routing,
        "ForeFlightClient",
        lambda: ForeFlightClient(config, transport=httpx.MockTransport(respond)),
    )


@pytest.mark.asyncio
async def test_compares_only_valid_directional_saved_routes(monkeypatch, config):
    requested = []
    routes = [
        saved("FIRTA A123 SENUP", flightLevel=390),
        saved("FIRTA A123 SENUP", flightLevel=390, objectId="duplicate"),
        saved("FORVA W45 RIVTE"),
        saved("BADFIX"),
        saved("DCT"),
        saved("IGNORE", designation="Partial"),
        saved("IGNORE", designation="Alternate"),
        saved("IGNORE", designation="Escape"),
        saved("REVERSE", departure="UACC", destination="UAAA"),
    ]

    def respond(request):
        if request.method == "GET":
            assert request.url.path == "/public/api/savedroutes"
            return httpx.Response(200, json={"routes": routes})
        assert request.url.path == "/public/api/Flights/performance"
        flight = json.loads(request.content)["flight"]
        route = flight["routeToDestination"]["route"]
        requested.append(route)
        assert flight["aircraftRegistration"] == "UP-TEST"
        assert flight["scheduledTimeOfDeparture"] == "2026-11-24T10:00:00Z"
        assert flight["load"]["passengers"] == [{"type": "Adult"}] * 3
        assert flight["windOptions"] == {"windModel": "Forecasted"}
        if route == "BADFIX":
            return httpx.Response(422)
        if route == "FIRTA A123 SENUP":
            assert flight["routeToDestination"]["altitude"] == {
                "altitude": 390,
                "unit": "FL",
            }
            return httpx.Response(200, json=performance(route, 81, 456))
        return httpx.Response(200, json=performance(route, 77, 480))

    intercept(monkeypatch, config, respond)
    saved_routes = await charter_routing.load_saved_charter_routes()
    fastest = await charter_routing.find_charter_route(
        "UAAA", "UACC", DEPARTURE, "UP-TEST", 3, saved_routes=saved_routes
    )
    assert sorted(requested) == ["BADFIX", "FIRTA A123 SENUP", "FORVA W45 RIVTE"]
    assert fastest == {"route": "FORVA W45 RIVTE", "distance_nm": 480}


@pytest.mark.asyncio
async def test_ignores_direct_fallback_and_mismatched_echo(monkeypatch, config):
    def respond(request):
        flight = json.loads(request.content)["flight"]
        assert flight["load"] == {"passengers": []}
        route = flight["routeToDestination"]["route"]
        if route == "GCD":
            return httpx.Response(
                200,
                json=performance(
                    route,
                    61,
                    430,
                    waypoints=[{"identifier": "UAAA"}, {"identifier": "UACC"}],
                ),
            )
        return httpx.Response(200, json=performance("DCT", 60, 480))

    intercept(monkeypatch, config, respond)
    result = await charter_routing.find_charter_route(
        "UAAA",
        "UACC",
        DEPARTURE,
        "UP-TEST",
        0,
        saved_routes=[saved("GCD"), saved("WPR")],
    )
    assert result is None


@pytest.mark.asyncio
async def test_never_requests_performance_without_a_matching_route(monkeypatch, config):
    intercept(monkeypatch, config, lambda _: pytest.fail("Unexpected request"))
    assert (
        await charter_routing.find_charter_route(
            "UACC", "UAAA", DEPARTURE, "UP-TEST", 4, saved_routes=[saved("FORVA W45")]
        )
        is None
    )


@pytest.mark.asyncio
async def test_reports_account_error_when_no_candidate_can_be_calculated(
    monkeypatch, config
):
    intercept(monkeypatch, config, lambda _: httpx.Response(401))
    with pytest.raises(ForeFlightError, match="HTTP 401"):
        await charter_routing.find_charter_route(
            "UAAA", "UACC", DEPARTURE, "UP-TEST", 4, saved_routes=[saved("FORVA W45")]
        )
