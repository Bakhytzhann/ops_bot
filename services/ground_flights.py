import asyncio
import re
from collections import defaultdict
from datetime import UTC, datetime, timedelta

from httpx import HTTPError

from integrations.leon.operations import records
from integrations.leon.tools import call_leon, get_flight_schedule
from services.snapshot_models import utc
from services.source_cache import SourceCache

DISPLAY_WINDOW = timedelta(hours=6)
SCHEDULE_WINDOW = timedelta(hours=30)
WATCH_BATCH_SIZE = 25
WATCH_TIMEOUT = 18
WATCH_WORKERS = 2
CACHE_SECONDS = 45
WATCH_TIMES = {
    "etaIso": "estimated_landing",
    "etdIso": "estimated_out",
    "blonIso": "actual_in",
    "bloffIso": "actual_out",
    "ldgIso": "landing",
    "toIso": "takeoff",
    "ctotIso": "ctot",
    "tobt": "tobt",
    "tsat": "tsat",
}
SOURCE_ERRORS = (HTTPError, OSError, RuntimeError, TypeError, ValueError)
cache = SourceCache(8)


def _identifier(value):
    if type(value) is int and 0 < value < 10**18:
        return value
    if isinstance(value, str) and re.fullmatch(r"[1-9][0-9]{0,17}", value):
        return int(value)
    return None


def _code(value, pattern):
    if not isinstance(value, str):
        return None
    value = value.strip().upper()
    return value if re.fullmatch(pattern, value) else None


def _legs(data):
    rows = data.get("flights")
    if not isinstance(rows, list):
        raise TypeError("Unexpected Leon schedule response")
    complete = data.get("complete") is True
    legs = {}
    conflicts = set()
    for row in rows:
        if not isinstance(row, dict):
            complete = False
            continue
        if row.get("cancelled") is True or row.get("status") in (
            "OPTION",
            "OPPORTUNITY",
        ):
            continue
        nid = _identifier(row.get("flightNid"))
        departure = _code(row.get("departure"), r"[A-Z0-9]{4}")
        arrival = _code(row.get("arrival"), r"[A-Z0-9]{4}")
        planned_out = utc(row.get("departureUTC"))
        planned_in = utc(row.get("arrivalUTC"))
        if (
            not all((nid, departure, arrival, planned_out, planned_in))
            or planned_in <= planned_out
        ):
            complete = False
            continue
        leg = {
            "id": nid,
            "number": _code(row.get("flightNumber"), r"[A-Z0-9 -]{1,16}"),
            "departure": departure,
            "arrival": arrival,
            "planned_out": planned_out,
            "planned_in": planned_in,
            "registration": _code(row.get("aircraft"), r"[A-Z0-9-]{1,12}"),
            "aircraft_type": _code(row.get("aircraftType"), r"[A-Z0-9]{2,4}"),
        }
        if nid in legs and legs[nid] != leg:
            conflicts.add(nid)
        legs[nid] = leg
    for nid in conflicts:
        legs.pop(nid)
    return list(legs.values()), complete and not conflicts


def _watch_rows(data, ids):
    if isinstance(data, dict) and isinstance(data.get("result"), list):
        data = data["result"]
    selected = {}
    conflicts = set()
    for row in records(data):
        nid = _identifier(row.get("flightNid"))
        fields = row.get("flightWatch")
        if nid not in ids or not isinstance(fields, dict):
            continue
        watch = {
            target: value
            for field, target in WATCH_TIMES.items()
            if (value := utc(fields.get(field)))
        }
        if nid in selected and selected[nid] != watch:
            conflicts.add(nid)
        if watch:
            selected[nid] = watch
    for nid in conflicts:
        selected.pop(nid, None)
    return selected


async def _load_watch(legs, now):
    ids = [
        leg["id"]
        for leg in sorted(
            legs,
            key=lambda leg: min(
                abs(leg["planned_in"] - now), abs(leg["planned_out"] - now)
            ),
        )
    ]
    watch = {}
    batches = iter(range(0, len(ids), WATCH_BATCH_SIZE))
    failed = False

    async def load_batches():
        nonlocal failed
        for offset in batches:
            if failed:
                return
            batch = ids[offset : offset + WATCH_BATCH_SIZE]
            try:
                data = await call_leon(
                    "get-flight-watch-by-flight-nids", {"flightNids": batch}
                )
                watch.update(_watch_rows(data, batch))
            except (*SOURCE_ERRORS, TimeoutError):
                failed = True
                return

    try:
        async with asyncio.timeout(WATCH_TIMEOUT):
            await asyncio.gather(*(load_batches() for _ in range(WATCH_WORKERS)))
    except TimeoutError:
        return watch, False
    return watch, not failed and len(watch) == len(ids)


def _preceding_legs(legs):
    by_tail = defaultdict(list)
    for leg in legs:
        if registration := leg["registration"]:
            by_tail[registration.replace("-", "")].append(leg)
    previous = {}
    for rotation in by_tail.values():
        rotation.sort(key=lambda leg: (leg["planned_out"], leg["id"]))
        for inbound, outbound in zip(rotation, rotation[1:], strict=False):
            turnaround = outbound["planned_out"] - inbound["planned_in"]
            if (
                inbound["arrival"] == outbound["departure"]
                and timedelta(0) <= turnaround <= DISPLAY_WINDOW
            ):
                previous[outbound["id"]] = inbound
    return previous


def _observed(value, now):
    return value if value and value <= now else None


def _card(inbound, outbound, watches, now):
    inbound_watch = watches.get(inbound["id"], {}) if inbound else {}
    outbound_watch = watches.get(outbound["id"], {}) if outbound else {}
    inbound = inbound or {}
    outbound = outbound or {}
    station = outbound.get("departure") or inbound["arrival"]
    key = (
        f"{station}:o:{outbound['id']}" if outbound else f"{station}:i:{inbound['id']}"
    )
    card = {
        "key": key,
        "station": station,
        "registration": outbound.get("registration") or inbound.get("registration"),
        "aircraft_type": outbound.get("aircraft_type") or inbound.get("aircraft_type"),
        "inbound_id": inbound.get("id"),
        "outbound_id": outbound.get("id"),
        "inbound_number": inbound.get("number"),
        "outbound_number": outbound.get("number"),
        "outbound_arrival": outbound.get("arrival"),
        "planned_in": inbound.get("planned_in"),
        "planned_out": outbound.get("planned_out"),
        "estimated_in": None,
        "estimated_landing": inbound_watch.get("estimated_landing"),
        "estimated_out": outbound_watch.get("estimated_out"),
        "actual_in": _observed(inbound_watch.get("actual_in"), now),
        "actual_out": _observed(outbound_watch.get("actual_out"), now),
        "landing": _observed(inbound_watch.get("landing"), now),
        "takeoff": _observed(outbound_watch.get("takeoff"), now),
        "watch": {
            key: value
            for key, value in outbound_watch.items()
            if key in {"ctot", "tobt", "tsat"}
        },
    }
    return _classify(card, now)


def _classify(card, now):
    earliest, latest = now - DISPLAY_WINDOW, now + DISPLAY_WINDOW
    has_departure = card["outbound_id"] is not None
    phase = "departure" if has_departure else "arrival"
    finished = (
        (card["actual_out"] or card["takeoff"]) if has_departure else card["actual_in"]
    )
    departure = card["estimated_out"] or card["planned_out"]
    estimated_arrival = card["estimated_in"] or card["estimated_landing"]
    arrival = (
        card["actual_in"] or card["landing"] or estimated_arrival or card["planned_in"]
    )
    if finished:
        if finished < earliest:
            return None
        return {
            **card,
            "status": "recent",
            "phase": phase,
            "reference_time": finished,
            "time_basis": "actual",
        }
    if departure and earliest <= departure <= latest:
        return {
            **card,
            "status": "active",
            "phase": "departure",
            "reference_time": departure,
            "time_basis": "estimate" if card["estimated_out"] else "planned",
        }
    if arrival and earliest <= arrival <= latest and not card["actual_in"]:
        return {
            **card,
            "status": "active",
            "phase": "arrival",
            "reference_time": arrival,
            "time_basis": (
                "actual"
                if card["actual_in"] or card["landing"]
                else "estimate"
                if estimated_arrival
                else "planned"
            ),
        }
    if departure and departure < earliest and card["actual_in"]:
        return {
            **card,
            "status": "clarify",
            "phase": "departure",
            "reference_time": departure,
            "time_basis": "estimate" if card["estimated_out"] else "planned",
        }
    return None


def effective_ground_status(turn, local_out, now):
    status = turn["status"]
    reference = turn.get("reference_time")
    local_out = utc(local_out)
    out = turn.get("actual_out") or local_out
    if out and out <= now:
        status = "recent" if now - out <= timedelta(hours=6) else None
        reference = out
    if (
        status in {"active", "recent"}
        and reference is not None
        and abs(reference - now) > timedelta(hours=6)
    ):
        return None, reference
    return status, reference


async def load_ground_flights(*, now=None, refresh=False):
    now = now or datetime.now(UTC)
    if now.utcoffset() is None:
        raise ValueError("Ground Status requires aware UTC times")
    now = now.astimezone(UTC)
    start = (now - SCHEDULE_WINDOW).date().isoformat()
    end = (now + SCHEDULE_WINDOW).date().isoformat()

    async def load():
        data = await get_flight_schedule(start, end)
        legs, schedule_complete = _legs(data)
        return legs, {}, schedule_complete, True, datetime.now(UTC)

    legs, watch, schedule_complete, watch_complete, fetched_at = await cache.get(
        (start, end), CACHE_SECONDS, load, refresh=refresh, refresh_interval=5
    )
    incoming = _preceding_legs(legs) if schedule_complete else {}
    matched_inbound = {leg["id"] for leg in incoming.values()}
    cards = [
        card
        for leg in legs
        if (card := _card(incoming.get(leg["id"]), leg, watch, now))
    ]
    cards.extend(
        card
        for leg in legs
        if leg["id"] not in matched_inbound and (card := _card(leg, None, watch, now))
    )
    cards.sort(key=lambda card: (abs(card["reference_time"] - now), card["key"]))
    return {
        "cards": cards,
        "schedule_complete": schedule_complete,
        "watch_complete": watch_complete,
        "fetched_at": fetched_at,
    }
