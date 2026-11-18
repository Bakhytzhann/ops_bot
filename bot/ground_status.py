"""Ground-handling event capture for authorized private OPS Bot users.

Qwen extracts candidate events; deterministic validation and SQLite decide what
is recorded. Never infer an unreported event time or operational clearance.
"""

import asyncio
import json
import re
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from config.settings import settings

GROUND_EVENTS = {
    "CHOCKS_ON": "Колодки установлены",
    "CHOCKS_OFF": "Колодки убраны",
    "STAND": "Стоянка",
    "FUEL_STARTED": "Заправка начата",
    "FUEL_COMPLETED": "Заправка завершена",
    "CABIN_READY": "Салон готов",
    "BOARDING_STARTED": "Посадка начата",
    "BOARDING_COMPLETED": "Посадка завершена",
    "PAX_ON_BOARD": "Пассажиры на борту",
    "REPORTED_READY": "Заявлена полная готовность",
}
GROUND_MARKUP = InlineKeyboardMarkup(
    [[InlineKeyboardButton("ВИС-код · ссылка", callback_data="ground:link")]]
)
FLIGHT_RE = re.compile(r"^[\w-]{2,24}$", re.UNICODE)
TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


class GroundStore:
    def __init__(self, path=None):
        self.path = path

    def _connect(self):
        path = Path(self.path or settings.database_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=10000")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS ground_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                flight_ref TEXT NOT NULL,
                message_id INTEGER,
                received_at TEXT NOT NULL,
                raw_text TEXT NOT NULL,
                UNIQUE(user_id, message_id)
            );
            CREATE TABLE IF NOT EXISTS ground_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id INTEGER NOT NULL REFERENCES ground_messages(id),
                event_type TEXT NOT NULL,
                event_time TEXT,
                value TEXT,
                evidence TEXT NOT NULL,
                recorded_at TEXT NOT NULL
            );
            """
        )
        return conn

    def record(self, user_id, flight_ref, telegram_message_id, raw_text, events):
        """Append events with the original message; repeated Telegram updates are idempotent."""
        received = datetime.now(UTC).isoformat()
        with closing(self._connect()) as conn, conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO ground_messages "
                "(user_id,flight_ref,message_id,received_at,raw_text) VALUES(?,?,?,?,?)",
                (user_id, flight_ref, telegram_message_id, received, raw_text),
            )
            if not cur.rowcount:
                return False
            for event in events:
                conn.execute(
                    "INSERT INTO ground_events "
                    "(message_id,event_type,event_time,value,evidence,recorded_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (
                        cur.lastrowid,
                        event["type"],
                        event.get("time"),
                        event.get("value"),
                        event["evidence"],
                        received,
                    ),
                )
        return True

    def events(self, user_id, flight_ref, limit=120):
        with closing(self._connect()) as conn:
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT e.*, m.received_at, m.raw_text "
                    "FROM ground_events e JOIN ground_messages m ON m.id=e.message_id "
                    "WHERE m.user_id=? AND m.flight_ref=? "
                    "ORDER BY e.id DESC LIMIT ?",
                    (user_id, flight_ref, limit),
                )
            ]


ground_store = GroundStore()


def validate_events(payload, original):
    """Accept only supported types with evidence copied from the actual message."""
    if not isinstance(payload, dict) or not isinstance(payload.get("events"), list):
        raise TypeError("Qwen не вернул структурированные события.")
    if len(payload["events"]) > 12:
        raise ValueError("Слишком много событий для одного сообщения.")
    result = []
    for item in payload["events"]:
        if not isinstance(item, dict) or item.get("type") not in GROUND_EVENTS:
            continue
        evidence = item.get("evidence")
        if not isinstance(evidence, str) or not evidence.strip():
            continue
        evidence = evidence.strip()
        if evidence.casefold() not in original.casefold():
            continue
        time = item.get("time")
        if time is not None and (not isinstance(time, str) or not TIME_RE.fullmatch(time)):
            continue
        # An event's clock time must also appear explicitly, with or without colon.
        if time and time not in original and time.replace(":", "") not in original:
            continue
        value = item.get("value")
        if value is not None and (not isinstance(value, str) or len(value) > 60):
            continue
        if item["type"] == "STAND" and (
            not value or value.casefold() not in original.casefold()
        ):
            continue
        result.append(
            {"type": item["type"], "time": time, "value": value, "evidence": evidence}
        )
    return result


async def extract_events(text):
    """Use Qwen strictly as a typed extractor, not as the source of truth."""
    from ollama import AsyncClient

    client = AsyncClient(host=settings.ollama_host)
    system = (
        "Extract ONLY explicitly reported ground-handling events from the user message. "
        "Return JSON object with events array. Each event: "
        "type (CHOCKS_ON, CHOCKS_OFF, STAND, FUEL_STARTED, FUEL_COMPLETED, "
        "CABIN_READY, BOARDING_STARTED, BOARDING_COMPLETED, PAX_ON_BOARD, "
        "REPORTED_READY), time (HH:MM or null), value (string or null), "
        "evidence (exact verbatim substring of user message). "
        "Interpret 1235 as 12:35 only when explicitly used as a time. "
        "Time MUST be null if missing. For STAND, value must be the explicit stand "
        "identifier. Never infer safety clearance, flight readiness, time, or any "
        "event not explicitly mentioned. No matches => empty events."
    )
    response = await asyncio.wait_for(
        client.chat(
            model=settings.ollama_model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": text}],
            format="json",
            think=False,
            keep_alive=settings.ollama_keep_alive,
            options={"temperature": 0, "num_predict": 500},
        ),
        timeout=45,
    )
    try:
        payload = json.loads(response.message.content or "{}")
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("Qwen вернул некорректный ответ.") from exc
    return validate_events(payload, text)


def status_text(flight_ref, entries):
    lines = [f"GROUND STATUS · {flight_ref}", "", "Последние зарегистрированные события:"]
    if not entries:
        lines.append("Событий пока нет.")
    else:
        for entry in reversed(entries[-20:]):
            title = GROUND_EVENTS[entry["event_type"]]
            tail = f" · {entry['event_time']}" if entry["event_time"] else ""
            value = f" · {entry['value']}" if entry["value"] else ""
            lines.append(f"• {title}{tail}{value}")
    lines.extend(
        ["", "Отправьте следующее событие обычным сообщением в этот чат.",
         "Указанная сотрудником готовность не является разрешением на вылет."]
    )
    return "\n".join(lines)


async def handle_ground_message(update, context):
    text = update.effective_message.text.strip()
    state = context.user_data
    if state.get("ground_selecting"):
        if not FLIGHT_RE.fullmatch(text):
            await update.effective_message.reply_text(
                "Укажите номер рейса или регистрацию одним значением (2–24 символа).",
            )
            return
        state["ground_flight"] = text.upper()
        state.pop("ground_selecting", None)
        entries = await asyncio.to_thread(
            ground_store.events, update.effective_user.id, state["ground_flight"]
        )
        await update.effective_message.reply_text(
            status_text(state["ground_flight"], entries), reply_markup=GROUND_MARKUP
        )
        return
    flight_ref = state.get("ground_flight")
    if not flight_ref:
        return
    try:
        events = await extract_events(text)
    except (TimeoutError, ConnectionError, ValueError, TypeError, OSError):
        await update.effective_message.reply_text(
            "Не удалось распознать сообщение через Qwen. Данные не сохранены. Повторите запрос."
        )
        return
    if not events:
        await update.effective_message.reply_text(
            "Подтверждённых событий не найдено. Уточните сообщение — статус не изменён."
        )
        return
    await asyncio.to_thread(
        ground_store.record,
        update.effective_user.id,
        flight_ref,
        getattr(update.effective_message, "message_id", None),
        text,
        events,
    )
    entries = await asyncio.to_thread(
        ground_store.events, update.effective_user.id, flight_ref
    )
    await update.effective_message.reply_text(
        status_text(flight_ref, entries), reply_markup=GROUND_MARKUP
    )
