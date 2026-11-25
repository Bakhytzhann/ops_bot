import math
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import cache

import airportsdata

from services.fleet_operations import build_fleet, station_for_aircraft
from services.journey_history import MAX_HISTORY_DAYS, JourneyHistory, journey_history
from services.snapshot_models import utc

KM_PER_NM = 1.852
ISA_CRUISE_TEMPERATURE_K = 216.65
BLOCK_OVERHEAD_MINUTES = 24


class CharterEstimateError(ValueError):
    """The requested route or timing cannot be estimated."""


@dataclass(frozen=True)
class _IsaProfile:
    cruise_kt: int
    fuel_kg_per_block_hour: int


# Rounded planning assumptions. Fuel rates are based on ICAO CORSIA CERT 2024,
# Table A-2.2.a. Speed inputs are nominal values below the Embraer Legacy 650E
# maximum cruise and near the Gulfstream G650ER and BADA CL60 cruise speeds.
# https://www.icao.int/sites/default/files/environmental-protection/CORSIA/Documents/CERT%202024/CERT-2024-Design-Development-and-Validation.pdf
# https://www.embraer.com/media/dg1crlts/defense-security-english.pdf
# https://assets.gulfstream.aero/downloads/22_464552_G650ER_Product_Sheet_PROD_DAM.pdf
# https://www.eurocontrol.int/sites/default/files/library/015_BADA_Aircraft_Performance_Summary_Tables.pdf
ISA_PROFILES = {
    "E35L": _IsaProfile(cruise_kt=421, fuel_kg_per_block_hour=1040),
    "GLF6": _IsaProfile(cruise_kt=488, fuel_kg_per_block_hour=1450),
    "CL60": _IsaProfile(cruise_kt=440, fuel_kg_per_block_hour=960),
}
TYPE_ALIASES = {
    "CHALLENGER 600": "CL60",
    "CHALLENGER 601": "CL60",
    "CHALLENGER 604": "CL60",
    "CHALLENGER 605": "CL60",
    "CHALLENGER 650": "CL60",
    "EMB-135BJ": "E35L",
    "LEGACY 600": "E35L",
    "LEGACY 650": "E35L",
    "LEGACY 650E": "E35L",
    "G650": "GLF6",
    "G650ER": "GLF6",
}


@cache
def _airports():
    return airportsdata.load("ICAO")


def _airport(code):
    airport = _airports().get(code)
    if airport is None:
        raise CharterEstimateError(f"Unknown airport ICAO code: {code}.")
    return airport


def validate_route(departure: str, arrival: str) -> None:
    departure, arrival = departure.strip().upper(), arrival.strip().upper()
    _airport(departure)
    _airport(arrival)
    if departure == arrival:
        raise CharterEstimateError("Departure and arrival airports must differ.")


def _route_distance(routing, departure, arrival):
    if not routing:
        return None, None
    route = routing.get("route")
    distance = routing.get("distance_nm")
    if not isinstance(route, str) or type(distance) not in (int, float):
        return None, None
    tokens = route.upper().split()
    if not set(tokens).difference({departure, arrival, "DCT", "DIRECT"}):
        return None, None
    if not math.isfinite(distance) or distance <= 0:
        return None, None
    return " ".join(tokens), distance


def _type_code(value):
    if not value or value == "—":
        return None
    for prefix in ("EMBRAER ", "GULFSTREAM ", "BOMBARDIER "):
        value = value.removeprefix(prefix)
    return TYPE_ALIASES.get(value, value)


def _utc_datetime(stamp):
    if stamp.utcoffset() is None:
        raise CharterEstimateError("Enter an explicit timezone for the departure time.")
    return stamp.astimezone(UTC)


def _iso(stamp):
    return stamp.isoformat() if stamp is not None else None


def _historical_mean(summary):
    count = summary.get("count", 0)
    mean = summary.get("mean")
    if count and mean is not None and mean > 0:
        return round(mean), count
    return None, 0


def _metric(value, samples, modeled):
    if value is not None:
        return value, "journey_log", samples
    if modeled is not None:
        return round(modeled), "isa_model", 0
    return None, "unavailable", 0


@dataclass(frozen=True)
class _RouteEstimator:
    aircraft_type: str | None
    history: JourneyHistory
    isa_deviation_c: float
    history_end: datetime

    def _history_statistics(self, departure, arrival):
        if not self.aircraft_type:
            return {}
        end = self.history_end.date() - timedelta(days=1)
        start = end - timedelta(days=MAX_HISTORY_DAYS - 1)
        try:
            return self.history.summary(
                start,
                end,
                departure=departure,
                arrival=arrival,
                aircraft_type=self.aircraft_type,
            )
        except (OSError, sqlite3.Error):
            return {}

    def _isa_block_minutes(self, profile, distance_nm, wind_kt):
        # At fixed Mach, true airspeed varies with the square root of absolute
        # temperature. The wind input is the signed along-track component.
        temperature_ratio = (
            ISA_CRUISE_TEMPERATURE_K + self.isa_deviation_c
        ) / ISA_CRUISE_TEMPERATURE_K
        ground_speed = profile.cruise_kt * math.sqrt(temperature_ratio) + wind_kt
        return BLOCK_OVERHEAD_MINUTES + distance_nm / ground_speed * 60

    def leg(self, departure, arrival, stamp, *, routing=None, wind_kt=0.0):
        route, distance_nm = _route_distance(routing, departure, arrival)
        historical = self._history_statistics(departure, arrival)
        actual_block, block_samples = _historical_mean(
            historical.get("block_minutes", {})
        )
        # The repository converts pounds to kilograms. Other units lack verified
        # density or conversions here and cannot be combined with mass data.
        actual_fuel, fuel_samples = _historical_mean(
            historical.get("fuel", {}).get("KG", {})
        )
        profile = ISA_PROFILES.get(self.aircraft_type)
        modeled_block = (
            self._isa_block_minutes(profile, distance_nm, wind_kt)
            if profile and distance_nm is not None
            else None
        )
        block, block_basis, block_samples = _metric(
            actual_block, block_samples, modeled_block
        )
        modeled_fuel = (
            block / 60 * profile.fuel_kg_per_block_hour
            if profile and block is not None
            else None
        )
        fuel, fuel_basis, fuel_samples = _metric(
            actual_fuel, fuel_samples, modeled_fuel
        )
        arrival_time = (
            stamp + timedelta(minutes=block) if stamp and block is not None else None
        )
        return {
            "departure": departure,
            "arrival": arrival,
            "departure_utc": _iso(stamp),
            "arrival_utc": _iso(arrival_time),
            "route": route,
            "route_basis": "foreflight" if route is not None else "unavailable",
            "distance_nm": round(distance_nm, 1) if distance_nm is not None else None,
            "distance_km": round(distance_nm * KM_PER_NM, 1)
            if distance_nm is not None
            else None,
            "block_minutes": block,
            "fuel_kg": fuel,
            "block_basis": block_basis,
            "fuel_basis": fuel_basis,
            "block_samples": block_samples,
            "fuel_samples": fuel_samples,
            "wind_kt": wind_kt,
        }


def _schedule_leg(flight):
    if flight is None:
        return None
    return {
        "flight_id": flight.get("flightNid"),
        "flight_number": flight.get("flightNumber"),
        "departure": flight.get("departure"),
        "arrival": flight.get("arrival"),
        "departure_utc": _iso(utc(flight.get("departureUTC"))),
        "arrival_utc": _iso(utc(flight.get("arrivalUTC"))),
        "ferry": flight.get("ferry"),
    }


def _has_invalid_times(schedule, aircraft):
    for flight in schedule.get("flights") or []:
        if not isinstance(flight, dict) or flight.get("cancelled") is True:
            continue
        registration = flight.get("aircraft")
        if (
            not isinstance(registration, str)
            or registration.strip().upper() != aircraft
        ):
            continue
        departure = utc(flight.get("departureUTC"))
        arrival = utc(flight.get("arrivalUTC"))
        if departure is None or arrival is None or arrival <= departure:
            return True
    return False


def _matches_ferry(flight, leg):
    return (
        flight.get("ferry") is True
        and flight.get("departure") == leg["departure"]
        and flight.get("arrival") == leg["arrival"]
        and utc(flight["departureUTC"]).replace(second=0, microsecond=0)
        == utc(leg["departure_utc"]).replace(second=0, microsecond=0)
    )


def _matching_ferry(flights, leg):
    matches = [flight for flight in flights if _matches_ferry(flight, leg)]
    return matches[0] if len(matches) == 1 else None


def _aircraft_at(schedule, aircraft, departure_utc):
    schedule = schedule or {}
    valid_times = not _has_invalid_times(schedule, aircraft)
    schedule_complete = schedule.get("complete") is True and valid_times
    position_complete = (
        schedule.get("position_complete", schedule_complete) is True and valid_times
    )
    fleet = build_fleet(schedule, departure_utc)
    item = next((item for item in fleet if item["registration"] == aircraft), {})
    return item, schedule_complete, position_complete


def _position_status(item, departure, scheduled_ferry, position_complete):
    active = item.get("active")
    if scheduled_ferry and position_complete and active in (None, scheduled_ferry):
        return "at_departure", departure
    if active:
        return "airborne", None
    station = (
        station_for_aircraft(item) if item.get("last") and position_complete else None
    )
    next_station = (item.get("next") or {}).get("departure")
    if not station or (next_station and station != next_station):
        return "unknown", None
    return ("at_departure" if station == departure else "ferry_required"), station


def charter_ferry_request(schedule, aircraft, departure, arrival, departure_utc):
    """Get the required positioning airport pair before route and duration retrieval."""
    departure_utc = _utc_datetime(departure_utc)
    aircraft = aircraft.strip().upper()
    departure, arrival = departure.strip().upper(), arrival.strip().upper()
    item, _, position_complete = _aircraft_at(schedule, aircraft, departure_utc)
    match = _matching_ferry(
        item.get("flights", []),
        {
            "departure": departure,
            "arrival": arrival,
            "departure_utc": _iso(departure_utc),
        },
    )
    status, station = _position_status(item, departure, match, position_complete)
    if status == "ferry_required" and station in _airports():
        # Evaluate routing near the quote time. Estimate the ferry STD after its
        # duration is known.
        return station, departure, departure_utc
    return None


def _first_conflict(flights, legs, ferry, matching_ferries):
    start = utc((ferry or {}).get("departure_utc")) or utc(legs[0]["departure_utc"])
    end = utc(legs[-1]["arrival_utc"])
    last_start = utc(legs[-1]["departure_utc"])
    if final_ferry := matching_ferries[-1]:
        scheduled_arrival = utc(final_ferry["arrivalUTC"])
        end = max(end, scheduled_arrival) if end else scheduled_arrival
    for flight in flights:
        if flight in matching_ferries:
            continue
        scheduled_start = utc(flight["departureUTC"])
        scheduled_end = utc(flight["arrivalUTC"])
        if scheduled_start <= start < scheduled_end:
            return _schedule_leg(flight)
        if start < scheduled_start and (
            scheduled_start < end if end else scheduled_start <= last_start
        ):
            return _schedule_leg(flight)
    return None


def _position(
    item, estimator, legs, *, schedule_complete, position_complete, ferry_route
):
    last, upcoming = item.get("last"), item.get("next")
    departure_utc = utc(legs[0]["departure_utc"])
    departure = legs[0]["departure"]
    flights = item.get("flights", [])
    matching_ferries = [_matching_ferry(flights, leg) for leg in legs]
    scheduled_ferry, ferry = matching_ferries[0], None
    status, station = _position_status(
        item, departure, scheduled_ferry, position_complete
    )
    if status == "at_departure" and not scheduled_ferry and last.get("ferry") is True:
        scheduled_ferry = last
    if status == "ferry_required" and station in _airports():
        ferry = estimator.leg(station, departure, None, routing=ferry_route)
        duration = ferry["block_minutes"]
        if duration is not None:
            ferry["departure_utc"] = _iso(departure_utc - timedelta(minutes=duration))
            ferry["arrival_utc"] = _iso(departure_utc)
    return {
        "status": status,
        "station": station,
        "as_of_utc": _iso(departure_utc),
        "schedule_complete": schedule_complete,
        "ferry": ferry,
        "scheduled_ferry": _schedule_leg(scheduled_ferry),
        "last_flight": _schedule_leg(last),
        "next_flight": _schedule_leg(upcoming),
        "schedule_conflict": _first_conflict(flights, legs, ferry, matching_ferries),
    }


def estimate_charter(
    *,
    departure: str,
    arrival: str,
    departure_utc: datetime,
    aircraft: str,
    schedule: dict | None,
    passengers: int,
    return_utc: datetime | None = None,
    isa_deviation_c: float = 0,
    wind_kt: float = 0,
    aircraft_type: str | None = None,
    history: JourneyHistory | None = None,
    routes: Mapping[tuple[str, str], dict] | None = None,
    ferry_route: dict | None = None,
):
    departure, arrival = departure.strip().upper(), arrival.strip().upper()
    validate_route(departure, arrival)
    departure_utc = _utc_datetime(departure_utc)
    if return_utc is not None:
        return_utc = _utc_datetime(return_utc)
        if return_utc <= departure_utc:
            raise CharterEstimateError(
                "Return departure must follow outbound departure."
            )
    if not -80 <= isa_deviation_c <= 80 or not -250 <= wind_kt <= 250:
        raise CharterEstimateError(
            "Temperature or wind adjustment is outside model bounds."
        )

    aircraft = aircraft.strip().upper()
    item, schedule_complete, position_complete = _aircraft_at(
        schedule, aircraft, departure_utc
    )
    scheduled_type = _type_code((item.get("type") or "").strip().upper())
    selected_type = _type_code((aircraft_type or "").strip().upper())
    aircraft_type = scheduled_type or selected_type
    if selected_type and scheduled_type and len(scheduled_type) != 4:
        aircraft_type = selected_type
    estimator = _RouteEstimator(
        aircraft_type,
        history if history is not None else journey_history,
        isa_deviation_c,
        min(departure_utc, datetime.now(UTC)),
    )
    routes = routes or {}
    legs = [
        estimator.leg(
            departure,
            arrival,
            departure_utc,
            routing=routes.get((departure, arrival)),
            wind_kt=wind_kt,
        )
    ]
    if return_utc is not None:
        outbound_arrival = utc(legs[0]["arrival_utc"])
        if outbound_arrival and return_utc < outbound_arrival:
            raise CharterEstimateError(
                "Return departure overlaps the estimated outbound leg."
            )
        legs.append(
            estimator.leg(
                arrival,
                departure,
                return_utc,
                routing=routes.get((arrival, departure)),
                wind_kt=-wind_kt,
            )
        )
    return {
        "aircraft": aircraft,
        "aircraft_type": aircraft_type,
        "passengers": passengers,
        "isa_deviation_c": isa_deviation_c,
        "wind_kt": wind_kt,
        "legs": legs,
        "position": _position(
            item,
            estimator,
            legs,
            schedule_complete=schedule_complete,
            position_complete=position_complete,
            ferry_route=ferry_route,
        ),
    }
