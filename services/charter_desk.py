import asyncio
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from functools import partial
from math import ceil, floor
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from httpx import HTTPError

from services.charter_crew import charter_crew
from services.charter_crew_operations import roster_crew_report
from services.charter_estimator import (
    ISA_PROFILES,
    KM_PER_NM,
    _airports,
    _RouteEstimator,
    _schedule_leg,
    _type_code,
)
from services.charter_performance import charter_route_performance
from services.fleet_operations import build_fleet
from services.journey_history import journey_history
from services.snapshot_models import utc

DAY = timedelta(days=1)
SCHEDULE_LIMITS = {"AOG", "MAINTENANCE", "MX", "TECHNICAL HOLD"}


def _iso(stamp):
    return stamp.isoformat() if stamp else None


def _anchor(leg):
    return (
        utc(leg.get("target_time_utc"))
        or utc(leg.get("window_start_utc"))
        or datetime.combine(date.fromisoformat(leg["date_utc"]), time.min, UTC)
    )


def _by_type(aircraft):
    return _type_code(str(aircraft.get("icao") or aircraft.get("type") or "").upper())


def _median(metric):
    value = metric.get("median")
    return value if metric.get("count") and value is not None and value > 0 else None


def _choose(provider, historical, modeled):
    if provider is not None:
        return provider, "foreflight"
    if historical is not None:
        return historical, "journey_log"
    if modeled is not None:
        return modeled, "isa_model"
    return None, "unavailable"


def _metrics(estimator, departure, arrival, performance):
    status = performance.get("status") or "no_route"
    # A failed direct route can have history from prior loads. Its historical
    # block/fuel would misstate the operability and cost of this itinerary.
    history = (
        {}
        if status in {"range_limit", "blocked"}
        else estimator._history_statistics(departure, arrival)
    )
    calculated = status == "calculated"
    profile = ISA_PROFILES.get(estimator.aircraft_type)
    distance = performance.get("distance_nm")
    modeled = (
        estimator._isa_block_minutes(profile, distance, 0)
        if profile and distance and calculated
        else None
    )
    block, block_basis = _choose(
        performance.get("block_minutes") if calculated else None,
        _median(history.get("block_minutes", {})),
        round(modeled) if modeled is not None else None,
    )
    fuel, fuel_basis = _choose(
        performance.get("fuel_kg") if calculated else None,
        _median(history.get("fuel", {}).get("KG", {})),
        round(block / 60 * profile.fuel_kg_per_block_hour)
        if profile and block is not None
        else None,
    )
    return {
        "departure": departure,
        "arrival": arrival,
        "route": performance.get("route"),
        "route_basis": "foreflight" if performance.get("route") else "unavailable",
        "distance_nm": distance,
        "distance_km": round(distance * KM_PER_NM, 1) if distance is not None else None,
        "airborne_minutes": (
            performance.get("airborne_minutes") if calculated else None
        )
        or _median(history.get("airborne_minutes", {})),
        "block_minutes": block,
        "fuel_kg": fuel,
        "block_basis": block_basis,
        "fuel_basis": fuel_basis,
        "performance_status": status,
        "issues": performance.get("issues", []),
    }


@dataclass
class _Desk:
    estimator: _RouteEstimator
    registration: str
    profiles: dict
    saved_routes: list
    route_lookup: object
    route_slots: asyncio.Semaphore

    async def metrics(self, origin, destination, stamp, passengers):
        try:
            async with self.route_slots:
                async with asyncio.timeout(18):
                    performance = await self.route_lookup(
                        origin, destination, stamp, self.registration, passengers
                    )
        except (HTTPError, OSError, RuntimeError, ValueError, TimeoutError):
            performance = None
        return await asyncio.to_thread(
            _metrics, self.estimator, origin, destination, performance or {}
        )

    def stop_turn(self, airport):
        return self.profiles.get(airport, {}).get("techstop_turn_minutes")

    def techstop_routes(self, departure, arrival):
        stops = {airport for airport in self.profiles if self.stop_turn(airport)} - {
            departure,
            arrival,
        }
        pairs = {
            (row.get("departure"), row.get("destination"))
            for row in self.saved_routes
            if row.get("designation") in (None, "Primary")
        }
        single = [
            [airport]
            for airport in sorted(stops)
            if (departure, airport) in pairs and (airport, arrival) in pairs
        ]
        double = [
            [left, right]
            for left in sorted(stops)
            for right in sorted(stops - {left})
            if (departure, left) in pairs
            and (left, right) in pairs
            and (right, arrival) in pairs
        ]
        return [*single, *double]


def _segment_times(segment, anchor, *, sta):
    block = segment["block_minutes"]
    if block is None:
        dates = (None, anchor) if sta else (anchor, None)
    else:
        duration = timedelta(minutes=block)
        dates = (anchor - duration, anchor) if sta else (anchor, anchor + duration)
    return {**segment, "departure_utc": _iso(dates[0]), "arrival_utc": _iso(dates[1])}


async def _segment(desk, departure, arrival, anchor, pax, *, sta):
    metrics = await desk.metrics(departure, arrival, anchor, pax)
    if sta and metrics["block_basis"] == "foreflight":
        # Forecasts must use the departure obtained from the requested arrival.
        revised = await desk.metrics(
            departure,
            arrival,
            anchor - timedelta(minutes=metrics["block_minutes"]),
            pax,
        )
        if revised["performance_status"] != "no_route":
            metrics = revised
    return _segment_times(metrics, anchor, sta=sta)


def _total(segments, key):
    values = [segment.get(key) for segment in segments]
    return round(sum(values), 1) if all(value is not None for value in values) else None


def _combined_basis(segments, key):
    values = {segment[key] for segment in segments}
    return next(iter(values)) if len(values) == 1 else "mixed"


def _techstop_details(desk, segments):
    return [
        {
            "airport": previous["arrival"],
            "arrival_utc": previous["arrival_utc"],
            "departure_utc": following["departure_utc"],
            "ground_minutes": desk.stop_turn(previous["arrival"]),
        }
        for previous, following in zip(segments, segments[1:])
    ]


async def _chain(desk, airports, anchor, pax, *, sta):
    pairs = list(zip(airports, airports[1:]))
    segments = []
    for departure, arrival in reversed(pairs) if sta else pairs:
        segment = (
            await _segment(desk, departure, arrival, anchor, pax, sta=sta)
            if anchor
            else {
                **await asyncio.to_thread(
                    _metrics, desk.estimator, departure, arrival, {}
                ),
                "departure_utc": None,
                "arrival_utc": None,
            }
        )
        segments.append(segment)
        stop = departure if sta else arrival
        edge = utc(segment["departure_utc"] if sta else segment["arrival_utc"])
        turn = desk.stop_turn(stop)
        anchor = (
            edge + (-1 if sta else 1) * timedelta(minutes=turn)
            if turn and edge
            else None
        )
    if sta:
        segments.reverse()
    return segments


def _join_chain(desk, segments):
    stops = _techstop_details(desk, segments)
    issues = [issue for segment in segments for issue in segment["issues"]]
    issues.extend(
        f"Set confirmed refuelling turnaround at {stop['airport']}."
        for stop in stops
        if stop["ground_minutes"] is None
    )
    result = {
        **segments[0],
        "arrival": segments[-1]["arrival"],
        "arrival_utc": segments[-1]["arrival_utc"],
        "elapsed_minutes": None,
        "techstops": stops,
        "segments": segments,
        "issues": list(dict.fromkeys(issues)),
    }
    if len(segments) > 1:
        result.update(
            block_minutes=_total(segments, "block_minutes"),
            fuel_kg=_total(segments, "fuel_kg"),
            distance_nm=_total(segments, "distance_nm"),
            distance_km=_total(segments, "distance_km"),
            airborne_minutes=_total(segments, "airborne_minutes"),
            route=" / ".join(
                segment["route"] for segment in segments if segment["route"]
            )
            or None,
            route_basis=_combined_basis(segments, "route_basis"),
            block_basis=_combined_basis(segments, "block_basis"),
            fuel_basis=_combined_basis(segments, "fuel_basis"),
            performance_status=_combined_basis(segments, "performance_status"),
        )
    start, end = utc(result["departure_utc"]), utc(result["arrival_utc"])
    if start and end:
        result["elapsed_minutes"] = (end - start).total_seconds() / 60
    return result


async def _route(desk, leg, anchor, pax):
    departure, arrival = leg["dep_icao"], leg["arr_icao"]
    via = leg.get("via") or []
    sta = leg["time_mode"] == "STA"
    segments = await _chain(desk, [departure, *via, arrival], anchor, pax, sta=sta)
    calculated = _join_chain(desk, segments)
    if via or calculated["performance_status"] != "range_limit":
        return calculated
    alternatives = []
    for via in desk.techstop_routes(departure, arrival)[:4]:
        alternative = await _chain(
            desk, [departure, *via, arrival], anchor, pax, sta=sta
        )
        if all(
            segment["performance_status"] == "calculated"
            and segment["block_basis"] == "foreflight"
            for segment in alternative
        ):
            alternatives.append(_join_chain(desk, alternative))
    if len(alternatives) == 1:
        return {**alternatives[0], "direct_issues": calculated["issues"]}
    calculated["alternative_chains"] = alternatives
    calculated["issues"].append(
        "ForeFlight reports a direct range limit. Confirm the refuelling chain."
        if alternatives
        else "ForeFlight reports a direct range limit. Add verified refuelling stops and their turnaround times."
    )
    return calculated


def _position(flights, stamp, schedule):
    for row in flights:
        if utc(row["departureUTC"]) <= stamp < utc(row["arrivalUTC"]):
            return None, "airborne"
    last = max(
        (row for row in flights if utc(row["arrivalUTC"]) <= stamp),
        key=lambda row: utc(row["arrivalUTC"]),
        default=None,
    )
    next_flight = next(
        (row for row in flights if utc(row["departureUTC"]) >= stamp), None
    )
    if (
        last
        and _covered(schedule, utc(last["arrivalUTC"]), stamp)
        and (not next_flight or last["arrival"] == next_flight["departure"])
    ):
        return last["arrival"], "on_ground"
    return None, "unconfirmed"


def _conflicts(flights, start, end):
    return [
        row
        for row in flights
        if utc(row["departureUTC"]) < end and utc(row["arrivalUTC"]) > start
    ]


def _window(leg):
    start = utc(leg.get("window_start_utc")) or _anchor(leg)
    end = utc(leg.get("window_end_utc")) or start + DAY
    return start, end


def _next_airport_open(profile, stamp):
    hours = _time_range(profile.get("operating_hours_utc"))
    curfew = _time_range(profile.get("curfew_utc"))
    midnight = datetime.combine(stamp.date(), time.min, UTC)
    boundaries = [
        midnight + timedelta(days=day, minutes=edge)
        for day in range(2)
        for edge in [*((hours[0],) if hours else ()), *((curfew[1],) if curfew else ())]
    ]
    return next(
        (
            candidate
            for candidate in sorted({stamp, *boundaries})
            if candidate >= stamp
            and (not hours or _in_range(candidate, hours))
            and (not curfew or not _in_range(candidate, curfew))
        ),
        None,
    )


def _next_window_sample(desk, leg, anchor, flights):
    arrival = utc(leg["arrival_utc"])
    delays = [timedelta()]
    for flight in _conflicts(flights, anchor, arrival):
        delays.append(utc(flight["arrivalUTC"]) - anchor)
    for part in leg["segments"]:
        for role in ("departure", "arrival"):
            stamp = utc(part[f"{role}_utc"])
            if not stamp:
                continue
            opened = _next_airport_open(desk.profiles.get(part[role], {}), stamp)
            if not opened:
                return None
            delays.append(opened - stamp)
    return anchor + max(delays)


async def _passenger_legs(desk, request, flights, now):
    legs = []
    previous = None
    for item in request["legs"]:
        mode = item["time_mode"]
        start, end = _window(item)
        anchor = (
            max(filter(None, (start, previous, now)))
            if mode == "WINDOW"
            else _anchor(item)
        )
        leg = await _route(desk, item, anchor, request["pax"])
        if mode == "WINDOW":
            # Check a concrete timing against covered SKED and configured port hours.
            # An untimed request remains flexible; this is not an exhaustive search.
            for _ in range(3):
                if not leg["arrival_utc"]:
                    break
                later = _next_window_sample(desk, leg, anchor, flights)
                if later == anchor:
                    break
                if later is None or later >= end:
                    leg["issues"].append(
                        "Known constraints block this sample; check the remaining requested window."
                    )
                    break
                anchor = later
                leg = await _route(desk, item, anchor, request["pax"])
        departure = utc(leg["departure_utc"])
        if departure and previous and departure < previous:
            leg["issues"].append("The previous sector arrives after this departure.")
        if mode == "WINDOW" and anchor >= end:
            leg["issues"].append(
                "No computed departure remains inside the requested window."
            )
        if departure and departure < now:
            leg["issues"].append("The calculated departure is in the past.")
        leg.update(
            time_mode=mode,
            window_start_utc=_iso(start) if mode == "WINDOW" else None,
            window_end_utc=_iso(end) if mode == "WINDOW" else None,
            timing_sample=mode == "WINDOW",
        )
        previous = utc(leg["arrival_utc"])
        legs.append(leg)
    return legs


async def _ferry(desk, station, first, now, before_leg):
    start = utc(first["departure_utc"])
    if not start or not station or station == first["departure"]:
        return []
    if station not in _airports():
        return []
    # Positioning before passenger departure needs the local turnaround constraint.
    turn = desk.stop_turn(first["departure"])
    anchor = start - timedelta(minutes=turn or 0)
    path = await _route(
        desk,
        {"dep_icao": station, "arr_icao": first["departure"], "time_mode": "STA"},
        anchor,
        0,
    )
    if turn is None:
        path["issues"].append(
            f"Set the turnaround at {first['departure']} between ferry and passenger service."
        )
        path["departure_utc"] = path["arrival_utc"] = None
        for segment in path["segments"]:
            segment["departure_utc"] = segment["arrival_utc"] = None
    if (ferry_start := utc(path["departure_utc"])) and ferry_start < now:
        path["issues"].append("Ferry departure is already in the past.")
    path.update(
        time_mode="STA",
        window_start_utc=None,
        window_end_utc=None,
        before_leg=before_leg,
    )
    return [path]


def _schedule_status(flights, all_legs, schedule_complete, blocks):
    start, end = utc(all_legs[0]["departure_utc"]), utc(all_legs[-1]["arrival_utc"])
    known = [(utc(leg["departure_utc"]), utc(leg["arrival_utc"])) for leg in all_legs]
    valid = all(a and b and a < b for a, b in known) and all(
        a <= b for (_, a), (b, _) in zip(known, known[1:])
    )
    known_starts = [a for a, _ in known if a]
    known_ends = [b for _, b in known if b]
    known_start = min(known_starts) if known_starts else start
    known_end = max(known_ends) if known_ends else end
    conflicts = (
        _conflicts(flights, known_start, known_end)
        if known_start and known_end and known_end > known_start
        else []
    )
    restrictions = [
        row
        for row in conflicts
        if str(row.get("status") or "").upper() in SCHEDULE_LIMITS
    ]
    aircraft_blocks = [
        block
        for block in blocks
        if known_start
        and known_end
        and block.start_utc < known_end
        and block.end_utc > known_start
    ]
    issues = []
    if restrictions:
        issues.append("Scheduled maintenance or AOG overlaps this itinerary.")
    if conflicts and not restrictions:
        issues.append("An assigned SKED flight overlaps this itinerary.")
    if not valid:
        issues.append(
            "Resolve the overlapping or undated sectors before treating this as a continuous rotation."
        )
    if not schedule_complete:
        issues.append("Check the SKED coverage for the requested operating dates.")
    issues.extend(
        f"{block.kind} · {block.reason} · {block.start_utc.isoformat()} to {block.end_utc.isoformat()}."
        for block in aircraft_blocks
    )
    return {
        "status": "conflict"
        if conflicts or aircraft_blocks
        else "clear"
        if valid and schedule_complete
        else "partial",
        "issues": issues,
        "conflicts": [_schedule_leg(row) for row in conflicts],
        "aircraft_blocks": [row.model_dump(mode="json") for row in aircraft_blocks],
    }


def _time_range(value):
    if not isinstance(value, str):
        return None
    match = re.fullmatch(
        r"([01]\d|2[0-3]):([0-5]\d)\s*[-–]\s*([01]\d|2[0-4]):([0-5]\d)", value.strip()
    )
    if not match:
        return None
    a, b, c, d = map(int, match.groups())
    return a * 60 + b, c * 60 + d


def _in_range(stamp, ranges):
    start, end = ranges
    point = stamp.hour * 60 + stamp.minute
    return start <= point < end if start < end else point >= start or point < end


def _airport_check(profiles, airport, role, stamp, now):
    profile = profiles.get(airport, {})
    opens = profile.get("operating_hours_utc")
    curfew = profile.get("curfew_utc")
    lead = profile.get("permit_lead_days")
    zone = _airports().get(airport, {}).get("tz")
    try:
        local = stamp.astimezone(ZoneInfo(zone)) if stamp and zone else None
    except ZoneInfoNotFoundError:
        local = None
    issues = []
    if (
        stamp
        and opens
        and (hours := _time_range(opens))
        and not _in_range(stamp, hours)
    ):
        issues.append(
            f"{airport} is outside its configured operating hours at the {role} time."
        )
    if (
        stamp
        and curfew
        and (closed := _time_range(curfew))
        and _in_range(stamp, closed)
    ):
        issues.append(f"{airport} curfew covers the {role} time.")
    if stamp and lead and stamp - now < timedelta(days=lead):
        issues.append(f"{airport} requires {lead} days of permit lead time.")
    return {
        "airport": airport,
        "role": role,
        "time_utc": _iso(stamp),
        "time_lt": _iso(local),
        "timezone": zone,
        "opening_hours_utc": opens,
        "curfew_utc": curfew,
        "permit_lead_days": lead,
        "status": "restricted"
        if issues
        else "checked"
        if profile and stamp and opens
        else "partial",
        "issues": issues
        or (
            []
            if opens
            else [f"Add verified operating hours for {airport} to the airport profile."]
        ),
    }


def _airport_checks(profiles, chain, now):
    checks = {}
    for leg in chain:
        for role, airport in (
            ("departure", leg["departure"]),
            ("arrival", leg["arrival"]),
        ):
            stamp = utc(leg[f"{role}_utc"])
            check = _airport_check(profiles, airport, role, stamp, now)
            checks[airport, role, stamp] = check
    return list(checks.values())


async def _crew_context(
    evidence, aircraft, aircraft_type, chain, flights, crew_lookup, now
):
    if report := roster_crew_report(evidence, aircraft, aircraft_type, chain, now):
        return report
    issues = [
        "Crew roster and positioning options are needed to name available replacement crews."
    ]
    matches = [
        flight
        for flight in flights
        for sector in chain
        if (flight["departure"], flight["arrival"])
        == (sector["departure"], sector["arrival"])
        and utc(flight["departureUTC"]) == utc(sector["departure_utc"])
        and utc(flight["arrivalUTC"]) == utc(sector["arrival_utc"])
    ]
    working = []
    for flight in matches[:4]:
        try:
            async with asyncio.timeout(10):
                report = await crew_lookup(flight)
        except (HTTPError, OSError, RuntimeError, TimeoutError):
            continue
        if report.get("members"):
            working.append(
                {
                    "name": flight.get("flightNumber") or "Scheduled crew",
                    "status": "scheduled_ftl",
                    "source": "Leon",
                    "members": report["members"],
                    "issues": ["Published limits apply to this scheduled sector."],
                }
            )
    return {
        "status": "partial",
        "issues": issues,
        "working": working,
        "replacements": [],
    }


def _profiles(airport_profiles):
    return {
        row["scope_key"]: row
        for row in airport_profiles
        if row.get("scope_type") == "icao"
    }


def _flatten(ferry, legs):
    staged = {item["before_leg"]: item for item in ferry}
    return [
        {
            **part,
            "request_leg_index": index if item is leg else -(index + 1),
            "ferry": item is not leg,
            "time_mode": item["time_mode"],
            "window_end_utc": item.get("window_end_utc"),
        }
        for index, leg in enumerate(legs)
        for item in (staged.get(index), leg)
        if item is not None
        for part in item["segments"]
    ]


def _current_quote_ftl(crew, chain, forecast, now):
    planned = next(iter(forecast.get("periods") or ()), forecast)
    report = utc(planned.get("report_utc"))
    release = utc(planned.get("release_utc"))
    fdp_end = utc(planned.get("fdp_end_utc"))
    members = (crew or {}).get("members", [])
    if not members or not release or release <= now or not report:
        return None

    blockers = [
        rest_until
        for member in members
        if member.get("status") == "resting"
        and (rest_until := utc(member.get("rest_until_utc")))
        and rest_until > report
    ]
    active = [member for member in members if member.get("status") == "on_duty"]
    if not active and not blockers:
        return None

    deadlines = {
        key: min(
            (
                stamp
                for member in active
                if (stamp := utc(member.get(f"{key}_limit_utc")))
            ),
            default=None,
        )
        for key in ("duty", "fdp")
    }
    ends = {"duty": release, "fdp": fdp_end}
    result = {
        "basis": "leon_current",
        "source": "Leon",
        "report_utc": report.isoformat(),
        "release_utc": release.isoformat(),
        "available_from_utc": max(blockers).isoformat() if blockers else None,
        "issues": [],
    }
    exceeded = []
    for key, target in ends.items():
        deadline = deadlines[key]
        result[f"required_{key}_minutes"] = (
            ceil((max(now, target) - now).total_seconds() / 60) if target else None
        )
        result[f"{key}_limit_minutes"] = (
            floor((deadline - now).total_seconds() / 60) if deadline else None
        )
        result[f"{key}_margin_minutes"] = (
            floor((deadline - target).total_seconds() / 60)
            if deadline and target
            else None
        )
        if deadline and target and target > deadline:
            exceeded.append(deadline)
    if exceeded:
        last_sector = planned.get("last_sector") or len(chain)
        first_deadline = min(exceeded)
        result["first_limited_segment"] = next(
            (
                index
                for index, sector in enumerate(chain[:last_sector], start=1)
                if (arrival := utc(sector.get("arrival_utc")))
                and arrival > first_deadline
            ),
            last_sector,
        )
    if blockers:
        result["issues"].append(
            "К моменту явки обязательный отдых текущего экипажа ещё не заканчивается."
        )
    if exceeded or blockers:
        result["status"] = "limited"
    elif len(active) == len(members) and all(
        any(ends[key] and utc(member.get(f"{key}_limit_utc")) for key in ends)
        for member in active
    ):
        result["status"] = "within_current_limits"
    else:
        result["status"] = "partial"
    return result


def _operating_today(chain, now):
    first = chain[0]
    departure = utc(first.get("departure_utc"))
    if departure is None or departure < now:
        return False
    airport = _airports().get(first["departure"], {})
    try:
        zone = ZoneInfo(airport.get("tz") or "UTC")
    except ZoneInfoNotFoundError:
        zone = UTC
    return now.astimezone(zone).date() == departure.astimezone(zone).date()


def _covered(schedule, start, end):
    if not start or not end:
        return False
    windows = schedule.get("coverage_windows")
    if windows is None:
        return schedule.get("complete") is True
    covered_until = start.date()
    for first, last in sorted(windows):
        first, last = date.fromisoformat(first), date.fromisoformat(last)
        if first > covered_until:
            return False
        if last >= end.date():
            return True
        covered_until = max(covered_until, last + DAY)
    return False


async def build_charter_matrix(
    request,
    fleet,
    schedule,
    *,
    saved_routes=(),
    airport_profiles=(),
    history=journey_history,
    route_lookup=None,
    crew_lookup=charter_crew,
    quote_ftl_lookup=None,
    current_crew_lookup=None,
    crew_operations=None,
    now=None,
):
    """Produce one operational comparison row for every tail in the active fleet."""
    now = now or datetime.now(UTC)
    schedule = schedule or {}
    survey = {row["registration"]: row for row in build_fleet(schedule, now)}
    profiles = _profiles(airport_profiles)
    routes = list(saved_routes)
    route_lookup = route_lookup or partial(
        charter_route_performance, saved_routes=routes
    )
    route_slots = asyncio.Semaphore(4)

    async def row(aircraft):
        tail = aircraft["registration"]
        aircraft_type = _by_type(aircraft)
        item = survey.get(tail, {})
        flights = item.get("flights", [])
        desk = _Desk(
            _RouteEstimator(aircraft_type, history, 0, now),
            tail,
            profiles,
            routes,
            route_lookup,
            route_slots,
        )
        legs = await _passenger_legs(desk, request, flights, now)
        first = legs[0]
        planned = utc(first["departure_utc"]) or _anchor(request["legs"][0])
        current, current_status = _position(flights, now, schedule)
        planned_station, projected_status = _position(flights, planned, schedule)
        if planned_station:
            projected_status = (
                "at_departure"
                if planned_station == first["departure"]
                else "ferry_required"
            )
        ferry = await _ferry(desk, planned_station, first, now, 0)
        for index, (previous, following) in enumerate(zip(legs, legs[1:]), start=1):
            ferry.extend(await _ferry(desk, previous["arrival"], following, now, index))
        chain = _flatten(ferry, legs)
        complete = _covered(
            schedule, utc(chain[0]["departure_utc"]), utc(chain[-1]["arrival_utc"])
        )
        blocks = (
            [
                block
                for block in crew_operations.aircraft_blocks
                if block.registration == tail and crew_operations.updated_at <= now
            ]
            if crew_operations
            else []
        )
        limitations = _schedule_status(flights, chain, complete, blocks)
        if current is None and current_status == "unconfirmed":
            limitations["issues"].append(
                "Confirm the current station for this tail against SKED."
            )
        if planned_station is None and projected_status == "unconfirmed":
            limitations["status"] = (
                "partial" if limitations["status"] == "clear" else limitations["status"]
            )
            limitations["issues"].append(
                "Confirm the station before the first requested departure to determine ferry need."
            )
        crew = await _crew_context(
            crew_operations, tail, aircraft_type, chain, flights, crew_lookup, now
        )
        if quote_ftl_lookup:
            forecast = await quote_ftl_lookup(aircraft, chain, now=now)
            if (
                planned_station is None
                and forecast.get("required_duty_minutes") is not None
            ):
                forecast = {
                    **forecast,
                    "issues": [
                        *forecast.get("issues", []),
                        "Начальный перегон не включён в duty: позиция борта к вылету не подтверждена.",
                    ],
                }
            crew["quote_ftl"] = forecast
            if current_crew_lookup and _operating_today(chain, now):
                crew["current_quote_ftl"] = _current_quote_ftl(
                    await current_crew_lookup(tail), chain, forecast, now
                )
        return {
            "registration": tail,
            "aircraft_type": aircraft_type,
            "position": {
                "current_station": current,
                "planned_station": planned_station,
                "current_status": current_status,
                "projected_status": projected_status,
                "as_of_utc": now.isoformat(),
                "planned_for_utc": planned.isoformat(),
            },
            "legs": legs,
            "ferry": ferry,
            "ferry_required": (
                True
                if ferry or (planned_station and planned_station != first["departure"])
                else False
                if planned_station
                else None
            ),
            "schedule": limitations,
            "crew": crew,
            "airports": _airport_checks(profiles, chain, now),
        }

    return {
        "aircraft": await asyncio.gather(*(row(aircraft) for aircraft in fleet)),
        "as_of_utc": now.isoformat(),
        "schedule_complete": schedule.get("complete") is True,
    }


def charter_opportunities(schedule, *, now=None):
    now = now or datetime.now(UTC)
    schedule = schedule or {}
    horizon = now + 7 * DAY
    empty_legs, open_windows = [], []
    complete = schedule.get("complete") is True
    for aircraft in build_fleet(schedule, now):
        tail, flights = aircraft["registration"], aircraft["flights"]
        for flight in flights:
            departure = utc(flight["departureUTC"])
            if flight.get("ferry") is True and now <= departure < horizon:
                empty_legs.append(
                    {
                        "registration": tail,
                        "aircraft_type": aircraft["type"],
                        **_schedule_leg(flight),
                    }
                )
        for index, previous in enumerate(flights):
            following = flights[index + 1] if index + 1 < len(flights) else None
            station = previous["arrival"]
            landed = utc(previous["arrivalUTC"])
            start = max(now, landed)
            end = min(horizon, utc(following["departureUTC"])) if following else horizon
            if (
                station
                and (not following or following["departure"] == station)
                and end - start >= DAY
                and _covered(schedule, landed, end)
            ):
                open_windows.append(
                    {
                        "registration": tail,
                        "station": station,
                        "start_utc": _iso(start),
                        "end_utc": _iso(end),
                        "minutes": int((end - start).total_seconds() / 60),
                    }
                )
    return {
        "empty_legs": sorted(
            empty_legs, key=lambda item: (item["departure_utc"], item["registration"])
        ),
        "open_windows": sorted(
            open_windows, key=lambda item: (item["start_utc"], item["registration"])
        ),
        "as_of_utc": now.isoformat(),
        "schedule_complete": complete,
    }
