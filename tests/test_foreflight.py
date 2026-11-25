import json
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr

from integrations.foreflight.api_client import ForeFlightClient, ForeFlightError

FLIGHT_ID = "11111111-1111-4111-8111-111111111111"


@pytest.fixture
def config():
    return SimpleNamespace(
        foreflight_api_key=SecretStr("test-only-key"),
        foreflight_vendor_id=SecretStr("22222222-2222-4222-8222-222222222222"),
        foreflight_api_url="",
        api_timeout=5,
    )


@pytest.mark.asyncio
async def test_both_credentials_sent_on_every_request(config):
    seen = []

    def respond(request):
        seen.append(request)
        assert request.method == "GET"
        assert request.url.host == "public-api.foreflight.com"
        assert (
            request.headers["x-api-key"] == config.foreflight_api_key.get_secret_value()
        )
        assert (
            request.headers["x-vendorId"]
            == config.foreflight_vendor_id.get_secret_value()
        )
        return httpx.Response(200, json={"ok": True})

    async with ForeFlightClient(
        config, transport=httpx.MockTransport(respond)
    ) as client:
        await client.api_key_info()
        await client.list_aircraft()
        start = datetime(2026, 11, 11, tzinfo=UTC)
        await client.list_flights(start, start + timedelta(days=1))
        await client.get_flight(FLIGHT_ID)
        await client.get_performance(FLIGHT_ID)
        await client.get_overflight(FLIGHT_ID)
        await client.get_document(FLIGHT_ID, "navlog")
        await client.list_saved_routes()
    assert len(seen) == 8
    assert seen[2].url.params["fromDate"] == "2026-11-11T00:00:00Z"
    assert seen[2].url.params["toDate"] == "2026-11-12T00:00:00Z"
    assert seen[6].url.path == f"/public/api/Flights/{FLIGHT_ID}/navlog"


@pytest.mark.parametrize(
    "url",
    [
        "http://public-api.foreflight.com",
        "https://example.com",
        "https://public-api.foreflight.com.evil.test",
        "https://public-api.foreflight.com:444",
        "https://public-api.foreflight.com/?target=elsewhere",
    ],
)
@pytest.mark.asyncio
async def test_credentials_never_sent_to_other_hosts(config, url):
    config.foreflight_api_url = url
    with pytest.raises(ValueError):
        async with ForeFlightClient(config):
            pytest.fail("Client must reject the URL before requests")


@pytest.mark.asyncio
async def test_redirect_is_not_followed(config):
    seen = []

    def redirect(request):
        seen.append(request)
        return httpx.Response(302, headers={"Location": "https://example.com"})

    async with ForeFlightClient(
        config, transport=httpx.MockTransport(redirect)
    ) as client:
        with pytest.raises(ForeFlightError) as error:
            await client.api_key_info()
    assert error.value.status_code == 302
    assert len(seen) == 1


@pytest.mark.parametrize("field", ["foreflight_vendor_id", "foreflight_api_key"])
@pytest.mark.asyncio
async def test_credentials_required(config, field):
    setattr(config, field, None)
    with pytest.raises(ForeFlightError):
        async with ForeFlightClient(config):
            pytest.fail("Must require both credentials")


@pytest.mark.asyncio
async def test_errors_do_not_echo_response_or_credentials(config):
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            401, text="test-only-key 22222222-2222-4222-8222-222222222222"
        )
    )
    async with ForeFlightClient(config, transport=transport) as client:
        with pytest.raises(ForeFlightError) as error:
            await client.api_key_info()
    assert str(error.value) == "ForeFlight returned HTTP 401"


@pytest.mark.asyncio
async def test_mutation_endpoints_blocked(config):
    async with ForeFlightClient(config) as client:
        for path in (
            "Flights/release",
            "../schedule/flights",
            "https://example.com",
            "contacts",
        ):
            with pytest.raises((ValueError, PermissionError)):
                await client._get(path)


@pytest.mark.asyncio
async def test_calculate_routed_performance_only(config):
    requests = []

    def respond(request):
        requests.append((request.method, request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"performance": {}})

    departure = datetime(2026, 11, 24, 15, tzinfo=timezone(timedelta(hours=5)))
    async with ForeFlightClient(
        config, transport=httpx.MockTransport(respond)
    ) as client:
        args = ("UAAA", "UACC", departure, "UP-TEST", 2)
        await client.calculate_route_performance(
            *args,
            " uaaa  DOGAR A231 PRISU uacc ",
            flight_level=390,
        )
        for route in ("DCT", "UAAA DCT UACC", "N0450F400 DCT"):
            with pytest.raises(ValueError, match="explicit enroute routing"):
                await client.calculate_route_performance(*args, route)

    assert requests == [
        (
            "POST",
            "/public/api/Flights/performance",
            {
                "flight": {
                    "departure": "UAAA",
                    "destination": "UACC",
                    "scheduledTimeOfDeparture": "2026-11-24T10:00:00Z",
                    "aircraftRegistration": "UP-TEST",
                    "routeToDestination": {
                        "route": "DOGAR A231 PRISU",
                        "altitude": {"altitude": 390, "unit": "FL"},
                    },
                    "load": {"passengers": [{"type": "Adult"}, {"type": "Adult"}]},
                    "windOptions": {"windModel": "Forecasted"},
                    "flightRule": "IFR",
                    "atcTypeOfFlight": "N",
                }
            },
        )
    ]


@pytest.mark.asyncio
async def test_create_autoroute_deletes_only_its_draft(config):
    requests = []

    def respond(request):
        requests.append((request.method, request.url.path))
        if request.method == "POST":
            flight = json.loads(request.content)["flight"]
            assert "crew" not in flight
            assert "routeToDestination" not in flight
            assert len(flight["tags"]) == 1
            return httpx.Response(200, json={"flight": {"flightId": FLIGHT_ID}})
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "flightData": {"routeToDestination": {"route": "DOGAR A231 PRISU"}}
                },
            )
        return httpx.Response(200, text="Flight deleted successfully")

    async with ForeFlightClient(
        config, transport=httpx.MockTransport(respond)
    ) as client:
        calculated = await client.calculate_autorouted_performance(
            "UAAA", "UACC", datetime(2026, 11, 30, tzinfo=UTC), "UP-TEST", 3
        )

    assert calculated["flightData"]["routeToDestination"]["route"] == "DOGAR A231 PRISU"
    assert requests == [
        ("POST", "/public/api/Flights"),
        ("GET", f"/public/api/Flights/{FLIGHT_ID}/performance"),
        ("DELETE", f"/public/api/Flights/{FLIGHT_ID}"),
    ]


@pytest.mark.asyncio
async def test_invalid_flight_query_blocked(config):
    async with ForeFlightClient(config) as client:
        with pytest.raises(ValueError):
            await client.get_flight("../release")
        with pytest.raises(ValueError):
            await client.get_document(FLIGHT_ID, "release")
        start = datetime(2026, 11, 11, tzinfo=UTC)
        with pytest.raises(ValueError):
            await client.list_flights(start, start - timedelta(days=1))
        with pytest.raises(ValueError):
            await client.list_flights(start.replace(tzinfo=None), start)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "flight_id",
    ["ABCDEFAB-1234-4567-89AB-ABCDEFABCDEF", "abcdefab1234456789ababcdefabcdef"],
)
async def test_foreflight_id_format_is_preserved(config, flight_id):
    seen = []

    def respond(request):
        seen.append(request.url.path)
        return httpx.Response(200, json={})

    async with ForeFlightClient(
        config, transport=httpx.MockTransport(respond)
    ) as client:
        await client.get_flight(flight_id)
        await client.get_document(flight_id, "briefing")
    assert seen == [
        f"/public/api/Flights/{flight_id}",
        f"/public/api/Flights/{flight_id}/briefing",
    ]
