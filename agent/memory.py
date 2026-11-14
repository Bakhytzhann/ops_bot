import json
import sqlite3
from datetime import UTC, datetime

from config.settings import settings

MAX_HISTORY = 10


class AgentMemory:
    def __init__(self):
        self.db_path = settings.database_path
        self.db_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )
        self._init_db()

    def _connect(self):
        return sqlite3.connect(
            self.db_path,
            timeout=10,
        )

    def _init_db(self):
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS conversations (
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    messages TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (chat_id, user_id)
                )
            """)

    def get_history(
        self,
        chat_id: int,
        user_id: int,
    ) -> list[dict]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT messages
                FROM conversations
                WHERE chat_id = ? AND user_id = ?
                """,
                (chat_id, user_id),
            ).fetchone()

        if not row:
            return []

        return json.loads(row[0])[-MAX_HISTORY:]

    def save_history(
        self,
        chat_id: int,
        user_id: int,
        messages: list[dict],
    ):
        history = messages[-MAX_HISTORY:]

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO conversations
                    (chat_id, user_id, messages, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(chat_id, user_id)
                DO UPDATE SET
                    messages = excluded.messages,
                    updated_at = excluded.updated_at
                """,
                (
                    chat_id,
                    user_id,
                    json.dumps(history, ensure_ascii=False),
                    datetime.now(UTC).isoformat(),
                ),
            )

    def clear_history(
        self,
        chat_id: int,
        user_id: int,
    ):
        with self._connect() as conn:
            conn.execute(
                """
                DELETE FROM conversations
                WHERE chat_id = ? AND user_id = ?
                """,
                (chat_id, user_id),
            )


memory = AgentMemory()
