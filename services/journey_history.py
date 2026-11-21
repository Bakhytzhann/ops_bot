import asyncio
import re
import sqlite3
from collections import Counter, defaultdict
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from statistics import mean, median

from httpx import HTTPError

from config.settings import settings
from integrations.leon.tools import get_flight_history
from services.schedule_dates import MAX_SCHEDULE_DAYS
from services.snapshot_models import utc
from services.source_cache import SourceCache

SCHEMA = """
    CREATE TABLE IF NOT EXISTS journey_flights (
        operator TEXT NOT NULL, flight_nid INTEGER NOT NULL, planned_out TEXT NOT NULL,
        planned_in TEXT, planned_departure TEXT, planned_arrival TEXT,
        aircraft TEXT, aircraft_type TEXT, ferry INTEGER, synced_at TEXT NOT NULL,
        PRIMARY KEY (operator, flight_nid)
    );
    CREATE INDEX IF NOT EXISTS journey_flights_window
        ON journey_flights (operator, planned_out);
    CREATE TABLE IF NOT EXISTS journey_actual (
        operator TEXT NOT NULL, flight_nid INTEGER NOT NULL, log_status TEXT NOT NULL,
        departure TEXT, arrival TEXT, aircraft TEXT, aircraft_type TEXT,
        block_out TEXT, takeoff TEXT, landing TEXT, block_in TEXT,
        fuel_burn REAL, fuel_unit TEXT, fuel_uplift REAL, uplift_unit TEXT,
        distance_nm REAL, synced_at TEXT NOT NULL,
        PRIMARY KEY (operator, flight_nid),
        FOREIGN KEY (operator, flight_nid)
            REFERENCES journey_flights (operator, flight_nid) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS journey_sync_days (
        operator TEXT NOT NULL, day TEXT NOT NULL, complete INTEGER NOT NULL,
        synced_at TEXT NOT NULL, PRIMARY KEY (operator, day)
    );
"""

UPSERT_FLIGHT = """
    INSERT INTO journey_flights VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(operator, flight_nid) DO UPDATE SET
        planned_out=excluded.planned_out, planned_in=excluded.planned_in,
        planned_departure=excluded.planned_departure,
        planned_arrival=excluded.planned_arrival, aircraft=excluded.aircraft,
        aircraft_type=excluded.aircraft_type, ferry=excluded.ferry,
        synced_at=excluded.synced_at
    WHERE excluded.synced_at >= journey_flights.synced_at
"""

UPSERT_ACTUAL = """
    INSERT INTO journey_actual VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(operator, flight_nid) DO UPDATE SET
        log_status=excluded.log_status, departure=excluded.departure, arrival=excluded.arrival,
        aircraft=excluded.aircraft, aircraft_type=excluded.aircraft_type,
        block_out=excluded.block_out, takeoff=excluded.takeoff,
        landing=excluded.landing, block_in=excluded.block_in,
        fuel_burn=excluded.fuel_burn, fuel_unit=excluded.fuel_unit,
        fuel_uplift=excluded.fuel_uplift, uplift_unit=excluded.uplift_unit,
        distance_nm=excluded.distance_nm, synced_at=excluded.synced_at
"""

FUEL_UNITS = {"KG": "KG", "LB": "LB", "LBS": "LB", "L": "L", "USGAL": "USGAL"}
MAX_HISTORY_DAYS = 366
MAX_REFRESH_DAYS = 90
INSIGHTS_SYNC_TIMEOUT = 110
INSIGHTS_SOURCE_ERRORS = (HTTPError, OSError, RuntimeError, TypeError, ValueError)
RECENT_CACHE_DAYS = 2
RECENT_CACHE_TTL = timedelta(hours=2)


def _code(value, pattern):
    if isinstance(value, str):
        value = value.strip().upper()
        if re.fullmatch(pattern, value):
            return value
    return None


def _icao(value):
    return _code(value, r"[A-Z]{4}")


def _airport_icao(airport):
    if not isinstance(airport, dict):
        return None
    code = airport.get("code")
    return _icao(code.get("icao") if isinstance(code, dict) else airport.get("icao"))


def _aircraft(aircraft):
    if not isinstance(aircraft, dict):
        return None, None
    kind = aircraft.get("acftType")
    return (
        _code(aircraft.get("registration"), r"[A-Z0-9-]{1,12}"),
        _code(kind.get("icao"), r"[A-Z0-9]{2,4}") if isinstance(kind, dict) else None,
    )


def _timestamp(value, *, utc_field=False):
    if type(value) is int and 946684800 <= value <= 4102444800:
        return datetime.fromtimestamp(value, UTC).isoformat()
    if utc_field and isinstance(value, str):
        try:
            stamp = datetime.fromisoformat(value)
        except ValueError:
            return None
        if "T" in value:
            return stamp.replace(tzinfo=stamp.tzinfo or UTC).astimezone(UTC).isoformat()
    stamp = utc(value)
    return stamp.isoformat() if stamp else None


def _quantity(value, *, allow_zero=False):
    if type(value) in {int, float} and 0 <= value < 1e9 and (value or allow_zero):
        return value
    return None


def _unit(value):
    return FUEL_UNITS.get(_code(value, r"[A-Z]{1,5}"))


def _log_values(flight, now):
    log = flight.get("journeyLog")
    empty = (None,) * 13
    if (
        flight.get("cancelled") is True
        or flight.get("journeyLogCancelled") is True
        or (isinstance(log, dict) and log.get("isCnl") is True)
    ):
        return ("cancelled", *empty)
    if "journeyLog" not in flight:
        return None
    if log is None:
        return ("missing", *empty)
    if not isinstance(log, dict) or not log:
        return None
    if log.get("isClosed") is False:
        return ("open", *empty)
    registration, kind = _aircraft(log.get("acft"))
    block_out = _timestamp(log.get("bloffUTC"), utc_field=True) or _timestamp(
        log.get("startTime")
    )
    takeoff = _timestamp(log.get("toTimeUTC"), utc_field=True) or _timestamp(
        log.get("atd")
    )
    landing = _timestamp(log.get("ldgTimeUTC"), utc_field=True) or _timestamp(
        log.get("ata")
    )
    block_in = _timestamp(log.get("blonUTC"), utc_field=True) or _timestamp(
        log.get("endTime")
    )
    times = [stamp for stamp in (block_out, takeoff, landing, block_in) if stamp]
    if times and (times != sorted(times) or times[-1] > now):
        block_out = takeoff = landing = block_in = None
    departure = _airport_icao(log.get("startAirport"))
    arrival = _airport_icao(log.get("endAirport"))
    fuel_burn = _quantity(log.get("usedFuel"))
    fuel_uplift = _quantity(log.get("refuel"), allow_zero=True)
    distance = _quantity(log.get("distance"))
    if not any(
        value is not None
        for value in (
            departure,
            arrival,
            block_out,
            block_in,
            takeoff,
            landing,
            fuel_burn,
            fuel_uplift,
            distance,
        )
    ):
        return None
    return (
        "reported",
        departure,
        arrival,
        registration,
        kind or _code(log.get("acftTypeId"), r"[A-Z0-9]{2,4}"),
        block_out,
        takeoff,
        landing,
        block_in,
        fuel_burn,
        _unit(log.get("fuelUnit")),
        fuel_uplift,
        _unit(log.get("refuelUnit")),
        distance,
    )


def _duration(start, end):
    if not start or not end:
        return None
    minutes = (
        datetime.fromisoformat(end) - datetime.fromisoformat(start)
    ).total_seconds() / 60
    return minutes if 0 < minutes < 24 * 60 else None


def _bounds(start, end, max_days):
    if type(start) is not date or type(end) is not date:
        raise TypeError("Journey History requires UTC calendar dates")
    days = (end - start).days + 1
    if not 1 <= days <= max_days:
        raise ValueError("Invalid Journey History period")
    return days


def _completed_period(start, end, max_days):
    _bounds(start, end, max_days)
    if end >= datetime.now(UTC).date():
        raise ValueError("Journey History refresh requires completed UTC days")


def _windows(start, end, covered=()):
    for stop in (*sorted(covered), end + timedelta(days=1)):
        while start < stop:
            last = min(
                start + timedelta(days=MAX_SCHEDULE_DAYS - 1),
                stop - timedelta(days=1),
            )
            yield start, last
            start = last + timedelta(days=1)
        start = stop + timedelta(days=1)


def _stats(values):
    return {
        "count": len(values),
        "total": round(sum(values), 2) if values else None,
        "mean": round(mean(values), 2) if values else None,
        "median": round(median(values), 2) if values else None,
    }


def _fuel_stats(rows, value_key, unit_key):
    grouped = defaultdict(list)
    for row in rows:
        amount, unit = row[value_key], row[unit_key]
        if amount is None or unit is None:
            continue
        if unit == "LB":
            unit, amount = "KG", amount * 0.45359237
        grouped[unit].append(amount)
    return {unit: _stats(grouped[unit]) for unit in sorted(grouped)}


def _report(rows):
    statuses = Counter(row["log_status"] for row in rows)
    actual = [row for row in rows if row["log_status"] == "reported"]
    routes = Counter(
        (row["departure"], row["arrival"])
        for row in actual
        if row["departure"] and row["arrival"]
    )
    airports = Counter(
        airport
        for row in actual
        for airport in {row["departure"], row["arrival"]}
        if airport
    )
    kinds = Counter(row["aircraft_type"] for row in actual if row["aircraft_type"])
    block = [_duration(row["block_out"], row["block_in"]) for row in actual]
    airborne = [_duration(row["takeoff"], row["landing"]) for row in actual]
    planned_block = [_duration(row["planned_out"], row["planned_in"]) for row in actual]
    return {
        "scheduled_count": len(rows),
        "actual_count": len(actual),
        "known_log_count": statuses["reported"] + statuses["open"],
        "missing_log_count": statuses["missing"],
        "open_log_count": statuses["open"],
        "unknown_log_count": statuses[None],
        "cancelled_count": statuses["cancelled"],
        "reported_ferry_count": sum(row["ferry"] == 1 for row in actual),
        "reported_unknown_ferry_count": sum(row["ferry"] is None for row in actual),
        "route_aircraft_count": sum(
            bool(row["departure"] and row["arrival"] and row["aircraft_type"])
            for row in actual
        ),
        "routes": [
            {"departure": dep, "arrival": arr, "count": count}
            for (dep, arr), count in sorted(
                routes.items(), key=lambda pair: (-pair[1], pair[0])
            )
        ],
        "airports": [
            {"icao": icao, "count": count}
            for icao, count in sorted(
                airports.items(), key=lambda pair: (-pair[1], pair[0])
            )
        ],
        "aircraft_types": [
            {"type": kind, "count": count}
            for kind, count in sorted(
                kinds.items(), key=lambda pair: (-pair[1], pair[0])
            )
        ],
        "block_minutes": _stats([minutes for minutes in block if minutes is not None]),
        "planned_block_minutes": _stats(
            [
                minutes
                for row in rows
                if row["log_status"] != "cancelled"
                and (minutes := _duration(row["planned_out"], row["planned_in"]))
                is not None
            ]
        ),
        "block_delta_minutes": _stats(
            [
                actual_minutes - planned_minutes
                for actual_minutes, planned_minutes in zip(
                    block, planned_block, strict=True
                )
                if actual_minutes is not None and planned_minutes is not None
            ]
        ),
        "airborne_minutes": _stats(
            [minutes for minutes in airborne if minutes is not None]
        ),
        "distance_nm": _stats(
            [row["distance_nm"] for row in actual if row["distance_nm"] is not None]
        ),
        "fuel": _fuel_stats(actual, "fuel_burn", "fuel_unit"),
        "fuel_uplift": _fuel_stats(actual, "fuel_uplift", "uplift_unit"),
        "fuel_missing_unit_count": sum(
            row["fuel_burn"] is not None and not row["fuel_unit"] for row in actual
        ),
    }


class JourneyHistory:
    def __init__(self, path=None):
        self.path = path

    def _connect(self):
        path = self.path or settings.database_path
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, timeout=10)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.executescript(SCHEMA)
        except sqlite3.Error:
            conn.close()
            raise
        return conn

    @staticmethod
    def _operator():
        return settings.leon_operator.strip().lower()

    def complete_days(self, start, end):
        now = datetime.now(UTC)
        recent_start = now.date() - timedelta(days=RECENT_CACHE_DAYS)
        freshness_cutoff = now - RECENT_CACHE_TTL
        with closing(self._connect()) as conn:
            return {
                date.fromisoformat(row["day"])
                for row in conn.execute(
                    """
                    SELECT day FROM journey_sync_days
                    WHERE operator = ? AND day BETWEEN ? AND ? AND complete = 1
                        AND (day < ? OR synced_at >= ?)
                    """,
                    (
                        self._operator(),
                        start.isoformat(),
                        end.isoformat(),
                        recent_start.isoformat(),
                        freshness_cutoff.isoformat(),
                    ),
                )
            }

    def ingest(self, start, end, result, *, fetched_at):
        days = _bounds(start, end, MAX_SCHEDULE_DAYS)
        if not isinstance(result, dict) or not isinstance(result.get("flights"), list):
            raise TypeError("Leon flight history missing")
        operator = self._operator()
        timestamp = fetched_at.astimezone(UTC).isoformat()
        schedule_complete = (
            result.get("schedule_complete", result.get("complete")) is True
        )
        details_complete = result.get("complete") is True
        unknown_days = set()
        first, last = start.isoformat(), (end + timedelta(days=1)).isoformat()
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            newer_days = {
                row["day"]
                for row in conn.execute(
                    """
                    SELECT day FROM journey_sync_days
                    WHERE operator = ? AND day >= ? AND day < ? AND synced_at > ?
                    """,
                    (operator, first, last, timestamp),
                )
            }
            for flight in result["flights"]:
                if not isinstance(flight, dict):
                    schedule_complete = False
                    continue
                nid = flight.get("flightNid")
                planned_out = _timestamp(flight.get("departureUTC"), utc_field=True)
                if type(nid) is not int or nid <= 0 or not planned_out:
                    schedule_complete = False
                    continue
                if not first <= planned_out < last:
                    continue
                planned_day = planned_out[:10]
                actual = _log_values(flight, timestamp)
                if actual is None:
                    unknown_days.add(planned_day)
                if planned_day in newer_days:
                    continue
                ferry = flight.get("ferry")
                saved = conn.execute(
                    UPSERT_FLIGHT,
                    (
                        operator,
                        nid,
                        planned_out,
                        _timestamp(flight.get("arrivalUTC"), utc_field=True),
                        _icao(flight.get("departure")),
                        _icao(flight.get("arrival")),
                        _code(flight.get("aircraft"), r"[A-Z0-9-]{1,12}"),
                        _code(flight.get("aircraftType"), r"[A-Z0-9]{2,4}"),
                        ferry if type(ferry) is bool else None,
                        timestamp,
                    ),
                )
                if actual is not None and saved.rowcount:
                    conn.execute(UPSERT_ACTUAL, (operator, nid, *actual, timestamp))
            if schedule_complete:
                conn.execute(
                    """
                    DELETE FROM journey_flights
                    WHERE operator = ? AND planned_out >= ? AND planned_out < ?
                        AND synced_at < ?
                        AND NOT EXISTS (
                            SELECT 1 FROM journey_sync_days AS coverage
                            WHERE coverage.operator = journey_flights.operator
                                AND coverage.day = SUBSTR(journey_flights.planned_out, 1, 10)
                                AND coverage.synced_at > ?
                        )
                    """,
                    (operator, first, last, timestamp, timestamp),
                )
            conn.executemany(
                """
                INSERT INTO journey_sync_days VALUES (?, ?, ?, ?)
                ON CONFLICT(operator, day) DO UPDATE SET
                    complete=excluded.complete, synced_at=excluded.synced_at
                WHERE excluded.synced_at >= journey_sync_days.synced_at
                """,
                (
                    (
                        operator,
                        day,
                        schedule_complete
                        and (details_complete or bool(unknown_days))
                        and day not in unknown_days,
                        timestamp,
                    )
                    for day in (
                        (start + timedelta(days=offset)).isoformat()
                        for offset in range(days)
                    )
                ),
            )

    def summary(self, start, end, *, departure=None, arrival=None, aircraft_type=None):
        days = _bounds(start, end, MAX_HISTORY_DAYS)
        operator = self._operator()
        first, last = start.isoformat(), (end + timedelta(days=1)).isoformat()
        with closing(self._connect()) as conn:
            conn.execute("BEGIN")
            rows = conn.execute(
                """
                SELECT log_status, departure, arrival, block_out, takeoff, landing, block_in,
                    planned_out, planned_in, ferry,
                    fuel_burn, fuel_unit, fuel_uplift, uplift_unit, distance_nm,
                    COALESCE(journey_actual.aircraft_type,
                        CASE WHEN journey_actual.aircraft = journey_flights.aircraft
                            THEN journey_flights.aircraft_type END) AS aircraft_type
                FROM journey_flights LEFT JOIN journey_actual USING (operator, flight_nid)
                WHERE operator = ? AND planned_out >= ? AND planned_out < ?
                    AND (? IS NULL OR departure = ?) AND (? IS NULL OR arrival = ?)
                """,
                (operator, first, last, departure, departure, arrival, arrival),
            ).fetchall()
            covered, latest = conn.execute(
                """
                SELECT COALESCE(SUM(complete), 0), MAX(synced_at) FROM journey_sync_days
                WHERE operator = ? AND day >= ? AND day < ?
                """,
                (operator, first, last),
            ).fetchone()
        if aircraft_type:
            rows = [row for row in rows if row["aircraft_type"] == aircraft_type]
        return {
            "start": first,
            "end": end.isoformat(),
            "complete": covered == days,
            "covered_days": covered,
            "last_synced_at": latest,
            **_report(rows),
        }


journey_history = JourneyHistory()
_sync_slots = asyncio.Semaphore(2)
_insights_windows = SourceCache(32)


async def _sync_window(start, end):
    async with _sync_slots:
        fetched_at = datetime.now(UTC)
        result = await get_flight_history(start.isoformat(), end.isoformat())
        await asyncio.to_thread(
            journey_history.ingest, start, end, result, fetched_at=fetched_at
        )


async def refresh_journey_history(start, end):
    _completed_period(start, end, MAX_REFRESH_DAYS)
    for first, last in _windows(start, end):
        await _sync_window(first, last)
    return await asyncio.to_thread(journey_history.summary, start, end)


async def load_journey_insights(start, end, *, refresh=False):
    _completed_period(start, end, MAX_HISTORY_DAYS)
    covered = (
        ()
        if refresh
        else await asyncio.to_thread(journey_history.complete_days, start, end)
    )
    windows = iter(reversed(list(_windows(start, end, covered))))
    source_failed = False

    async def worker():
        nonlocal source_failed
        for first, last in windows:
            if source_failed:
                return
            try:
                key = (
                    journey_history.path or settings.database_path,
                    settings.leon_operator,
                    first,
                    last,
                )
                await _insights_windows.get(
                    key, 1, lambda: _sync_window(first, last), refresh=refresh
                )
            except INSIGHTS_SOURCE_ERRORS:
                source_failed = True
                return

    try:
        async with asyncio.timeout(INSIGHTS_SYNC_TIMEOUT):
            await asyncio.gather(worker(), worker())
    except TimeoutError:
        source_failed = True

    summary = await asyncio.to_thread(journey_history.summary, start, end)
    return {**summary, "source_failed": source_failed}
