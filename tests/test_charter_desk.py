from datetime import UTC, datetime

import pytest

from services.charter_crew_operations import CrewOperations, roster_crew_report
from services.charter_desk import build_charter_matrix, charter_opportunities
from services.charter_performance import route_performance

NOW = datetime(2026, 11, 24, 5, tzinfo=UTC)


def stamp(day, clock):
    return f"2026-11-{day:02}T{clock}:00+00:00"


def sector(dep, arr, start, end, **values):
    return dict(
        departure=dep, arrival=arr, departure_utc=start, arrival_utc=end, **values
    )


def duty(station, start, end, **values):
    return (
        dict(
            station=station,
            available_from_utc=start,
            available_until_utc=end,
            report_lead_minutes=30,
            post_duty_minutes=20,
            max_duty_minutes=420,
            max_fdp_minutes=360,
            max_sectors=2,
        )
        | values
    )


def crew_info(name, windows, **values):
    return dict(name=name, windows=windows, aircraft_types=["E35L"]) | values


def roster(*crews, blocks=()):
    return CrewOperations.model_validate(
        {
            "source": "Crew roster export",
            "updated_at": stamp(24, "04:00"),
            "coverage_start_utc": stamp(24, "00:00"),
            "coverage_end_utc": stamp(30, "23:59"),
            "crews": list(crews),
            "aircraft_blocks": list(blocks),
        }
    )


@pytest.mark.parametrize(
    ("first_limit", "expected"), [(420, "fits"), (180, "fits_after_rest")]
)
def test_working_crew_through_two_sectors_or_approved_rest(first_limit, expected):
    first = duty(
        "UAAA",
        stamp(25, "07:00"),
        stamp(25, "17:00"),
        max_fdp_minutes=first_limit,
        min_rest_minutes=120,
    )
    second = duty("UACC", stamp(25, "12:00"), stamp(25, "20:00"))
    program = [
        sector("UAAA", "UACC", stamp(25, "08:00"), stamp(25, "09:00")),
        sector("UACC", "UTTT", stamp(25, "12:30"), stamp(25, "14:00")),
    ]
    if expected == "fits":
        program[1].update(
            departure_utc=stamp(25, "10:00"), arrival_utc=stamp(25, "11:30")
        )
    evidence = roster(
        crew_info("Day crew", [first, second], assigned_registration="UP-A")
    )
    report = roster_crew_report(evidence, "UP-A", "E35L", program, NOW)
    assert report["working"][0]["status"] == expected
    if expected == "fits_after_rest":
        assert report["working"][0]["rest_breaks"] == [
            {
                "station": "UACC",
                "previous_release_utc": stamp(25, "09:20"),
                "available_from_utc": stamp(25, "12:00"),
                "next_departure_utc": stamp(25, "12:30"),
            }
        ]


def test_handover_checks_positioning_duty_with_roster_order_independent():
    program = [
        sector("UAAA", "UACC", stamp(25, "08:00"), stamp(25, "09:00")),
        sector("UACC", "UTTT", stamp(25, "12:00"), stamp(25, "14:00")),
    ]
    short = duty(
        "UAAA",
        stamp(25, "07:00"),
        stamp(25, "17:00"),
        max_fdp_minutes=180,
        min_rest_minutes=240,
    )
    recovery = duty("UACC", stamp(25, "10:00"), stamp(25, "20:00"))
    reserve = crew_info(
        "Reserve",
        [duty("UAAA", stamp(25, "07:00"), stamp(25, "20:00"))],
        positioning=[
            {
                "departure": "UAAA",
                "arrival": "UACC",
                "departure_utc": stamp(25, "09:00"),
                "arrival_utc": stamp(25, "10:00"),
                "ready_utc": stamp(25, "11:00"),
                "flight_number": "KC101",
                "counted_duty_minutes": 120,
                "counted_fdp_minutes": 0,
            }
        ],
    )
    evidence = roster(
        reserve, crew_info("Day crew", [short, recovery], assigned_registration="UP-A")
    )
    report = roster_crew_report(evidence, "UP-A", "E35L", program, NOW)
    current = report["working"][0]
    assert current["first_limited_segment"] == 2
    assert current["earliest_continuation_utc"] == stamp(25, "13:50")
    shifted = report["replacements"][-1]
    assert shifted["handover_station"] == "UACC"
    assert shifted["handover_utc"] == stamp(25, "11:30")
    assert shifted["positioning"]["flight_number"] == "KC101"
    assert shifted["remaining_minutes"] == 130


def test_wide_window_retains_a_crew_continuation_after_rest():
    first = duty(
        "UAAA",
        stamp(25, "07:00"),
        stamp(25, "16:00"),
        max_fdp_minutes=180,
        min_rest_minutes=240,
    )
    second = duty("UACC", stamp(25, "10:00"), stamp(25, "20:00"))
    program = [
        sector(
            "UAAA", "UACC", stamp(25, "08:00"), stamp(25, "09:00"), request_leg_index=0
        ),
        sector(
            "UACC",
            "UTTT",
            stamp(25, "12:00"),
            stamp(25, "14:00"),
            request_leg_index=1,
            time_mode="WINDOW",
            window_end_utc=stamp(25, "17:00"),
        ),
    ]
    evidence = roster(
        crew_info("Day crew", [first, second], assigned_registration="UP-A")
    )
    report = roster_crew_report(evidence, "UP-A", "E35L", program, NOW)
    assert report["working"][0]["status"] == "projected_after_rest"
    assert report["working"][0]["rest_breaks"][0]["next_departure_utc"] == stamp(
        25, "13:50"
    )


def test_foreflight_rejects_great_circle_and_uses_taxi_plus_airborne():
    response = {
        "flightData": {"routeToDestination": {"route": "KOLAR"}},
        "performance": {
            "distances": {"destination": 270, "gcdDestination": 270},
            "times": {
                "timeToDestinationMinutes": 42,
                "taxiTimeMinutes": 8,
                "totalTimeMinutes": 560,
            },
            "fuel": {
                "unit": "Pound",
                "flightFuel": 221,
                "totalFuel": 1000,
                "maxTotalFuel": 900,
            },
        },
    }
    assert route_performance(response, "UAAA", "UACC", "KOLAR") is None
    response["performance"]["distances"]["destination"] = 275
    result = route_performance(response, "UAAA", "UACC", "KOLAR")
    assert result["block_minutes"] == 50
    assert result["fuel_kg"] == 100.2
    assert result["status"] == "range_limit"


class EmptyHistory:
    def summary(self, *args, **kwargs):
        return {}


def flight(tail, origin, destination, at, end, *, ferry=False):
    return {
        "aircraft": tail,
        "aircraftType": "E35L",
        "departure": origin,
        "arrival": destination,
        "departureUTC": at,
        "arrivalUTC": end,
        "ferry": ferry,
    }


@pytest.mark.asyncio
async def test_all_tails_sta_techstop_and_published_aircraft_block():
    async def lookup(origin, destination, _stamp, tail, _pax):
        limited = tail == "UP-Z" and (origin, destination) == ("UAAA", "UTTT")
        return {
            "status": "range_limit" if limited else "calculated",
            "route": "KOLAR",
            "distance_nm": 250,
            "block_minutes": 60,
            "fuel_kg": 700,
            "issues": ["Usable fuel exceeded"] if limited else [],
        }

    request = {
        "legs": [
            {
                "dep_icao": "UAAA",
                "arr_icao": "UTTT",
                "time_mode": "STA",
                "date_utc": "2026-11-25",
                "target_time_utc": stamp(25, "12:00"),
            }
        ],
        "pax": 6,
        "is_return": False,
        "stay_days": 0,
    }
    result = await build_charter_matrix(
        request,
        [
            {"registration": "UP-Z", "icao": "E35L"},
            {"registration": "UP-A", "icao": "E35L"},
        ],
        {
            "complete": True,
            "flights": [
                flight("UP-Z", "UACC", "UAAA", stamp(24, "01:00"), stamp(24, "02:00"))
            ],
        },
        saved_routes=[
            {"departure": "UAAA", "destination": "UACC"},
            {"departure": "UACC", "destination": "UTTT"},
        ],
        airport_profiles=[
            {
                "scope_type": "icao",
                "scope_key": "UACC",
                "techstop_turn_minutes": 45,
                "operating_hours_utc": "05:00-12:00",
            }
        ],
        route_lookup=lookup,
        history=EmptyHistory(),
        now=NOW,
        crew_operations=roster(
            blocks=[
                {
                    "registration": "UP-Z",
                    "kind": "MAINTENANCE",
                    "start_utc": stamp(25, "08:00"),
                    "end_utc": stamp(25, "10:00"),
                    "reason": "Scheduled inspection",
                    "station": "UAAA",
                }
            ]
        ),
    )
    assert [row["registration"] for row in result["aircraft"]] == ["UP-Z", "UP-A"]
    stop, direct = result["aircraft"]
    assert stop["position"]["projected_status"] == "at_departure"
    assert stop["legs"][0]["departure_utc"] == stamp(25, "09:15")
    assert stop["legs"][0]["elapsed_minutes"] == 165
    assert stop["legs"][0]["techstops"][0]["airport"] == "UACC"
    assert stop["schedule"]["aircraft_blocks"][0]["kind"] == "MAINTENANCE"
    assert stop["schedule"]["status"] == "conflict"
    assert direct["legs"][0]["departure_utc"] == stamp(25, "11:00")
    assert direct["position"]["planned_station"] is None


def test_opportunities_require_ferry_flag_and_consistent_station():
    data = {
        "complete": True,
        "flights": [
            flight("UP-A", "UAAA", "UACC", stamp(24, "01:00"), stamp(24, "02:00")),
            flight(
                "UP-A",
                "UACC",
                "UAAA",
                stamp(26, "01:00"),
                stamp(26, "02:00"),
                ferry=True,
            ),
        ],
    }
    result = charter_opportunities(data, now=NOW)
    assert result["empty_legs"][0]["registration"] == "UP-A"
    assert result["open_windows"][0]["minutes"] == 44 * 60


@pytest.mark.asyncio
@pytest.mark.parametrize("day", [24, 30])
async def test_a_current_crew_remainder_applies_to_today_but_not_to_future_quote(day):
    aircraft = {"id": 123, "registration": "UP-A", "icao": "E35L"}
    programmes = []
    actual_calls = []

    async def route_lookup(*args):
        return {"status": "calculated", "block_minutes": 60, "fuel_kg": 100}

    async def quote_lookup(tail, chain, *, now):
        assert tail == aircraft and now == NOW
        programmes.append(chain)
        return {
            "status": "fits",
            "required_duty_minutes": 110,
            "duty_limit_minutes": 600,
            "report_utc": stamp(day, "06:30"),
            "fdp_end_utc": stamp(day, "08:00"),
            "release_utc": stamp(day, "08:20"),
        }

    async def actual_lookup(registration):
        actual_calls.append(registration)
        return {
            "members": [{"status": "on_duty", "duty_limit_utc": stamp(24, "07:50")}]
        }

    result = await build_charter_matrix(
        {
            "legs": [
                {
                    "dep_icao": "UAAA",
                    "arr_icao": "UACC",
                    "time_mode": "STD",
                    "date_utc": f"2026-11-{day}",
                    "target_time_utc": stamp(day, "07:00"),
                }
            ],
            "pax": 4,
        },
        [aircraft],
        {
            "complete": True,
            "flights": [
                flight("UP-A", "UACC", "UAAA", stamp(24, "01:00"), stamp(24, "02:00"))
            ],
        },
        route_lookup=route_lookup,
        history=EmptyHistory(),
        now=NOW,
        quote_ftl_lookup=quote_lookup,
        current_crew_lookup=actual_lookup,
    )
    crew = result["aircraft"][0]["crew"]
    assert len(programmes) == 1 and not programmes[0][0]["ferry"]
    assert crew["quote_ftl"]["status"] == "fits"
    if day == 24:
        assert actual_calls == ["UP-A"]
        assert crew["current_quote_ftl"]["status"] == "limited"
        assert crew["current_quote_ftl"]["duty_margin_minutes"] == -30
        assert crew["current_quote_ftl"]["first_limited_segment"] == 1
    else:
        assert actual_calls == []
        assert "current_quote_ftl" not in crew


@pytest.mark.asyncio
async def test_flexible_window_samples_airport_hours_and_exposes_requested_bounds():
    async def lookup(*args):
        return {
            "status": "calculated",
            "route": "KOLAR",
            "distance_nm": 250,
            "block_minutes": 60,
            "fuel_kg": 700,
            "issues": [],
        }

    request = {
        "legs": [
            {
                "dep_icao": "UAAA",
                "arr_icao": "UACC",
                "time_mode": "WINDOW",
                "date_utc": "2026-11-25",
                "window_start_utc": stamp(25, "00:00"),
                "window_end_utc": stamp(25, "13:00"),
            }
        ],
        "pax": 6,
    }
    result = await build_charter_matrix(
        request,
        [{"registration": "UP-A", "icao": "E35L"}],
        {
            "complete": True,
            "flights": [
                flight("UP-A", "UACC", "UAAA", stamp(24, "15:00"), stamp(24, "16:00"))
            ],
        },
        airport_profiles=[
            {
                "scope_type": "icao",
                "scope_key": "UAAA",
                "operating_hours_utc": "06:00-18:00",
            },
            {
                "scope_type": "icao",
                "scope_key": "UACC",
                "operating_hours_utc": "08:00-18:00",
            },
        ],
        route_lookup=lookup,
        history=EmptyHistory(),
        now=NOW,
    )
    leg = result["aircraft"][0]["legs"][0]
    assert leg["departure_utc"] == stamp(25, "07:00")
    assert leg["window_start_utc"] == stamp(25, "00:00")
    assert leg["window_end_utc"] == stamp(25, "13:00")
    assert leg["timing_sample"] is True


@pytest.mark.asyncio
async def test_explicit_range_limit_cannot_be_suppressed_by_history():
    class Historical:
        def summary(self, *args, **kwargs):
            return {
                "block_minutes": {"count": 10, "median": 70},
                "fuel": {"KG": {"count": 10, "median": 660}},
            }

    async def lookup(*args):
        return {
            "status": "range_limit",
            "route": "KOLAR",
            "distance_nm": 250,
            "block_minutes": 60,
            "fuel_kg": 700,
            "issues": ["Usable fuel exceeded"],
        }

    request = {
        "legs": [
            {
                "dep_icao": "UAAA",
                "arr_icao": "UACC",
                "time_mode": "STD",
                "date_utc": "2026-11-25",
                "target_time_utc": stamp(25, "08:00"),
            }
        ],
        "pax": 6,
    }
    result = await build_charter_matrix(
        request,
        [{"registration": "UP-A", "icao": "E35L"}],
        {
            "complete": True,
            "flights": [
                flight("UP-A", "UACC", "UAAA", stamp(24, "15:00"), stamp(24, "16:00"))
            ],
        },
        history=Historical(),
        route_lookup=lookup,
        now=NOW,
    )
    tail = result["aircraft"][0]
    assert tail["legs"][0]["block_minutes"] is None
    assert tail["legs"][0]["fuel_kg"] is None
    assert tail["schedule"]["status"] == "partial"
