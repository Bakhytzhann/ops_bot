"""Shared turnaround event log and statistics of recorded ground milestones."""

import json
import re
import sqlite3
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import UTC, datetime
from math import ceil
from statistics import mean, median

from config.settings import settings

KINDS = frozenset(
    {
        "IN_BLOCK",
        "LANDING",
        "TAKEOFF",
        "PAX_OFF_STARTED",
        "PAX_OFF_COMPLETED",
        "NOTE",
        "OUT_BLOCK",
        "CHOCKS_ON",
        "CHOCKS_OFF",
        "STAND",
        "FUEL_REQUESTED",
        "FUEL_TRUCK_ARRIVED",
        "FUEL_STARTED",
        "FUEL_COMPLETED",
        "CABIN_READY",
        "HANDLING_READY",
        "DOCUMENTS_READY",
        "BOARDING_STARTED",
        "BOARDING_COMPLETED",
        "PAX_ON_BOARD",
        "REPORTED_READY",
        "TARGET",
        "BLOCKER_OPEN",
        "BLOCKER_CLOSE",
    }
)
PROCESSES = frozenset({"fuel", "handling", "cabin", "pax", "documents", "other"})
FUEL_DURATION = ("FUEL_STARTED", "FUEL_COMPLETED")
FUEL_WAIT = ("FUEL_REQUESTED", "FUEL_TRUCK_ARRIVED")
GROUND_DURATION = ("IN_BLOCK", "OUT_BLOCK")

SCHEMA = """
    CREATE TABLE IF NOT EXISTS ground_turn_sessions (
        turn_key TEXT PRIMARY KEY,
        station TEXT NOT NULL,
        inbound_id INTEGER,
        outbound_id INTEGER,
        registration TEXT,
        aircraft_type TEXT,
        planned_in TEXT,
        planned_out TEXT,
        first_seen_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS ground_turn_sessions_cohort
        ON ground_turn_sessions(station, aircraft_type);
    CREATE TABLE IF NOT EXISTS ground_turn_events (
        id INTEGER PRIMARY KEY,
        turn_key TEXT NOT NULL REFERENCES ground_turn_sessions(turn_key),
        kind TEXT NOT NULL,
        event_at TEXT NOT NULL,
        actor_id INTEGER,
        recorded_at TEXT NOT NULL,
        source TEXT NOT NULL,
        request_id TEXT NOT NULL,
        detail TEXT NOT NULL,
        UNIQUE(turn_key, request_id)
    );
    CREATE INDEX IF NOT EXISTS ground_turn_events_timeline
        ON ground_turn_events(turn_key, event_at, id);
    CREATE INDEX IF NOT EXISTS ground_turn_events_kind
        ON ground_turn_events(turn_key, kind);
"""


def _timestamp(value):
    try:
        stamp = datetime.fromisoformat(value) if isinstance(value, str) else value
    except ValueError as error:
        raise ValueError("Неверное время наземного события.") from error
    if not isinstance(stamp, datetime) or stamp.utcoffset() is None:
        raise ValueError("Наземному событию требуется время с часовым поясом.")
    return stamp.astimezone(UTC).isoformat(timespec="microseconds")


def _identifier(value, pattern):
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise ValueError("Неверный идентификатор наземного события.")
    return value


def _flight_id(value):
    if value is not None and (type(value) is not int or not 0 < value < 2**63):
        raise ValueError("Неверный идентификатор рейса Leon.")
    return value


def _metadata(turn):
    station = _identifier(turn["station"], r"[A-Z0-9]{4}")
    inbound_id = _flight_id(turn.get("inbound_id"))
    outbound_id = _flight_id(turn.get("outbound_id"))
    if inbound_id is None and outbound_id is None:
        raise ValueError("Выберите рейс Leon перед записью наземного события.")
    registration = turn.get("registration") or turn.get("aircraft")
    aircraft_type = turn.get("aircraft_type")
    return (
        _identifier(turn["key"], r"[A-Za-z0-9:_-]{1,96}"),
        station,
        inbound_id,
        outbound_id,
        registration.strip().upper() if isinstance(registration, str) else None,
        aircraft_type.strip().upper() if isinstance(aircraft_type, str) else None,
        _timestamp(turn["planned_in"]) if turn.get("planned_in") is not None else None,
        _timestamp(turn["planned_out"])
        if turn.get("planned_out") is not None
        else None,
    )


def _event_detail(kind, detail):
    if detail is not None and not isinstance(detail, dict):
        raise ValueError("Неверные детали наземного события.")
    payload = dict(detail or {})
    if kind in {"BLOCKER_OPEN", "BLOCKER_CLOSE"}:
        if payload.get("process") not in PROCESSES:
            raise ValueError("Выберите процесс для регистрации задержки.")
        if payload.get("expected_at") is not None:
            payload["expected_at"] = _timestamp(payload["expected_at"])
    if kind == "TARGET" and payload.get("target_at") is not None:
        payload["target_at"] = _timestamp(payload["target_at"])
    serialized = json.dumps(payload, ensure_ascii=False, allow_nan=False)
    if len(serialized) > 4000:
        raise ValueError("Описание наземного события слишком длинное.")
    return serialized


def _durations(tours, start, end):
    result = []
    for events in tours.values():
        if start not in events or end not in events:
            continue
        filed_start = datetime.fromisoformat(events[start])
        filed_end = datetime.fromisoformat(events[end])
        if filed_start < filed_end:
            result.append((filed_end - filed_start).total_seconds() / 60)
    return result


def _summary(values):
    if not values:
        return {
            "count": 0,
            "median_minutes": None,
            "mean_minutes": None,
            "p80_minutes": None,
        }
    ordered = sorted(values)
    count = len(ordered)
    return {
        "count": count,
        "median_minutes": round(median(ordered), 1),
        "mean_minutes": round(mean(ordered), 1),
        "p80_minutes": round(ordered[ceil(0.8 * count) - 1], 1),
    }


class GroundTurnStore:
    def __init__(self, path=None):
        self.path = path

    @contextmanager
    def _connect(self):
        path = self.path or settings.database_path
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.executescript(SCHEMA)
            with conn:
                yield conn
        finally:
            conn.close()

    @staticmethod
    def _observe(conn, metadata, observed_at):
        conn.execute(
            """
            INSERT INTO ground_turn_sessions
              (turn_key,station,inbound_id,outbound_id,registration,aircraft_type,
               planned_in,planned_out,first_seen_at)
            VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(turn_key) DO UPDATE SET
                inbound_id=COALESCE(ground_turn_sessions.inbound_id,excluded.inbound_id),
                outbound_id=COALESCE(ground_turn_sessions.outbound_id,excluded.outbound_id),
                registration=COALESCE(ground_turn_sessions.registration,excluded.registration),
                aircraft_type=COALESCE(ground_turn_sessions.aircraft_type,excluded.aircraft_type),
                planned_in=COALESCE(ground_turn_sessions.planned_in,excluded.planned_in),
                planned_out=COALESCE(ground_turn_sessions.planned_out,excluded.planned_out)
            """,
            (*metadata, observed_at),
        )

    def observe(self, turn):
        metadata = _metadata(turn)
        now = _timestamp(datetime.now(UTC))
        with self._connect() as conn:
            self._observe(conn, metadata, now)
            # Leon supplies flight identity and schedule only. Operational facts
            # must be explicitly entered by an operator.

    def baseline(self, turn_key):
        with self._connect() as conn:
            row = conn.execute(
                """SELECT
                     inbound_id,outbound_id,registration,aircraft_type,
                     planned_in,planned_out,first_seen_at
                   FROM ground_turn_sessions WHERE turn_key=?""",
                (turn_key,),
            ).fetchone()
        return dict(row) if row is not None else None

    def record(
        self,
        turn,
        kind,
        *,
        actor_id,
        event_at,
        request_id,
        detail=None,
        source="telegram",
    ):
        if kind not in KINDS:
            raise ValueError("Неизвестный этап наземного обслуживания.")
        if actor_id is None:
            if source != "leon":
                raise ValueError("Наземному событию требуется автор.")
        elif type(actor_id) is not int or not 0 < actor_id < 2**63:
            raise ValueError("Неверный автор наземного события.")
        request_id = _identifier(request_id, r"[A-Za-z0-9:_-]{1,160}")
        source = _identifier(source, r"[a-z][a-z0-9_-]{0,31}")
        event_at = _timestamp(event_at)
        recorded_at = _timestamp(datetime.now(UTC))
        payload = _event_detail(kind, detail)
        metadata = _metadata(turn)
        with self._connect() as conn:
            self._observe(conn, metadata, recorded_at)
            inserted = conn.execute(
                """INSERT INTO ground_turn_events
                     (turn_key,kind,event_at,actor_id,recorded_at,source,request_id,detail)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(turn_key,request_id) DO NOTHING""",
                (
                    metadata[0],
                    kind,
                    event_at,
                    actor_id,
                    recorded_at,
                    source,
                    request_id,
                    payload,
                ),
            )
            return bool(inserted.rowcount)

    def events(self, turn_key, limit=100):
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id,kind,event_at,actor_id,recorded_at,source,detail
                FROM ground_turn_events WHERE turn_key=?
                ORDER BY id DESC LIMIT ?
                """,
                (turn_key, limit),
            ).fetchall()
        return sorted(
            ({**dict(row), "detail": json.loads(row["detail"])} for row in rows),
            key=lambda event: (event["event_at"], event["id"]),
        )

    def recent_outs(self, turn_keys):
        keys = tuple(dict.fromkeys(turn_keys))
        if not keys:
            return {}
        latest = {}
        now = _timestamp(datetime.now(UTC))
        with self._connect() as conn:
            for offset in range(0, len(keys), 200):
                batch = keys[offset : offset + 200]
                placeholders = ",".join("?" for _ in batch)
                rows = conn.execute(
                    f"""
                    WITH latest_outs AS (
                        SELECT turn_key,event_at,ROW_NUMBER() OVER (
                            PARTITION BY turn_key ORDER BY (source='leon') DESC,id DESC
                        ) AS position
                        FROM ground_turn_events
                        WHERE kind='OUT_BLOCK' AND event_at<=?
                          AND turn_key IN ({placeholders})
                    )
                    SELECT turn_key,event_at FROM latest_outs WHERE position=1
                    """,
                    (now, *batch),
                )
                latest.update((row["turn_key"], row["event_at"]) for row in rows)
        return latest

    def _history(self, station, aircraft_type, exclude_turn):
        if aircraft_type:
            aircraft_type = aircraft_type.strip().upper()
        filters = ["event.event_at<=?"]
        parameters = [_timestamp(datetime.now(UTC))]
        for clause, value in (
            ("session.station=?", station),
            ("session.aircraft_type=?", aircraft_type),
            ("session.turn_key<>?", exclude_turn),
        ):
            if value is not None:
                filters.append(clause)
                parameters.append(value)
        cohort = " AND ".join(filters)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                WITH ranked_events AS (
                    SELECT session.turn_key,event.kind,event.event_at,event.detail,
                        ROW_NUMBER() OVER (
                            PARTITION BY session.turn_key,event.kind
                            ORDER BY (event.source='leon') DESC,event.id DESC
                        ) AS position
                    FROM ground_turn_sessions session JOIN ground_turn_events event
                        ON event.turn_key=session.turn_key
                    WHERE {cohort}
                      AND event.kind IN ('IN_BLOCK','OUT_BLOCK','FUEL_STARTED',
                        'FUEL_COMPLETED','FUEL_REQUESTED','FUEL_TRUCK_ARRIVED','BLOCKER_OPEN')
                )
                SELECT turn_key,kind,event_at,detail FROM ranked_events
                WHERE position=1 OR kind='BLOCKER_OPEN'
                """,
                parameters,
            )
            tours = defaultdict(dict)
            blockers = set()
            for row in rows:
                key = row["turn_key"]
                kind = row["kind"]
                if kind == "BLOCKER_OPEN":
                    tours.setdefault(key, {})
                    blockers.add((key, json.loads(row["detail"])["process"]))
                else:
                    tours[key][kind] = row["event_at"]
        return tours, Counter(process for _, process in blockers)

    def station_history(self, station, aircraft_type=None, *, exclude_turn=None):
        tours, blockers = self._history(station, aircraft_type, exclude_turn)
        return {
            "turn_count": len(tours),
            "ground_duration": _summary(_durations(tours, *GROUND_DURATION)),
            "fuel_duration": _summary(_durations(tours, *FUEL_DURATION)),
            "fuel_wait": _summary(_durations(tours, *FUEL_WAIT)),
            "blocker_counts": dict(sorted(blockers.items())),
        }

    def station_report(self, station, *, days=180):
        """Manual-only metrics; corrections use the newest event for each kind."""
        from datetime import timedelta

        since = _timestamp(datetime.now(UTC) - timedelta(days=days))
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT event.turn_key, event.kind, event.event_at, event.detail,
                          event.recorded_at, session.first_seen_at
                   FROM ground_turn_events event
                   JOIN ground_turn_sessions session ON session.turn_key=event.turn_key
                   WHERE session.station=? AND event.source='telegram'
                   ORDER BY event.id""",
                (station,),
            ).fetchall()
        tours = defaultdict(dict)
        recent_notes = []
        critical_notes = []
        total = 0
        critical_terms = (
            "разлив", "разлил", "утечк", "поврежден", "повреждён", "поврежд", "зацеп", "наезд", "опасн",
            "столкнов", "удар", "пожар", "травм", "spill",
            "damage", "collision", "incident", "safety",
        )
        for row in rows:
            detail = json.loads(row["detail"])
            if row["kind"] == "NOTE":
                note = detail.get("text", "").strip()
                if not note:
                    continue
                item = {"date": row["event_at"][:10], "text": note[:500]}
                if row["event_at"] >= since:
                    recent_notes.append(item)
                    tours.setdefault(row["turn_key"], {})
                    total += 1
                if detail.get("critical") is True or any(
                    word in note.casefold() for word in critical_terms
                ):
                    critical_notes.append(item)
                continue
            if row["event_at"] < since:
                continue
            total += 1
            tours[row["turn_key"]][row["kind"]] = row["event_at"]
        metrics = {
            "Заправка": _summary(_durations(tours, "FUEL_STARTED", "FUEL_COMPLETED")),
            "Ожидание ТЗ": _summary(_durations(tours, "FUEL_REQUESTED", "FUEL_TRUCK_ARRIVED")),
            "Посадка пассажиров": _summary(_durations(tours, "BOARDING_STARTED", "BOARDING_COMPLETED")),
            "Высадка пассажиров": _summary(_durations(tours, "PAX_OFF_STARTED", "PAX_OFF_COMPLETED")),
            "Время на земле": _summary(_durations(tours, "IN_BLOCK", "OUT_BLOCK")),
        }
        frequency = Counter(kind for events in tours.values() for kind in events)
        return {
            "station": station,
            "days": days,
            "turns": len(tours),
            "events": total,
            "event_counts": dict(frequency.most_common()),
            "metrics": metrics,
            "notes": recent_notes[-45:],
            "critical": critical_notes[-30:],
            "note_count": len(recent_notes),
            "critical_count": len(critical_notes),
        }

    def fuel_stats(self, station, aircraft_type=None, *, exclude_turn=None):
        tours, _ = self._history(station, aircraft_type, exclude_turn)
        return _summary(_durations(tours, *FUEL_DURATION))

    def fuel_wait_stats(self, station, aircraft_type=None, *, exclude_turn=None):
        tours, _ = self._history(station, aircraft_type, exclude_turn)
        return _summary(_durations(tours, *FUEL_WAIT))


ground_turns = GroundTurnStore()
