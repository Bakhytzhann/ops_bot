"""Persistent Telegram admission; never imported by operational services."""

import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime

from config.settings import settings


class AccessStore:
    def __init__(self, path=None):
        self.path = path

    @contextmanager
    def connect(self):
        path = self.path or settings.database_path
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                conn.executescript("""
                    CREATE TABLE IF NOT EXISTS bot_access (
                        user_id INTEGER PRIMARY KEY, name TEXT NOT NULL DEFAULT '',
                        status TEXT NOT NULL CHECK(status IN ('pending','approved','rejected','revoked')),
                        revision INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL, decided_by INTEGER
                    );
                    CREATE TABLE IF NOT EXISTS bot_access_notifications (
                        user_id INTEGER NOT NULL, revision INTEGER NOT NULL, admin_id INTEGER NOT NULL,
                        PRIMARY KEY(user_id, revision, admin_id)
                    );
                """)
                now = datetime.now(UTC).isoformat()
                conn.executemany(
                    "INSERT OR IGNORE INTO bot_access(user_id,status,updated_at) VALUES(?,'approved',?)",
                    [(user, now) for user in settings.allowed_user_ids],
                )
                yield conn
        finally:
            conn.close()

    def get(self, user_id):
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM bot_access WHERE user_id=?", (user_id,)
            ).fetchone()
            return dict(row) if row else None

    def is_allowed(self, user_id):
        if user_id in settings.admin_ids:
            return True
        record = self.get(user_id)
        return record is not None and record["status"] == "approved"

    def request(self, user_id, name=""):
        if user_id <= 0:
            raise ValueError("Invalid user ID")
        with self.connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO bot_access(user_id,name,status,updated_at) VALUES(?,?,'pending',?)",
                (user_id, name[:120], datetime.now(UTC).isoformat()),
            )
            resubmitted = (
                conn.execute(
                    "UPDATE bot_access SET status='pending',revision=revision+1,updated_at=? "
                    "WHERE user_id=? AND status='revoked'",
                    (datetime.now(UTC).isoformat(), user_id),
                ).rowcount
                == 1
            )
            record = dict(
                conn.execute(
                    "SELECT * FROM bot_access WHERE user_id=?", (user_id,)
                ).fetchone()
            )
            return {**record, "resubmitted": resubmitted}

    def decide(self, actor, user_id, action, revision):
        if actor not in settings.admin_ids:
            raise PermissionError("Administrator required")
        if user_id in settings.admin_ids:
            raise ValueError("Administrators are managed through TELEGRAM_ADMIN_IDS")
        if action not in {"approve", "reject", "revoke"}:
            raise ValueError("Unknown access decision")
        status, previous = {
            "approve": ("approved", "pending"),
            "reject": ("rejected", "pending"),
            "revoke": ("revoked", "approved"),
        }[action]
        with self.connect() as conn:
            changed = conn.execute(
                "UPDATE bot_access SET status=?,revision=revision+1,updated_at=?,decided_by=? "
                "WHERE user_id=? AND revision=? AND status=?",
                (
                    status,
                    datetime.now(UTC).isoformat(),
                    actor,
                    user_id,
                    revision,
                    previous,
                ),
            ).rowcount
            if not changed:
                raise ValueError("Заявка уже обработана или кнопка устарела.")
        return self.get(user_id)

    def list(self, page=0):
        if page < 0:
            raise ValueError("Invalid page")
        with self.connect() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM bot_access ORDER BY CASE status WHEN 'pending' THEN 0 ELSE 1 END, user_id LIMIT 11 OFFSET ?",
                    (page * 10,),
                )
            ]

    def notified(self, user_id, revision, admin_id):
        with self.connect() as conn:
            return (
                conn.execute(
                    "SELECT 1 FROM bot_access_notifications WHERE user_id=? AND revision=? AND admin_id=?",
                    (user_id, revision, admin_id),
                ).fetchone()
                is not None
            )

    def mark_notified(self, user_id, revision, admin_id):
        with self.connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO bot_access_notifications VALUES(?,?,?)",
                (user_id, revision, admin_id),
            )


access_store = AccessStore()
