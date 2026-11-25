import asyncio
import re
from datetime import UTC, datetime, timedelta

from httpx import HTTPError

from integrations.leon.operations import records
from integrations.leon.tools import call_leon, get_flight_crew
from services.flight_duty import (
    assigned_members,
    duration_value,
    flight_calculation,
    ftl_days,
    reporting_interval,
)
from services.ground_flights import _legs, _load_watch
from services.snapshot_models import utc

SOURCE_ERRORS = (HTTPError, OSError, RuntimeError, TypeError, ValueError)
CLOCK = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d")


def _stamp(value):
    return value.isoformat() if value is not None else None


def _tail(value):
    return value.strip().upper().replace("-", "") if isinstance(value, str) else ""


def _latest_actual(legs, watches, now):
    candidates = []
    for leg in legs:
        watch = watches.get(leg["id"], {})
        out = next(
            (
                watch[key]
                for key in ("actual_out", "takeoff")
                if key in watch and watch[key] <= now
            ),
            None,
        )
        arrival = next(
            (
                watch[key]
                for key in ("actual_in", "landing")
                if key in watch and watch[key] <= now
            ),
            None,
        )
        if out and arrival and arrival < out:
            continue
        if out or arrival:
            candidates.append((arrival or out, leg, watch, out, arrival))
    if not candidates:
        return None
    latest = max(entry[0] for entry in candidates)
    matches = [entry for entry in candidates if entry[0] == latest]
    return matches[0][1:] if len(matches) == 1 else None


def _position(leg, watch, out, arrival, now):
    if arrival and (out is None or arrival >= out):
        return leg["arrival"], "on_ground"
    if (watch.get("estimated_landing") or leg["planned_in"]) <= now:
        return None, "arrival_unconfirmed"
    if (takeoff := watch.get("takeoff")) is not None and takeoff <= now:
        return None, "airborne"
    return None, "departed"


def _clock(value):
    if not isinstance(value, str) or not CLOCK.fullmatch(value):
        return None
    hours, minutes = map(int, value.split(":"))
    return timedelta(hours=hours, minutes=minutes)


def _sector_bounds(sector):
    schedule = sector.get("schedule") or {}
    day = utc(schedule.get("date"))
    dep, arr = _clock(schedule.get("startTime")), _clock(schedule.get("endTime"))
    report = _clock((sector.get("reportOn") or {}).get("time"))
    if (
        day is None
        or dep is None
        or arr is None
        or report is None
        or schedule.get("cnl")
    ):
        return None
    day = day.replace(hour=0, minute=0, second=0, microsecond=0)
    beginning = day + report - (timedelta(days=1) if report > dep else timedelta())
    end = day + arr + (timedelta(days=1) if arr < dep else timedelta())
    return beginning, end


def _metric(calculation, name, limit_type):
    metric = calculation.get(name)
    if not isinstance(metric, dict) or metric.get("limitType") != limit_type:
        return None
    limit = metric.get("limit")
    return int(limit) if duration_value(limit) is not None and limit > 0 else None


def _elapsed(start, now):
    return int((now - start).total_seconds())


def _load_period(result, prefix, start, reference, limit):
    elapsed = _elapsed(start, reference)
    result[f"{prefix}_start_utc"] = _stamp(start)
    result[f"{prefix}_elapsed_seconds"] = elapsed
    result[f"{prefix}_limit_seconds"] = limit
    if limit is not None:
        result[f"{prefix}_remaining_seconds"] = limit - elapsed
        result[f"{prefix}_limit_utc"] = _stamp(start + timedelta(seconds=limit))


def _fdp_period(calculation):
    sectors = [s for s in calculation.get("fdpSectors", []) if s.get("isInFdp") is True]
    bounds = [_sector_bounds(sector) for sector in sectors]
    if not bounds or any(bound is None for bound in bounds):
        return None
    start = min(period[0] for period in bounds)
    end = max(period[1] for period in bounds)
    metric = calculation.get("fdpLength") or {}
    recorded = metric.get("actual")
    if duration_value(recorded) is None or abs(_elapsed(start, end) - recorded) > 60:
        return None
    return start, end


def _terminal_release(calculation, leg, watch, planned_release, now):
    on_block = watch.get("actual_in")
    if on_block is None or on_block > now:
        return None
    bounds = [
        (_sector_bounds(s), s.get("nid")) for s in calculation.get("fdpSectors", [])
    ]
    if not bounds or any(period is None for period, _ in bounds):
        return None
    last_end = max(period[1] for period, _ in bounds)
    final = [flight_id for period, flight_id in bounds if period[1] == last_end]
    if final != [leg["id"]] or planned_release < last_end:
        return None
    # Leon reports the post-flight interval. Anchor it to the observed on-block event.
    return on_block + (planned_release - last_end)


def _has_later_duty(evidence, calculation, release, now):
    for day in ftl_days(evidence):
        for other in day.get("ftlCalculations", []):
            if other is calculation:
                continue
            interval = reporting_interval(other)
            if interval and release <= interval[0] <= now < interval[1]:
                return True
        for row in day.get("rosterDuties", []):
            start, end = utc(row.get("startTimeIso")), utc(row.get("endTimeIso"))
            if (
                row.get("type") == "DUTY"
                and start
                and end
                and release <= start <= now < end
            ):
                return True
    return False


def _crew_status(result, evidence, leg, watch, phase, now):
    calculation = flight_calculation(evidence, leg["id"])
    interval = reporting_interval(calculation)
    if not interval or interval[0] > now:
        return result
    start, scheduled_release = interval
    actual_release = _terminal_release(calculation, leg, watch, scheduled_release, now)
    if actual_release is not None and actual_release < start:
        return result
    release = actual_release or scheduled_release
    minimum_rest = _metric(calculation, "restAfterDuty", "MIN")
    rest_until = (
        release + timedelta(seconds=minimum_rest)
        if actual_release and minimum_rest
        else None
    )
    if now < release or (actual_release is None and phase in {"departed", "airborne"}):
        status = "on_duty"
    elif (
        rest_until
        and now < rest_until
        and not _has_later_duty(evidence, calculation, release, now)
    ):
        status = "resting"
    else:
        return result

    result["status"] = status
    result["source"] = "Leon FTL"
    if status == "resting":
        result["rest_until_utc"] = _stamp(rest_until)
    next_eobt = utc(calculation.get("nextEOBT"))
    earliest = max(now, rest_until) if rest_until is not None else now
    result["next_eobt_utc"] = (
        _stamp(next_eobt) if next_eobt and next_eobt >= earliest else None
    )
    end = min(now, release) if actual_release else now
    _load_period(
        result, "duty", start, end, _metric(calculation, "dutyLimitedLength", "MAX")
    )
    fdp = _fdp_period(calculation)
    if fdp and fdp[0] <= end:
        actual_in = watch.get("actual_in")
        if actual_release and actual_in and fdp[0] <= actual_in <= end:
            end = actual_in
        _load_period(
            result, "fdp", fdp[0], end, _metric(calculation, "fdpLength", "MAX")
        )
    return result


async def _member_login(member, contact):
    nid = (member.get("login") or {}).get("loginNid")
    if type(nid) is int and nid > 0:
        return nid
    contact_id = contact.get("contactNid")
    if type(contact_id) is not int or contact_id <= 0:
        return None
    profiles = records(
        await call_leon(
            "get-contact-profiles-by-nid-list", {"contactsNids": [contact_id]}
        )
    )
    matches = [
        profile for profile in profiles if profile.get("contactNid") == contact_id
    ]
    if len(matches) != 1:
        return None
    nid = (matches[0].get("login") or {}).get("loginNid")
    return nid if type(nid) is int and nid > 0 else None


async def _member_status(member, leg, watch, phase, now, slots):
    contact = member.get("contact") or {}
    position = member.get("position") or member.get("primaryPosition")
    full_name = " ".join(
        str(contact.get(key) or "").strip() for key in ("name", "surname")
    ).strip()
    result = {
        "name": full_name or contact.get("knownAs"),
        "role": position.get("name") if isinstance(position, dict) else position,
        "status": "unconfirmed",
    }
    try:
        async with slots:
            async with asyncio.timeout(12):
                nid = await _member_login(member, contact)
                if nid is None:
                    return result
                start = min(leg["planned_out"], now) - timedelta(days=2)
                end = now + timedelta(days=2)
                evidence = await call_leon(
                    "get-crew-ftl-sheet",
                    {
                        "crewMember": nid,
                        "timeInterval": {
                            "start": start.strftime("%Y-%m-%dT00:00:00Z"),
                            "end": end.strftime("%Y-%m-%dT23:59:59Z"),
                        },
                    },
                )
        return _crew_status(result, evidence, leg, watch, phase, now)
    except SOURCE_ERRORS:
        return result


async def _operational_crew(leg, watch, out, arrival, phase, now, slots):
    flight = {
        "id": leg["id"],
        "number": leg["number"],
        "departure": leg["departure"],
        "arrival": leg["arrival"],
        "departure_utc": _stamp(out),
        "arrival_utc": _stamp(arrival),
    }
    result = {
        "status": "unconfirmed",
        "source": "Leon",
        "as_of_utc": _stamp(now),
        "flight": flight,
        "members": [],
    }
    try:
        async with slots:
            async with asyncio.timeout(12):
                crew = records((await get_flight_crew(leg["id"]))["crew"])
    except SOURCE_ERRORS:
        return {**result, "status": "unavailable", "reason": "assignment_unavailable"}
    assigned = [
        member
        for member in assigned_members(crew, leg["id"])
        if isinstance(member, dict)
    ]
    evaluated = await asyncio.gather(
        *(_member_status(member, leg, watch, phase, now, slots) for member in assigned)
    )
    # Names from a closed or stale leg cannot establish the current crew by themselves.
    current = phase in {"departed", "airborne"} or any(
        member["status"] != "unconfirmed" for member in evaluated
    )
    if current:
        statuses = {member["status"] for member in evaluated}
        result.update(
            status="mixed"
            if len(statuses) > 1
            else next(iter(statuses), "unconfirmed"),
            members=evaluated,
        )
    if not assigned:
        result["reason"] = "no_assignment_on_observed_flight"
    elif not current:
        result["reason"] = "current_duty_unconfirmed"
    elif any(member["status"] == "unconfirmed" for member in evaluated):
        result["reason"] = "member_ftl_incomplete"
    return result


async def current_fleet_crew(fleet, schedule, *, now=None):
    """Report FTL for the operational crew tied to observed movement of each tail."""
    now = now or datetime.now(UTC)
    fleet = list(fleet)
    by_tail = {_tail(aircraft.get("registration")): [] for aircraft in fleet}
    schedule = schedule or {"flights": []}
    legs, schedule_complete = _legs(schedule)
    for leg in legs:
        if (tail := _tail(leg["registration"])) in by_tail:
            by_tail[tail].append(leg)
    watches, watch_complete = await _load_watch(
        [leg for rows in by_tail.values() for leg in rows], now
    )
    slots = asyncio.Semaphore(4)

    async def aircraft_status(aircraft):
        registration = aircraft["registration"]
        position = {"current_station": None, "current_status": "unconfirmed"}
        result = {
            "registration": registration,
            "aircraft_type": aircraft.get("aircraft_type") or aircraft.get("type"),
            "current_station": None,
            "position": position,
        }
        actual = _latest_actual(by_tail[_tail(registration)], watches, now)
        if actual is None:
            if not schedule_complete:
                reason = "incomplete_schedule"
            elif not watch_complete:
                reason = "incomplete_flight_watch"
            else:
                reason = "no_observed_movement"
            return {
                **result,
                "crew": {
                    "status": "unconfirmed" if schedule_complete else "unavailable",
                    "source": "Leon",
                    "as_of_utc": _stamp(now),
                    "flight": None,
                    "members": [],
                    "reason": reason,
                },
            }
        leg, watch, out, arrival = actual
        station, phase = _position(leg, watch, out, arrival, now)
        position.update(current_station=station, current_status=phase)
        crew = await _operational_crew(leg, watch, out, arrival, phase, now, slots)
        return {**result, "current_station": station, "crew": crew}

    return {
        "aircraft": await asyncio.gather(
            *(aircraft_status(aircraft) for aircraft in fleet)
        ),
        "as_of_utc": _stamp(now),
        "complete": schedule_complete and watch_complete,
    }
