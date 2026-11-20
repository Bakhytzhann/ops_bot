"""Scheduled fleet position and station-note selection shared by both OPS clients."""

import asyncio
import re
from datetime import UTC, datetime, timedelta

from config.settings import settings
from services.snapshot_models import utc

NOTE_LOOKBACK_DAYS = 30
MAX_NOTES_PER_STATION = 4
AIRPORT_PATTERN = re.compile(r"[A-Z]{4}")


def charter_sales_access(user_id):
    return user_id in settings.admin_ids or str(user_id) in {
        part.strip() for part in settings.charter_sales_user_ids.split(",")
    }


def build_fleet(schedule, now=None):
    """Group complete, non-cancelled Leon schedule by actual registration."""
    now = now or datetime.now(UTC)
    groups = {}
    for flight in schedule.get("flights") or []:
        if not isinstance(flight, dict) or flight.get("cancelled") is True:
            continue
        registration = flight.get("aircraft")
        departure = utc(flight.get("departureUTC"))
        arrival = utc(flight.get("arrivalUTC"))
        if not isinstance(registration, str) or not registration.strip():
            continue
        if not departure or not arrival or arrival <= departure:
            continue
        groups.setdefault(registration.strip().upper(), []).append(flight)
    result = []
    for registration, flights in sorted(groups.items()):
        flights.sort(key=lambda f: utc(f["departureUTC"]))
        active = next(
            (
                f
                for f in flights
                if utc(f["departureUTC"]) <= now < utc(f["arrivalUTC"])
            ),
            None,
        )
        previous = [f for f in flights if utc(f["arrivalUTC"]) <= now]
        upcoming = [f for f in flights if utc(f["departureUTC"]) > now]
        last = max(previous, key=lambda f: utc(f["arrivalUTC"])) if previous else None
        next_flight = (
            min(upcoming, key=lambda f: utc(f["departureUTC"])) if upcoming else None
        )
        result.append(
            {
                "registration": registration,
                "type": next(
                    (str(f["aircraftType"]) for f in flights if f.get("aircraftType")),
                    "—",
                ),
                "active": active,
                "last": last,
                "next": next_flight,
                "flights": flights,
            }
        )
    return result


def station_for_aircraft(item):
    """Only the latest past scheduled arrival, not a guessed future location."""
    if item["active"] or item["last"] is None:
        return None
    station = item["last"].get("arrival")
    if not isinstance(station, str) or not AIRPORT_PATTERN.fullmatch(station):
        return None
    return station


def current_notes(rows, now):
    """Unarchived notes from 30 days; pinned older notes remain visibly dated."""
    cutoff = now - timedelta(days=NOTE_LOOKBACK_DAYS)
    selected = []
    for row in rows:
        if row.get("status") != "active":
            continue
        observed = utc(row.get("observed_at_utc"))
        if observed is None or observed > now:
            continue
        older = observed < cutoff
        if older and not row.get("pinned"):
            continue
        selected.append({**row, "older": older})
    return selected[:MAX_NOTES_PER_STATION]


async def load_position_notes(fleet, user_id, now, repository):
    stations = sorted(
        {station for item in fleet if (station := station_for_aircraft(item))}
    )
    role = await asyncio.to_thread(repository.role, user_id)
    notes_access = "ready" if role else "unbound"
    station_notes = {}
    if role:
        slots = asyncio.Semaphore(5)

        async def get_station(station):
            async with slots:
                try:
                    rows = await asyncio.to_thread(
                        repository.list, user_id, station, 20, 0
                    )
                    return station, current_notes(rows, now)
                except (ValueError, PermissionError, OSError):
                    return station, None

        station_notes = dict(
            await asyncio.gather(*(get_station(station) for station in stations))
        )
    return notes_access, station_notes
