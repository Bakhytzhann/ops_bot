import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime

from config.settings import settings

ROLES = {"Crew", "Ops", "Controller", "Admin"}
MODERATORS = {"Ops", "Admin"}


class NotesRepository:
    def __init__(self, path=None):
        self.path = path

    @contextmanager
    def connect(self):
        path = self.path or settings.database_path
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            self.ensure_schema(conn)
            with conn:
                yield conn
        finally:
            conn.close()

    @staticmethod
    def ensure_schema(conn):
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS station_staff (
                telegram_id INTEGER PRIMARY KEY, employee_id TEXT NOT NULL UNIQUE,
                role TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS station_notes (
                id INTEGER PRIMARY KEY, station_icao TEXT NOT NULL, flight_id INTEGER,
                author_id INTEGER NOT NULL, author_role TEXT NOT NULL, text TEXT NOT NULL,
                category TEXT, observed_at_utc TEXT NOT NULL, created_at_utc TEXT NOT NULL,
                updated_at_utc TEXT NOT NULL, verified_by INTEGER, verified_at TEXT,
                status TEXT NOT NULL DEFAULT 'active', version INTEGER NOT NULL DEFAULT 1,
                pinned INTEGER NOT NULL DEFAULT 0, request_id TEXT UNIQUE NOT NULL
            );
            CREATE INDEX IF NOT EXISTS station_notes_station ON station_notes(station_icao, created_at_utc);
            CREATE INDEX IF NOT EXISTS station_notes_flight ON station_notes(flight_id);
            CREATE TABLE IF NOT EXISTS station_note_history (
                id INTEGER PRIMARY KEY, note_id INTEGER NOT NULL, actor_id INTEGER NOT NULL,
                action TEXT NOT NULL, changed_at TEXT NOT NULL, record_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS station_staff_audit (
                id INTEGER PRIMARY KEY, telegram_id INTEGER NOT NULL, employee_id TEXT,
                role TEXT, changed_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS station_schema_migrations (
                version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL
            );
            INSERT OR IGNORE INTO station_schema_migrations(version,applied_at)
                VALUES(1, strftime('%Y-%m-%dT%H:%M:%SZ','now'));
        """)

    def bind(self, telegram_id, employee_id, role):
        if role not in ROLES or not employee_id.strip() or telegram_id <= 0:
            raise ValueError("Invalid verified staff binding")
        now = datetime.now(UTC).isoformat()
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO station_staff VALUES (?,?,?,?) ON CONFLICT(telegram_id) DO UPDATE SET employee_id=excluded.employee_id, role=excluded.role, updated_at=excluded.updated_at",
                (telegram_id, employee_id, role, now),
            )
            conn.execute(
                "INSERT INTO station_staff_audit(telegram_id,employee_id,role,changed_at) VALUES(?,?,?,?)",
                (telegram_id, employee_id, role, now),
            )

    def role(self, user):
        with self.connect() as conn:
            return self._role(conn, user)

    @staticmethod
    def _role(conn, user):
        row = conn.execute(
            "SELECT role FROM station_staff WHERE telegram_id=?", (user,)
        ).fetchone()
        return row[0] if row else None

    def list(self, user, station, limit=10, offset=0):
        with self.connect() as conn:
            if not self._role(conn, user):
                raise PermissionError("Verified staff required")
            return [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM station_notes WHERE station_icao=? AND status='active' ORDER BY pinned DESC, created_at_utc DESC,id DESC LIMIT ? OFFSET ?",
                    (station, min(limit, 20), offset),
                )
            ]

    def create(
        self, user, station, text, request_id, *, observed_at=None, flight_id=None
    ):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            return self.create_in(
                conn,
                user,
                station,
                text,
                request_id,
                observed_at=observed_at,
                flight_id=flight_id,
            )

    def create_in(
        self,
        conn,
        user,
        station,
        text,
        request_id,
        *,
        observed_at=None,
        flight_id=None,
        critical=False,
    ):
        if not re.fullmatch(r"[A-Z]{4}", station) or not 1 <= len(text.strip()) <= 2000:
            raise ValueError("Station/text invalid")
        now = datetime.now(UTC)
        stamp = datetime.fromisoformat(observed_at) if observed_at else now
        if stamp.tzinfo is None or stamp > now:
            raise ValueError("Observation time must be UTC and not in the future")
        role = self._role(conn, user)
        if not role:
            raise PermissionError("Verified staff required")
        existing = conn.execute(
            "SELECT id,author_id FROM station_notes WHERE request_id=?", (request_id,)
        ).fetchone()
        if existing:
            if existing["author_id"] != user:
                raise PermissionError("Request belongs to another author")
            return existing["id"]
        cursor = conn.execute(
            "INSERT INTO station_notes(station_icao,flight_id,author_id,author_role,text,category,observed_at_utc,created_at_utc,updated_at_utc,request_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                station,
                flight_id,
                user,
                role,
                text.strip(),
                "safety" if critical else None,
                stamp.astimezone(UTC).isoformat(),
                now.isoformat(),
                now.isoformat(),
                request_id,
            ),
        )
        self._audit(conn, cursor.lastrowid, user, "create", now.isoformat())
        return cursor.lastrowid

    @staticmethod
    def _audit(conn, note_id, user, action, now):
        row = dict(
            conn.execute(
                "SELECT * FROM station_notes WHERE id=?", (note_id,)
            ).fetchone()
        )
        conn.execute(
            "INSERT INTO station_note_history(note_id,actor_id,action,changed_at,record_json) VALUES(?,?,?,?,?)",
            (note_id, user, action, now, json.dumps(row, ensure_ascii=False)),
        )

    def change(self, user, note_id, version, action, text=None):
        if action not in {"edit", "archive", "verify", "pin"}:
            raise ValueError("Unknown note action")
        now = datetime.now(UTC).isoformat()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            role = self._role(conn, user)
            row = conn.execute(
                "SELECT * FROM station_notes WHERE id=?", (note_id,)
            ).fetchone()
            if not role or not row or row["status"] != "active":
                raise PermissionError("Note not available")
            own = row["author_id"] == user
            if (
                (action == "edit" and not own)
                or (action == "archive" and not own and role not in MODERATORS)
                or (action in {"verify", "pin"} and role not in MODERATORS)
            ):
                raise PermissionError("Note action denied")
            if row["version"] != version:
                raise ValueError("Запись уже изменена. Откройте станцию заново.")
            if action == "edit":
                if not text or not 1 <= len(text.strip()) <= 2000:
                    raise ValueError("Текст должен содержать 1–2000 символов.")
                conn.execute(
                    "UPDATE station_notes SET text=?,verified_by=NULL,verified_at=NULL WHERE id=?",
                    (text.strip(), note_id),
                )
            elif action == "archive":
                conn.execute(
                    "UPDATE station_notes SET status='archived' WHERE id=?", (note_id,)
                )
            elif action == "verify":
                conn.execute(
                    "UPDATE station_notes SET verified_by=?,verified_at=? WHERE id=?",
                    (user, now, note_id),
                )
            else:
                conn.execute(
                    "UPDATE station_notes SET pinned=1-pinned WHERE id=?", (note_id,)
                )
            conn.execute(
                "UPDATE station_notes SET version=version+1,updated_at_utc=? WHERE id=?",
                (now, note_id),
            )
            self._audit(conn, note_id, user, action, now)
            return row["station_icao"]


notes = NotesRepository()
