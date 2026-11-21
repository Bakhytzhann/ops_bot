import asyncio
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from time import monotonic
from urllib.parse import urlsplit

from httpx import HTTPError
from loguru import logger

from integrations.leon.tools import call_leon, get_flight_schedule
from services.analytics_report import load_analytics
from services.charter_intelligence import list_charter_requests, load_charter_request
from services.fleet_operations import (
    build_fleet,
    load_position_notes,
    station_for_aircraft,
)
from services.ground_flights import effective_ground_status, load_ground_flights
from services.ground_timing import station_samples, turn_timing
from services.ground_turns import ground_turns
from services.journey_history import journey_history, load_journey_insights
from services.snapshot_models import utc
from services.source_cache import SourceCache
from services.station_notes import notes

SOURCE_ERRORS = (HTTPError, OSError, RuntimeError, ValueError, TypeError, sqlite3.Error)
STATION_METRICS = {
    "Заправка": "Fuel uplift",
    "Ожидание ТЗ": "Fuel truck wait",
    "Посадка пассажиров": "Boarding",
    "Высадка пассажиров": "Deplaning",
    "Время на земле": "On ground",
}


def source_state(stamp, ttl, *, complete=True):
    stamp = stamp if isinstance(stamp, datetime) else utc(stamp)
    return {
        "status": "READY" if complete else "PARTIAL",
        "fetched_at": stamp,
        "fresh_until": stamp + timedelta(seconds=ttl) if stamp else None,
    }


def _records(result):
    if isinstance(result, dict) and not result.get("error"):
        result = result.get("result")
    if not isinstance(result, list) or any(
        not isinstance(row, dict) or row.get("error") for row in result
    ):
        raise ValueError("Unexpected Leon records")
    return result


def _text(value, limit=160):
    return value.strip()[:limit] if isinstance(value, str) else None


def _file_link(value):
    if not isinstance(value, str) or any(char.isspace() for char in value):
        return None
    try:
        url = urlsplit(value)
        if (
            url.scheme == "https"
            and url.hostname
            and not url.username
            and not url.password
        ):
            return value
    except ValueError:
        return None
    return None


def _leg(row):
    if row is None:
        return None
    return {
        "id": row.get("flightNid"),
        "number": row.get("flightNumber"),
        "departure": row.get("departure"),
        "arrival": row.get("arrival"),
        "std": utc(row.get("departureUTC")),
        "sta": utc(row.get("arrivalUTC")),
        "ferry": row.get("ferry") is True,
    }


class OpsViews:
    def __init__(self):
        self.cache = SourceCache(64)
        self.slots = asyncio.Semaphore(3)
        self.analytics_slot = asyncio.Semaphore(1)

    async def read(self, key, ttl, loader, *, refresh=False, timeout=22, stale=True):
        async def load():
            async with asyncio.timeout(timeout):
                async with self.slots:
                    return datetime.now(UTC), await loader()

        try:
            stamp, data = await self.cache.get(
                key, ttl, load, refresh=refresh, refresh_interval=5
            )
        except SOURCE_ERRORS as exc:
            logger.warning("OPS source {} unavailable ({})", key[0], type(exc).__name__)
            prior = self.cache.peek(key) if stale else None
            if prior and monotonic() - prior[0] < max(90, ttl * 5):
                stamp, data = prior[1]
                return {**data, **source_state(stamp, ttl), "status": "STALE"}
            return {"status": "UNAVAILABLE", "fetched_at": None, "fresh_until": None}
        return {**data, **source_state(stamp, ttl, complete=data.get("complete", True))}

    async def ground(self, *, refresh=False):
        now = datetime.now(UTC)
        try:
            async with asyncio.timeout(25):
                async with self.slots:
                    snapshot = await load_ground_flights(now=now, refresh=refresh)
            cards = snapshot["cards"]
            keys = [turn["key"] for turn in cards]
            outs, markers = await asyncio.gather(
                asyncio.to_thread(ground_turns.recent_outs, keys),
                asyncio.to_thread(ground_turns.current_markers, keys),
            )
        except SOURCE_ERRORS as exc:
            logger.warning("Ground Status unavailable ({})", type(exc).__name__)
            return {
                "status": "UNAVAILABLE",
                "fetched_at": None,
                "fresh_until": None,
                "cards": [],
            }
        items = []
        for turn in cards:
            status, reference = effective_ground_status(
                turn, outs.get(turn["key"]), now
            )
            if status:
                items.append(
                    {
                        **turn,
                        "status": status,
                        "reference_time": reference,
                        "blockers": markers.get(turn["key"], {}).get("blockers", []),
                        "ready_at": markers.get(turn["key"], {}).get("ready_at")
                        if status == "active"
                        else None,
                    }
                )
        return {
            **source_state(
                snapshot["fetched_at"], 45, complete=snapshot["schedule_complete"]
            ),
            "cards": sorted(items, key=lambda turn: abs(turn["reference_time"] - now)),
        }

    async def ground_detail(self, turn, state):
        def detail():
            events = ground_turns.events(turn["key"])
            timing = turn_timing(
                turn, events, baseline=ground_turns.baseline(turn["key"])
            )
            public_events = [
                {
                    field: event[field]
                    for field in ("id", "kind", "event_at", "source", "detail")
                }
                for event in events
            ]
            return {
                **{key: state[key] for key in ("status", "fetched_at", "fresh_until")},
                "turn": turn,
                "events": public_events,
                "timing": timing,
            }

        return await asyncio.to_thread(detail)

    async def station(self, station):
        try:
            report = await asyncio.to_thread(ground_turns.station_report, station)
        except (OSError, sqlite3.Error):
            return {"status": "UNAVAILABLE", "fetched_at": None, "fresh_until": None}
        return {
            **source_state(datetime.now(UTC), 45),
            **report,
            "metrics": {
                STATION_METRICS[key]: sample
                for key, sample in report["metrics"].items()
                if key in STATION_METRICS
            },
        }

    async def ground_history(self, turn):
        try:
            samples, blocker = await asyncio.to_thread(
                station_samples,
                turn["station"],
                turn["aircraft_type"],
                exclude_turn=turn["key"],
                store=ground_turns,
            )
        except (OSError, sqlite3.Error):
            return {"status": "UNAVAILABLE", "fetched_at": None, "fresh_until": None}
        return {
            **source_state(datetime.now(UTC), 45),
            "samples": samples,
            "blocker": blocker,
        }

    async def fleet(self, brief, user_id, *, refresh=False):
        now = datetime.now(UTC)
        start = (now - timedelta(days=21)).date().isoformat()
        end = (now + timedelta(days=7)).date().isoformat()

        async def load():
            schedule = await get_flight_schedule(start, end)
            if not isinstance(schedule, dict) or not isinstance(
                schedule.get("flights"), list
            ):
                raise TypeError("Leon schedule unavailable")
            return {"schedule": schedule, "complete": schedule.get("complete") is True}

        data = await self.read(
            ("fleet", start, end), 120, load, refresh=refresh, timeout=55
        )
        schedule = data.pop("schedule", None)
        if schedule is None:
            return data
        if not brief:
            first = (now - timedelta(days=2)).date()
            schedule = {
                "flights": [
                    row
                    for row in schedule["flights"]
                    if isinstance(row, dict)
                    and (stamp := utc(row.get("departureUTC")))
                    and stamp.date() >= first
                ]
            }
        fleet = build_fleet(schedule, now)
        access, station_notes = "unbound", {}
        if brief:
            try:
                access, station_notes = await load_position_notes(
                    fleet, user_id, now, notes
                )
            except (OSError, sqlite3.Error):
                access = "unavailable"
            if (access == "unavailable" or None in station_notes.values()) and data[
                "status"
            ] == "READY":
                data["status"] = "PARTIAL"
        return {
            **data,
            "notes_access": access,
            "fleet": [
                {
                    "registration": item["registration"],
                    "type": item["type"],
                    "active": _leg(item["active"]),
                    "last": _leg(item["last"]),
                    "next": _leg(item["next"]),
                    "station": station,
                    "notes": None
                    if rows is None
                    else [
                        {
                            "text": row["text"],
                            "observed_at": row["observed_at_utc"],
                            "verified": bool(row.get("verified_by")),
                            "pinned": bool(row.get("pinned")),
                            "older": row["older"],
                        }
                        for row in rows
                    ],
                }
                for item in fleet
                for station in [station_for_aircraft(item)]
                for rows in [station_notes.get(station, [])]
            ],
        }

    async def aircraft(self, *, refresh=False):
        async def load():
            rows = _records(await call_leon("get-fleet", {"includeGndAircraft": False}))
            return {
                "aircraft": sorted(
                    [
                        {
                            "id": row["aircraftNid"],
                            "registration": row["registration"].strip().upper(),
                            "type": _text(row.get("acftTypeName"), 40),
                        }
                        for row in rows
                        if type(row.get("aircraftNid")) is int
                        and isinstance(row.get("registration"), str)
                        and re.fullmatch(
                            r"[A-Z0-9-]{1,12}", row["registration"].strip().upper()
                        )
                    ],
                    key=lambda row: row["registration"],
                )
            }

        return await self.read(("document fleet",), 300, load, refresh=refresh)

    async def documents(self, aircraft_id, *, refresh=False):
        fleet = await self.aircraft()

        async def load():
            if fleet["status"] == "UNAVAILABLE":
                raise RuntimeError("Leon fleet unavailable")
            aircraft = next(
                (row for row in fleet["aircraft"] if row["id"] == aircraft_id), None
            )
            if aircraft is None:
                return {"aircraft": None, "documents": []}
            documents = []
            complete = True
            for offset in range(0, 100, 20):
                rows = _records(
                    await call_leon(
                        "get-aircraft-documents",
                        {
                            "aircraftNidList": [aircraft_id],
                            "limit": 20,
                            "offset": offset,
                        },
                    )
                )
                for row in rows:
                    if row.get("aircraftNid") != aircraft_id:
                        complete = False
                        continue
                    registration = _text(row.get("aircraftRegistration"))
                    if (
                        registration
                        and registration.upper() != aircraft["registration"]
                    ):
                        complete = False
                        continue
                    files = [
                        {"name": _text(file.get("fileName")), "url": url}
                        for file in (row.get("files") or [])
                        if isinstance(file, dict)
                        and (url := _file_link(file.get("signedUrl")))
                    ]
                    documents.append(
                        {
                            "title": _text(row.get("documentName")),
                            "type": _text(row.get("fleetDocumentType")),
                            "expiry": _text(row.get("expireDate"), 30),
                            "never_expires": row.get("neverExpires") is True,
                            "files": files,
                        }
                    )
                if len(rows) < 20:
                    break
            else:
                complete = False
            return {"aircraft": aircraft, "documents": documents, "complete": complete}

        return await self.read(
            ("aircraft documents", aircraft_id), 45, load, refresh=refresh, stale=False
        )

    async def operator(self, *, refresh=False):
        async def load():
            data = await call_leon("get-aoc-list", {})
            entries = data
            if isinstance(data, dict) and isinstance(data.get("operator"), dict):
                entries = data["operator"].get("aocList")
            rows = _records(entries)
            return {
                "certificates": [
                    {
                        "name": _text(row.get("name") or row.get("aocName")),
                        "number": _text(row.get("aocNumber") or row.get("number")),
                        "code": _text(row.get("aocCode") or row.get("code")),
                    }
                    for row in rows
                ]
            }

        return await self.read(("operator",), 300, load, refresh=refresh)

    async def analytics(self, period, *, refresh=False):
        async def load():
            async with self.analytics_slot:
                result = await load_analytics(period)
            scalar_fields = (
                "start",
                "end",
                "legs",
                "cancelled",
                "unassigned",
                "excluded",
                "minutes",
                "known_durations",
                "average",
                "median",
                "ferry_count",
                "nonferry_count",
                "unknown_ferry",
                "ferry_minutes",
                "nonferry_minutes",
                "ferry_missing_duration",
                "idle_minutes",
                "idle_72h",
                "complete",
                "total_windows",
                "received_windows",
                "complete_windows",
            )
            ranking = {}
            for key in (
                "tails",
                "routes",
                "destinations",
                "ferry_routes",
                "idle_by_tail",
            ):
                ranking[key] = [
                    {
                        "label": " → ".join(label)
                        if isinstance(label, tuple)
                        else label,
                        "value": count,
                    }
                    for label, count in result[key].most_common(8)
                ]
            return {
                **{key: result[key] for key in scalar_fields},
                **ranking,
                "hours_by_tail": result["hours_by_tail"],
                "hours_by_month": result["hours_by_month"],
                "legs_by_month": result["legs_by_month"],
                "idle_count": len(result["idle"]),
                "overlap_count": len(result["overlaps"]),
                "position_mismatch_count": len(result["position_mismatches"]),
                "turn_count": len(result["turns"]),
            }

        return await self.read(
            ("analytics", period), 180, load, refresh=refresh, timeout=125
        )

    async def journey(self, days, departure, arrival, aircraft_type, *, refresh=False):
        end = datetime.now(UTC).date() - timedelta(days=1)
        start = end - timedelta(days=days - 1)

        async def load():
            return await load_journey_insights(start, end, refresh=refresh)

        data = await self.read(
            ("journey", days, end), 180, load, refresh=refresh, timeout=115
        )
        if data["status"] != "UNAVAILABLE" and (departure or arrival or aircraft_type):
            try:
                result = await asyncio.to_thread(
                    journey_history.summary,
                    start,
                    end,
                    departure=departure,
                    arrival=arrival,
                    aircraft_type=aircraft_type,
                )
            except (OSError, sqlite3.Error):
                return {
                    "status": "UNAVAILABLE",
                    "fetched_at": None,
                    "fresh_until": None,
                }
            data.update(result)
        for key in ("routes", "airports", "aircraft_types"):
            if key in data:
                data[key] = data[key][:12]
        return data

    async def charter_sales(self, rfq_id=None, *, refresh=False):
        async def load():
            if rfq_id is not None:
                return await load_charter_request(rfq_id)
            return {"requests": await list_charter_requests()}

        return await self.read(
            ("sales", rfq_id), 90, load, refresh=refresh, stale=False
        )
