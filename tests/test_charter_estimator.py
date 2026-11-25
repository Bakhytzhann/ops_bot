from datetime import UTC, datetime, timedelta

import pytest

from services.charter_estimator import (
    CharterEstimateError,
    charter_ferry_request,
    estimate_charter,
    validate_route,
)

STD = datetime(2026, 11, 24, 12, tzinfo=UTC)
TAIL = "UP-TEST"
OUTBOUND = {"route": "ABC DCT DEF", "distance_nm": 618.2}
INBOUND = {"route": "DEF A123 ABC", "distance_nm": 659.1}
FERRY = {"route": "XYZ DCT ABC", "distance_nm": 705.8}


class RouteHistory:
    def __init__(self, routes=None):
        self.routes = routes or {}
        self.calls = []

    def summary(self, start, end, *, departure, arrival, aircraft_type):
        route = (departure, arrival, aircraft_type)
        self.calls.append(route)
        return self.routes.get(route, {})


def leon_flight(nid, departure, arrival, start, end, *, ferry=None):
    return {
        "flightNid": nid,
        "flightNumber": f"TEST{nid}",
        "departure": departure,
        "arrival": arrival,
        "departureUTC": start.isoformat(),
        "arrivalUTC": end.isoformat(),
        "aircraft": TAIL,
        "aircraftType": "E35L",
        "ferry": ferry,
    }


def quote(**changes):
    inputs = {
        "departure": "UAAA",
        "arrival": "UACC",
        "departure_utc": STD,
        "aircraft": TAIL,
        "aircraft_type": "Embraer Legacy 650",
        "schedule": None,
        "passengers": 6,
        "history": RouteHistory(),
        "routes": {("UAAA", "UACC"): OUTBOUND, ("UACC", "UAAA"): INBOUND},
    }
    inputs.update(changes)
    return estimate_charter(**inputs)


def test_actual_route_metrics_and_reverse_leg_are_direction_specific():
    history = RouteHistory(
        {
            ("UAAA", "UACC", "E35L"): {
                "block_minutes": {"count": 4, "mean": 82.5},
                "fuel": {"KG": {"count": 3, "mean": 1369.9}},
            }
        }
    )

    result = quote(history=history, return_utc=STD + timedelta(hours=4), wind_kt=38)
    outbound, inbound = result["legs"]

    assert history.calls == [("UAAA", "UACC", "E35L"), ("UACC", "UAAA", "E35L")]
    assert outbound["route"] == OUTBOUND["route"]
    assert outbound["distance_nm"] == 618.2
    assert outbound["distance_km"] == 1144.9
    assert inbound["route"] == INBOUND["route"]
    assert inbound["distance_nm"] == 659.1
    assert inbound["block_minutes"] == 127
    assert outbound["block_minutes"] == 82
    assert outbound["fuel_kg"] == 1370
    assert outbound["block_basis"] == outbound["fuel_basis"] == "journey_log"
    assert (outbound["block_samples"], outbound["fuel_samples"]) == (4, 3)
    assert inbound["block_basis"] == inbound["fuel_basis"] == "isa_model"
    assert inbound["wind_kt"] == -38


def test_volume_fuel_history_preserves_block_data_and_uses_model_for_fuel():
    history = RouteHistory(
        {
            ("UAAA", "UACC", "E35L"): {
                "block_minutes": {"count": 3, "mean": 93},
                "fuel": {"L": {"count": 3, "mean": 1480}},
            }
        }
    )

    leg = quote(history=history, routes=None)["legs"][0]

    assert leg["block_minutes"] == 93
    assert leg["block_basis"] == "journey_log"
    assert leg["fuel_basis"] == "isa_model"
    assert leg["fuel_kg"] > 0
    assert leg["fuel_samples"] == 0


def test_missing_foreflight_route_returns_history_without_an_assumed_distance():
    history = RouteHistory(
        {
            ("UAAA", "UACC", "E35L"): {
                "block_minutes": {"count": 4, "mean": 82.5},
                "fuel": {"KG": {"count": 3, "mean": 1369.9}},
                "distance_nm": {"count": 4, "mean": 600},
            }
        }
    )

    outbound, inbound = quote(
        history=history, routes=None, return_utc=STD + timedelta(hours=3)
    )["legs"]

    assert outbound["route"] is inbound["route"] is None
    assert outbound["route_basis"] == inbound["route_basis"] == "unavailable"
    assert outbound["distance_nm"] is outbound["distance_km"] is None
    assert inbound["distance_nm"] is inbound["distance_km"] is None
    assert outbound["block_basis"] == outbound["fuel_basis"] == "journey_log"
    assert inbound["block_minutes"] is inbound["fuel_kg"] is None


@pytest.mark.parametrize(
    "routing",
    [
        {"route": "DCT", "distance_nm": 520},
        {"route": "UAAA DCT UACC", "distance_nm": 520},
        {"route": "ABC DCT DEF", "distance_nm": float("nan")},
        {"route": "ABC DCT DEF", "distance_nm": -1},
        {"route": "ABC DCT DEF", "distance_nm": True},
    ],
)
def test_direct_or_invalid_provider_route_cannot_feed_isa(routing):
    leg = quote(routes={("UAAA", "UACC"): routing})["legs"][0]

    assert leg["route"] is leg["distance_nm"] is None
    assert leg["block_minutes"] is leg["fuel_kg"] is None


def test_wind_and_temperature_adjust_only_modeled_legs():
    baseline = quote()["legs"][0]
    tailwind = quote(wind_kt=60)["legs"][0]
    warm = quote(isa_deviation_c=30)["legs"][0]

    assert tailwind["block_minutes"] < baseline["block_minutes"]
    assert tailwind["fuel_kg"] < baseline["fuel_kg"]
    assert warm["block_minutes"] < baseline["block_minutes"]


def test_positioning_leg_shows_schedule_conflict_and_next_flight():
    last = leon_flight(
        1, "UAII", "UACC", STD - timedelta(hours=2), STD - timedelta(minutes=10)
    )
    next_flight = leon_flight(
        2, "UACC", "UAII", STD + timedelta(hours=4), STD + timedelta(hours=5)
    )

    schedule = {"complete": True, "flights": [next_flight, last]}
    result = quote(
        schedule=schedule, ferry_route=FERRY, return_utc=STD + timedelta(hours=8)
    )
    position = result["position"]

    assert charter_ferry_request(schedule, TAIL, "UAAA", "UACC", STD) == (
        "UACC",
        "UAAA",
        STD,
    )
    assert position["status"] == "ferry_required"
    assert position["station"] == "UACC"
    assert position["ferry"]["departure"] == "UACC"
    assert position["ferry"]["arrival"] == "UAAA"
    assert position["ferry"]["arrival_utc"] == STD.isoformat()
    assert position["ferry"]["route"] == FERRY["route"]
    assert position["ferry"]["distance_nm"] == FERRY["distance_nm"]
    assert result["legs"][1]["distance_nm"] == INBOUND["distance_nm"]
    assert position["scheduled_ferry"] is None
    assert position["schedule_conflict"]["flight_id"] == 1
    assert position["next_flight"]["flight_id"] == 2


def test_positioning_without_provider_keeps_requirement_without_an_assumed_duration():
    last = leon_flight(
        1, "UAII", "UACC", STD - timedelta(hours=2), STD - timedelta(minutes=10)
    )
    schedule = {"complete": True, "flights": [last]}
    position = quote(schedule=schedule)["position"]

    assert position["status"] == "ferry_required"
    assert position["ferry"]["route"] is None
    assert position["ferry"]["block_minutes"] is None
    assert position["ferry"]["departure_utc"] is None
    assert (
        charter_ferry_request(
            {**schedule, "complete": False}, TAIL, "UAAA", "UACC", STD
        )
        is None
    )


def test_matching_confirmed_ferry_is_shown_without_a_schedule_conflict():
    ferry = leon_flight(1, "UAAA", "UACC", STD, STD + timedelta(hours=2), ferry=True)

    schedule = {"complete": True, "flights": [ferry]}
    position = quote(schedule=schedule)["position"]

    assert charter_ferry_request(schedule, TAIL, "UAAA", "UACC", STD) is None
    assert position["station"] == "UAAA"
    assert position["scheduled_ferry"]["flight_id"] == 1
    assert position["schedule_conflict"] is None


def test_unsupported_model_and_incomplete_schedule_are_explicitly_unknown():
    last = leon_flight(
        1, "UACC", "UAAA", STD - timedelta(hours=2), STD - timedelta(minutes=30)
    )
    last["aircraftType"] = "H25B"

    result = quote(aircraft_type=None, schedule={"complete": False, "flights": [last]})
    leg, position = result["legs"][0], result["position"]

    assert leg["distance_nm"] > 0
    assert leg["block_minutes"] is None and leg["fuel_kg"] is None
    assert leg["block_basis"] == leg["fuel_basis"] == "unavailable"
    assert not position["schedule_complete"]
    assert position["status"] == "unknown" and position["station"] is None
    assert position["last_flight"]["flight_id"] == 1


def test_partial_round_trip_coverage_keeps_a_known_outbound_position():
    last = leon_flight(
        1, "UACC", "UAAA", STD - timedelta(hours=2), STD - timedelta(minutes=30)
    )
    result = quote(
        return_utc=STD + timedelta(days=10),
        schedule={"complete": False, "position_complete": True, "flights": [last]},
    )

    assert result["position"]["status"] == "at_departure"
    assert not result["position"]["schedule_complete"]


def test_incomplete_timing_for_selected_tail_prevents_position_assumption():
    last = leon_flight(
        1, "UACC", "UAAA", STD - timedelta(hours=2), STD - timedelta(minutes=30)
    )
    undated = {**last, "flightNid": 2, "arrivalUTC": None}
    result = quote(schedule={"complete": True, "flights": [last, undated]})

    assert result["position"]["status"] == "unknown"
    assert not result["position"]["schedule_complete"]


def test_discontinuous_leon_legs_cannot_establish_aircraft_position():
    last = leon_flight(
        1, "UACC", "UAAA", STD - timedelta(hours=2), STD - timedelta(minutes=30)
    )
    upcoming = leon_flight(
        2, "UACC", "UAAA", STD + timedelta(hours=4), STD + timedelta(hours=5)
    )

    position = quote(schedule={"complete": True, "flights": [last, upcoming]})[
        "position"
    ]

    assert position["status"] == "unknown"
    assert position["station"] is None
    assert position["next_flight"]["flight_id"] == 2


@pytest.mark.parametrize(
    ("departure", "arrival", "message"),
    [("UAAA", "UAAA", "must differ"), ("UAAA", "0000", "Unknown airport ICAO code")],
)
def test_rejects_invalid_routes(departure, arrival, message):
    with pytest.raises(CharterEstimateError, match=message):
        validate_route(departure, arrival)


def test_rejects_return_overlapping_the_outbound_estimate():
    with pytest.raises(CharterEstimateError, match="overlaps"):
        quote(return_utc=STD + timedelta(minutes=10))
