from datetime import UTC, datetime, timedelta

import pytest

from services import charter_performance
from services.source_cache import SourceCache


@pytest.mark.asyncio
async def test_archived_filed_route_is_cached_and_recalculated_for_each_tail(
    monkeypatch,
):
    now = datetime.now(UTC)
    flight_id = "f2033ef8-dbc1-4abe-b7ef-cb9468910ff0"
    calls = []
    estimated = []
    fetched = []

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def list_flights(self, start, end, *, search):
            calls.append((start, end, search))
            return {
                "flights": [
                    {
                        "departure": "OMDB",
                        "destination": "UAAA",
                        "route": "WRONG1",
                        "filingStatus": "Filed",
                        "departureTime": (end - timedelta(days=1)).isoformat(),
                    },
                    {
                        "departure": "UAAA",
                        "destination": "OMDB",
                        "route": "WRONG2",
                        "filingStatus": "Cancelled",
                        "departureTime": (end - timedelta(days=1)).isoformat(),
                    },
                    {
                        "departure": "UAAA",
                        "destination": "OMDB",
                        "route": "DCT",
                        "filingStatus": "Filed",
                        "departureTime": (end - timedelta(days=1)).isoformat(),
                    },
                    {
                        "departure": "UAAA",
                        "destination": "OMDB",
                        "route": None,
                        "filingStatus": "Filed",
                        "flightId": flight_id,
                        "departureTime": (end - timedelta(days=2)).isoformat(),
                    },
                ]
            }

        async def get_flight(self, identifier):
            fetched.append(identifier)
            return {
                "flightData": {
                    "departure": "UAAA",
                    "destination": "OMDB",
                    "routeToDestination": {"route": "UAAA ELMUZ A707 FAVSU OMDB"},
                }
            }

        async def calculate_route_performance(
            self, dep, arr, stamp, aircraft, pax, route, *, flight_level
        ):
            estimated.append((dep, arr, stamp, aircraft, pax, route))
            return {
                "flightData": {"routeToDestination": {"route": route}},
                "performance": {
                    "times": {"timeToDestinationMinutes": 200, "taxiTimeMinutes": 12},
                    "distances": {"destination": 1900, "gcdDestination": 1800},
                    "fuel": {"flightFuel": 4300, "unit": "kg"},
                },
            }

    monkeypatch.setattr(charter_performance, "ForeFlightClient", Client)
    monkeypatch.setattr(charter_performance, "_archive_cache", SourceCache())

    for tail in ("UP-A", "UP-B"):
        performance = await charter_performance.charter_route_performance(
            "UAAA", "OMDB", now, tail, 7, saved_routes=[]
        )
        assert performance["route_origin"] == "foreflight_archive"
        assert performance["block_minutes"] == 212
    assert len(calls) == 1
    assert calls[0][2] == "UAAA OMDB"
    assert fetched == [flight_id]
    assert estimated == [
        ("UAAA", "OMDB", now, tail, 7, "ELMUZ A707 FAVSU") for tail in ("UP-A", "UP-B")
    ]


def test_foreflight_structured_warning_remains_a_range_limit():
    result = charter_performance.route_performance(
        {
            "flightData": {"routeToDestination": {"route": "ELMUZ"}},
            "performance": {
                "distances": {"destination": 1900, "gcdDestination": 1800},
                "warnings": [{"message": "Insufficient usable fuel"}],
            },
        },
        "UAAA",
        "OMDB",
        "ELMUZ",
    )
    assert result["status"] == "range_limit"
    assert result["issues"] == ["Insufficient usable fuel"]
