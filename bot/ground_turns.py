"""Shared ground-handling cards and event entry in private Telegram chats."""

import asyncio
import re
import sqlite3
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from secrets import token_hex

from httpx import HTTPError
from telegram.error import TelegramError
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from bot.admission import require_user
from bot.message_edits import edit_message_text
from services.ground_flights import load_ground_flights
from services.ground_timing import choose_remark, station_remarks, turn_timing
from services.ground_turns import ground_turns
from services.ground_input import GroundModelUnavailable, parse_ground_input
from services.ground_report import interpret_station
from services.snapshot_models import utc, zulu

CATEGORIES = {"active": "Актуальные", "recent": "Недавние", "clarify": "Уточнить"}
EVENTS = {
    "IN_BLOCK": "На стоянке",
    "FUEL_REQUESTED": "Топливо заказано",
    "FUEL_TRUCK_ARRIVED": "Топливная машина подана",
    "FUEL_STARTED": "Заправка начата",
    "FUEL_COMPLETED": "Заправка завершена",
    "HANDLING_READY": "Handling завершён",
    "CABIN_READY": "Салон готов",
    "BOARDING_STARTED": "Посадка начата",
    "BOARDING_COMPLETED": "Посадка завершена",
    "PAX_ON_BOARD": "Пассажиры на борту",
    "DOCUMENTS_READY": "Документы готовы",
    "REPORTED_READY": "Доложена готовность",
    "OUT_BLOCK": "Ушли со стоянки",
}
PROCESSES = {
    "fuel": "Топливо",
    "handling": "Наземное обслуживание",
    "cabin": "Салон",
    "pax": "Пассажиры и багаж",
    "documents": "Документы",
    "other": "Другое",
}
STATE_KEY = "ground_turns_screen"
PAGE_SIZE = 8
DATA_ERRORS = (
    HTTPError,
    TimeoutError,
    OSError,
    ValueError,
    RuntimeError,
    sqlite3.Error,
)
TIME_INPUT = re.compile(r"(?:\d{4}-\d{2}-\d{2} )?(?:[01]\d|2[0-3]):[0-5]\d")


def _button(state, label, action, value=0, option=None):
    suffix = f":{option}" if option is not None else ""
    return InlineKeyboardButton(
        label, callback_data=f"gt:{state['token']}:{action}:{value}{suffix}"
    )


async def _render(update, text, rows):
    markup = InlineKeyboardMarkup(rows)
    query = getattr(update, "callback_query", None)
    if query:
        await edit_message_text(query, text, reply_markup=markup)
    else:
        await update.effective_message.reply_text(text, reply_markup=markup)


def _stamp(value, *, date=False):
    if isinstance(value, str):
        value = utc(value)
    return (
        value.astimezone(UTC).strftime("%d.%m %H:%MZ" if date else "%H:%MZ")
        if value
        else "—"
    )


def _label(turn):
    reference = turn.get("outbound_number") or turn.get("inbound_number") or "рейс"
    return f"{turn['station']} · {turn.get('registration') or 'борт не указан'} · {reference}"


async def show_ground_turns(update, context, *, refresh=False):
    require_user(update)
    state = {
        "token": token_hex(5),
        "cards": [],
        "snapshot": None,
        "view": ("active", 0),
        "pending": None,
        "current": None,
        "local": False,
        "enabled": True,
    }
    context.user_data[STATE_KEY] = state
    await _render(update, "GROUND STATUS · Загружаю актуальные рейсы Leon…", [])
    try:
        snapshot = await load_ground_flights(refresh=refresh)
    except DATA_ERRORS:
        if context.user_data.get(STATE_KEY) is state:
            await _render(
                update,
                "GROUND STATUS · Не удалось получить список рейсов Leon.",
                [[_button(state, "↻ Повторить", "refresh")]],
            )
        return
    if context.user_data.get(STATE_KEY) is state:
        state["snapshot"] = snapshot
        state["cards"] = snapshot["cards"]
        await _show_list(update, state, "active", 0)


async def _show_list(update, state, category, page):
    snapshot = state["snapshot"]
    cards = state["cards"]
    local_outs = (
        await asyncio.to_thread(
            ground_turns.recent_outs, [turn["key"] for turn in cards]
        )
        if cards
        else {}
    )
    now = datetime.now(UTC)
    selected = []
    for index, turn in enumerate(cards):
        status = turn["status"]
        reference = turn.get("reference_time")
        local_out = utc(local_outs.get(turn["key"]))
        out = turn.get("actual_out") or local_out
        if out and out <= now:
            status = "recent" if now - out <= timedelta(hours=6) else None
            reference = out
        if (
            status in {"active", "recent"}
            and reference is not None
            and abs(reference - now) > timedelta(hours=6)
        ):
            continue
        if status == category:
            selected.append((index, turn, reference))
    pages = max(1, (len(selected) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(page, pages - 1)
    state["view"] = (category, page)
    lines = [
        f"GROUND STATUS · {CATEGORIES[category]}",
        "Окно актуальности ±6 ч · времена UTC",
    ]
    if snapshot:
        if snapshot.get("fetched_at"):
            lines.append(f"Leon · обновлено {zulu(snapshot['fetched_at'])}")
        if not snapshot.get("schedule_complete") or not snapshot.get("watch_complete"):
            lines.append(
                "Часть актуальных времён Leon недоступна. Проверьте карточку рейса."
            )
    lines.append(f"Карточек в разделе: {len(selected)}")
    rows = [
        [
            _button(
                state,
                f"{_stamp(reference)} · {_label(turn)}",
                "open",
                index,
            )
        ]
        for index, turn, reference in selected[
            page * PAGE_SIZE : (page + 1) * PAGE_SIZE
        ]
    ]
    if pages > 1:
        rows.append(
            [
                _button(state, label, "list", category, target)
                for label, target in (
                    ("←", page - 1),
                    (f"{page + 1}/{pages}", page),
                    ("→", page + 1),
                )
                if 0 <= target < pages
            ]
        )
    rows.extend(
        [
            [
                _button(
                    state, f"{'● ' if key == category else ''}{label}", "list", key, 0
                )
                for key, label in CATEGORIES.items()
            ],
            [_button(state, "↻ Обновить рейсы", "refresh")],
        ]
    )
    await _render(update, "\n".join(lines), rows)


def _back(state):
    category, page = state["view"]
    return _button(state, "← К рейсам", "list", category, page)



@lru_cache(maxsize=1)
def _airport_catalog():
    import airportsdata
    return airportsdata.load("ICAO")


def _stamp_for(value, station, local=False):
    if isinstance(value, str):
        value = utc(value)
    if not value:
        return "—"
    if not local:
        return value.astimezone(UTC).strftime("%H:%MZ")
    try:
        timezone = _airport_catalog()[station]["tz"]
        return value.astimezone(ZoneInfo(timezone)).strftime("%H:%M LT")
    except (KeyError, ValueError, ZoneInfoNotFoundError):
        return value.astimezone(UTC).strftime("%H:%MZ")


ARRIVAL_EVENTS = (
    ("LANDING", "Landing"),
    ("IN_BLOCK", "On-block"),
    ("CHOCKS_ON", "Chocks on"),
    ("PAX_OFF_STARTED", "PAX off started"),
    ("PAX_OFF_COMPLETED", "PAX off completed"),
)
DEPARTURE_EVENTS = (
    ("FUEL_REQUESTED", "Fuel requested"),
    ("FUEL_TRUCK_ARRIVED", "Fuel truck"),
    ("FUEL_STARTED", "Fuel started"),
    ("FUEL_COMPLETED", "Fuel completed"),
    ("CABIN_READY", "Cabin ready"),
    ("HANDLING_READY", "Handling ready"),
    ("DOCUMENTS_READY", "Documents ready"),
    ("BOARDING_STARTED", "Boarding started"),
    ("BOARDING_COMPLETED", "Boarding completed"),
    ("PAX_ON_BOARD", "PAX on board"),
    ("REPORTED_READY", "Ready"),
    ("CHOCKS_OFF", "Chocks off"),
    ("OUT_BLOCK", "Off-block"),
    ("TAKEOFF", "Takeoff"),
)
GROUPED_EVENTS = (
    ("FUEL_STARTED", "FUEL_COMPLETED", "Fuel"),
    ("BOARDING_STARTED", "BOARDING_COMPLETED", "Boarding"),
    ("PAX_OFF_STARTED", "PAX_OFF_COMPLETED", "PAX off"),
)


def _card_text(turn, events, *, local=False):
    # Later manual recordings supersede earlier ones; every correction
    # remains in the append-only audit log.
    latest = {}
    notes = []
    for entry in sorted(events, key=lambda event: event["id"]):
        if entry.get("source") != "telegram":
            continue
        if entry["kind"] == "NOTE":
            note = (entry.get("detail") or {}).get("text", "").strip()
            if note:
                notes.append(note)
        else:
            latest[entry["kind"]] = entry["event_at"]

    station = turn["station"]
    inbound = turn.get("inbound_number")
    outbound = turn.get("outbound_number")
    name = f"{inbound} → {outbound}" if inbound and outbound else outbound or inbound or "Рейс"
    reference = turn.get("planned_in") or turn.get("planned_out")
    date = utc(reference).strftime("%d %b %Y").upper() if reference else ""
    lines = [
        f"GROUND STATUS | {station}",
        name,
        f"{turn.get('registration') or 'Борт не указан'} · {date} · {'LT' if local else 'UTC'}",
    ]

    def section(title, allowed):
        values = [(key, label) for key, label in allowed if key in latest]
        if not values:
            return
        lines.extend(("", title))
        handled = set()
        for first, last, name in GROUPED_EVENTS:
            if first in latest and last in latest and any(key == first for key, _ in allowed):
                times = f"{_stamp_for(latest[first], station, local)} / {_stamp_for(latest[last], station, local)}"
                lines.append(f"{name}: {times}")
                handled.update((first, last))
        for key, label in values:
            if key not in handled:
                lines.append(f"{label}: {_stamp_for(latest[key], station, local)}")

    if turn.get("inbound_id") or any(key in latest for key, _ in ARRIVAL_EVENTS):
        section("ARRIVAL", ARRIVAL_EVENTS)
    if turn.get("outbound_id") or any(key in latest for key, _ in DEPARTURE_EVENTS):
        section("DEPARTURE", DEPARTURE_EVENTS)
    if not turn.get("inbound_id") and not turn.get("outbound_id"):
        section("EVENTS", (*ARRIVAL_EVENTS, *DEPARTURE_EVENTS))

    if notes:
        lines.extend(("", "NOTES", *(f"• {note}" for note in notes[-12:])))
        if len(notes) > 12:
            lines.append(f"Ещё {len(notes) - 12} заметок в журнале.")
    if not latest and not notes:
        lines.extend(("", "Пока нет ручных отметок.", "Напишите, например: посадка 1245z"))
    return "\n".join(lines)


async def _show_card(update, state, index):
    turn = state["cards"][index]
    if state.get("current") != index:
        state["local"] = False  # UTC is always the default for each card.
    state["current"] = index
    state["enabled"] = True
    await asyncio.to_thread(ground_turns.observe, turn)
    events = await asyncio.to_thread(ground_turns.events, turn["key"], limit=250)
    rows = [
        [
            _button(state, "UTC" if state.get("local") else "LT", "tz", index),
            _button(state, "Статистика станции", "stats", index),
        ],
        [_back(state)],
    ]
    await _render(update, _card_text(turn, events, local=state.get("local", False)), rows)


async def _show_station_stats(update, state, index):
    station = state["cards"][index]["station"]
    report = await asyncio.to_thread(ground_turns.station_report, station)
    lines = [
        f"СТАТИСТИКА СТАНЦИИ | {station}",
        "За последние 180 дней · ручные вводные",
        f"Рейсов с отметками: {report['turns']} · событий: {report['events']}",
        f"Заметок: {report['note_count']}",
        "",
        "СРЕДНИЕ ВРЕМЕНА",
    ]
    for label, metric in report["metrics"].items():
        if metric["count"]:
            lines.append(f"{label}: {metric['mean_minutes']:g} мин (n={metric['count']})")
    if lines[-1] == "СРЕДНИЕ ВРЕМЕНА":
        lines.append("Пока недостаточно завершённых интервалов.")
    most_common = sorted(
        report.get("event_counts", {}).items(),
        key=lambda item: -item[1],
    )[:5]
    if most_common:
        lines.extend(("", "ЧАСТЫЕ ЭТАПЫ"))
        lines.extend(
            f"{EVENTS.get(kind, kind)}: {count} рейс(ов)"
            for kind, count in most_common
        )
    lines.extend(("", "ПРОБЛЕМЫ И РИСКИ"))
    try:
        result = await interpret_station(report)
    except Exception:  # optional insights must not block numeric statistics
        lines.append("Анализ заметок сейчас недоступен. Тайминги показаны выше.")
    else:
        if result is None or not (result.frequent or result.critical):
            lines.append("По сохранённым заметкам выводов пока нет.")
        else:
            if result.frequent:
                lines.extend(("Повторяющиеся проблемы:", *(f"• {item}" for item in result.frequent)))
            if result.critical:
                lines.extend(("Критичные случаи из истории:", *(f"• {item}" for item in result.critical)))
            lines.append("Актуальность старых происшествий требует проверки.")
    await _render(update, "\n".join(lines), [[_return_to_card(state, index)]])


def _return_to_card(state, index):
    return _button(state, "← К карточке", "open", index)


async def _event_choices(update, state, index, *, checklist=False):
    turn = state["cards"][index]
    events = await asyncio.to_thread(ground_turns.events, turn["key"])
    latest = {
        entry["kind"]: entry["event_at"]
        for entry in sorted(events, key=lambda row: row["id"])
    }
    for key, field in (("IN_BLOCK", "actual_in"), ("OUT_BLOCK", "actual_out")):
        if turn.get(field) and key not in latest:
            latest[key] = turn[field]
    title = "Чек-лист" if checklist else "Отметить этап"
    lines = [f"{title} · {_label(turn)}"]
    rows = []
    for kind, label in EVENTS.items():
        if kind == "OUT_BLOCK" and not turn.get("outbound_id"):
            continue
        text = f"✓ {label} · {_stamp(latest[kind])}" if kind in latest else label
        action = "when" if checklist or kind in latest else "now"
        rows.append([_button(state, text, action, index, kind)])
    rows.append([_return_to_card(state, index)])
    await _render(update, "\n".join(lines), rows)


async def _history(update, state, index, user_id, *, ai=False):
    turn = state["cards"][index]
    remarks = await asyncio.to_thread(
        station_remarks,
        turn["station"],
        turn.get("aircraft_type"),
        exclude_turn=turn["key"],
    )
    lines = [f"Опыт станции · {_label(turn)}"]
    if remarks:
        if ai:
            leader = await choose_remark(remarks, ("ground", user_id, turn["key"]))
            if leader and leader in remarks:
                remarks = [leader, *(item for item in remarks if item != leader)]
        lines.extend(f"• {remark}" for remark in remarks)
    else:
        lines.append("Пока недостаточно сопоставимых записей по этапам.")
    rows = [[_return_to_card(state, index)]]
    if len(remarks) > 1:
        rows.insert(0, [_button(state, "ИИ · Главное для рейса", "historyai", index)])
    await _render(update, "\n".join(lines), rows)


async def _blockers(update, state, index):
    turn = state["cards"][index]
    events = await asyncio.to_thread(ground_turns.events, turn["key"])
    timing = turn_timing(turn, events)
    rows = [
        [_button(state, label, "reason", index, process)]
        for process, label in PROCESSES.items()
    ]
    rows.extend(
        [
            [
                _button(
                    state,
                    f"✓ Закрыть · {PROCESSES[item['process']]}",
                    "close",
                    index,
                    item["process"],
                )
            ]
            for item in timing["blockers"]
            if item["process"] in PROCESSES
        ]
    )
    rows.append([_return_to_card(state, index)])
    await _render(update, "Что сейчас задерживает подготовку?", rows)


async def _timeline(update, state, index):
    turn = state["cards"][index]
    events = await asyncio.to_thread(ground_turns.events, turn["key"], limit=20)
    lines = [f"Журнал · {_label(turn)}"]
    for event in events:
        kind = event["kind"]
        detail = event["detail"]
        label = EVENTS.get(kind)
        if kind == "TARGET":
            label = f"Цель ухода {_stamp(detail.get('target_at'), date=True)}"
        elif kind in {"BLOCKER_OPEN", "BLOCKER_CLOSE"}:
            label = f"{'Препятствие' if kind == 'BLOCKER_OPEN' else 'Снято'} · {PROCESSES.get(detail.get('process'), 'Другое')}"
            if detail.get("reason"):
                label += f" · {detail['reason']}"
        if label:
            lines.append(
                f"{_stamp(event['event_at'], date=True)} · {label}"
                f" · пользователь {event['actor_id']} · {event['source']}"
            )
    if len(lines) == 1:
        lines.append("Записей сотрудников пока нет.")
    await _render(update, "\n".join(lines), [[_return_to_card(state, index)]])


def _parse_time(text, now, *, past=False):
    if not TIME_INPUT.fullmatch(text):
        raise ValueError("Укажите время UTC в формате ЧЧ:ММ или ГГГГ-ММ-ДД ЧЧ:ММ.")
    supplied_date = " " in text
    if supplied_date:
        try:
            value = datetime.fromisoformat(text).replace(tzinfo=UTC)
        except ValueError:
            raise ValueError(
                "Укажите существующую дату в формате ГГГГ-ММ-ДД."
            ) from None
    else:
        hour, minute = map(int, text.split(":"))
        value = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if past and value - now > timedelta(hours=12):
            value -= timedelta(days=1)
        elif not past and now - value > timedelta(hours=12):
            value += timedelta(days=1)
    if past and value > now:
        raise ValueError("Фактическое время не может быть позже текущего UTC.")
    return value


def has_ground_turn_input(context):
    state = context.user_data.get(STATE_KEY)
    return bool(state and state["pending"])


def clear_ground_turn_input(context):
    if state := context.user_data.get(STATE_KEY):
        state["pending"] = None


async def _record(turn, kind, update, request_id, event_at, detail=None):
    return await asyncio.to_thread(
        ground_turns.record,
        turn,
        kind,
        actor_id=update.effective_user.id,
        event_at=event_at,
        request_id=request_id,
        detail=detail,
    )


async def _prompt(update, state, index, action, option=None):
    prompts = {
        "manual": "Фактическое время UTC · ЧЧ:ММ или ГГГГ-ММ-ДД ЧЧ:ММ",
        "target": "Цель ухода со стоянки UTC · ЧЧ:ММ или ГГГГ-ММ-ДД ЧЧ:ММ",
        "reason": "Что задерживает? Укажите причину и, если известно, время UTC.",
    }
    text = prompts[action]
    if action == "reason":
        text += "\nПример: Ждём топливную машину | 14:00\nЕсли срока нет, напишите только причину."
    state["pending"] = (index, action, option)
    await _render(update, text, [[_return_to_card(state, index)]])



EXPLICIT_FLIGHT = re.compile(r"^\s*([A-Z]{2,3}\s?-?\d{2,5})\b", re.IGNORECASE)


def ground_text_accepted(context, text):
    state = context.user_data.get(STATE_KEY)
    return bool(
        text.strip()
        and (
            (state and state.get("enabled") and state.get("current") is not None)
            or (EXPLICIT_FLIGHT.match(text) and text[EXPLICIT_FLIGHT.match(text).end():].strip())
        )
    )


def deactivate_ground(context):
    if state := context.user_data.get(STATE_KEY):
        state["enabled"] = False


async def handle_ground_text(update, context):
    """Record natural-language input against the selected or explicit Leon flight."""
    require_user(update)
    message = update.effective_message
    text = (message.text or "").strip()
    explicit_match = EXPLICIT_FLIGHT.match(text)
    explicit = (
        re.sub(r"[\s-]", "", explicit_match.group(1)).upper()
        if explicit_match else ""
    )
    state = context.user_data.get(STATE_KEY)
    if not explicit and not (state and state.get("enabled") and state.get("current") is not None):
        return
    try:
        _, records = await parse_ground_input(
            text, (update.effective_chat.id, update.effective_user.id)
        )
        # Only a flight number explicitly placed at the start of the message
        # can override the selected card. Never trust an inferred model ID.

        if not state or (explicit and not state.get("cards")):
            snapshot = await load_ground_flights()
            state = {
                "token": token_hex(5), "cards": snapshot["cards"], "snapshot": snapshot,
                "view": ("active", 0), "current": None, "local": False,
                "enabled": True, "pending": None,
            }
            context.user_data[STATE_KEY] = state

        if explicit:
            matches = [
                index for index, turn in enumerate(state["cards"])
                if any(
                    re.sub(r"[\s-]", "", str(turn.get(key) or "")).upper() == explicit
                    for key in ("inbound_number", "outbound_number")
                )
            ]
            if not matches:
                # Source may have been refreshed since the last visit.
                snapshot = await load_ground_flights(refresh=True)
                state["cards"] = snapshot["cards"]
                state["snapshot"] = snapshot
                matches = [
                    index for index, turn in enumerate(state["cards"])
                    if any(
                        re.sub(r"[\s-]", "", str(turn.get(key) or "")).upper() == explicit
                        for key in ("inbound_number", "outbound_number")
                    )
                ]
            if len(matches) != 1:
                raise ValueError(
                    f"Рейс {explicit} не найден." if not matches
                    else f"Найдено несколько рейсов {explicit}. Откройте нужную карточку."
                )
            index = matches[0]
        else:
            index = state["current"]

        turn = state["cards"][index]
        screen = getattr(update, "main_screen", None)
        if screen is not None and screen.generation != context.user_data.get("screen_generation", 0):
            return
        await asyncio.to_thread(
            ground_turns.record_many,
            turn,
            records,
            actor_id=update.effective_user.id,
            request_id=f"message:{update.effective_chat.id}:{message.message_id}",
        )
        state["enabled"] = True
        await _show_card(update, state, index)
    except GroundModelUnavailable:
        await message.reply_text("Qwen сейчас недоступен. Вводная не записана, попробуйте ещё раз.")
        return
    except (ValueError, OSError, sqlite3.Error, HTTPError, TimeoutError) as exc:
        await message.reply_text(str(exc) if isinstance(exc, ValueError)
                                 else "Вводную не удалось сохранить. Повторите попытку.")
        return

    # Keep operator chats clean, but never delete input before the write completes.
    with suppress(AttributeError, TelegramError):
        await context.bot.delete_message(
            chat_id=update.effective_chat.id, message_id=message.message_id
        )


async def handle_ground_turn_input(update, context):
    require_user(update)
    state = context.user_data.get(STATE_KEY)
    if not state or not state["pending"]:
        return
    index, action, option = state["pending"]
    turn = state["cards"][index]
    message = update.effective_message
    text = (message.text or "").strip()
    now = datetime.now(UTC)
    try:
        if action == "manual":
            kind, at, detail = option, _parse_time(text, now, past=True), None
        elif action == "target":
            kind, at = "TARGET", now
            detail = {"target_at": _parse_time(text, now).isoformat()}
        else:
            reason, delimiter, estimate = text.partition("|")
            reason = reason.strip()
            if not reason or len(reason) > 160:
                raise ValueError("Укажите краткую причину до 160 символов.")
            expected_at = None
            if delimiter and estimate.strip().casefold() not in {"неизвестно", "—"}:
                expected_at = _parse_time(estimate.strip(), now)
                if expected_at <= now:
                    raise ValueError("Прогноз должен быть позже текущего UTC.")
            kind, at = "BLOCKER_OPEN", now
            detail = {
                "process": option,
                "reason": reason,
                "expected_at": expected_at.isoformat() if expected_at else None,
            }
        await _record(
            turn,
            kind,
            update,
            f"message:{update.effective_chat.id}:{message.message_id}",
            at,
            detail,
        )
    except ValueError as exc:
        await _render(update, str(exc), [[_return_to_card(state, index)]])
        return
    except (OSError, sqlite3.Error):
        await _render(
            update,
            "Запись не сохранена. Повторите ввод.",
            [[_return_to_card(state, index)]],
        )
        return
    state["pending"] = None
    await _show_card(update, state, index)


async def ground_turn_callback(update, context):
    require_user(update)
    query = update.callback_query
    state = context.user_data.get(STATE_KEY)
    parts = (query.data or "").split(":")
    if not state or len(parts) not in {4, 5} or parts[:2] != ["gt", state["token"]]:
        await query.answer(
            "Карточка устарела. Откройте GROUND STATUS.", show_alert=True
        )
        return
    action, selected = parts[2:4]
    option = parts[4] if len(parts) == 5 else None
    if action == "refresh" and selected == "0" and option is None:
        await query.answer()
        await show_ground_turns(update, context, refresh=True)
        return
    if (
        action == "list"
        and selected in CATEGORIES
        and option
        and option.isascii()
        and option.isdigit()
    ):
        state["pending"] = None
        await query.answer()
        await _show_list(update, state, selected, int(option))
        return
    event_actions = {"when", "now", "manual"}
    process_actions = {"reason", "close"}
    simple_actions = {
        "open",
        "events",
        "check",
        "block",
        "history",
        "historyai",
        "target",
        "timeline",
        "tz",
        "stats",
    }
    if not (
        selected.isascii()
        and selected.isdigit()
        and int(selected) < len(state["cards"])
        and (
            (action in event_actions and option in EVENTS)
            or (action in process_actions and option in PROCESSES)
            or (action in simple_actions and option is None)
        )
    ):
        await query.answer("Действие недоступно.", show_alert=True)
        return
    state["pending"] = None
    index = int(selected)
    turn = state["cards"][index]
    if not turn.get("outbound_id") and (action == "target" or option == "OUT_BLOCK"):
        await query.answer("Вылет ещё не сопоставлен с рейсом Leon.", show_alert=True)
        return
    if action == "tz" and not state.get("local"):
        try:
            zone = _airport_catalog()[turn["station"]]["tz"]
            ZoneInfo(zone)
        except (KeyError, ValueError, ZoneInfoNotFoundError):
            await query.answer(
                "Часовой пояс аэропорта не найден. Оставлено UTC.",
                show_alert=True,
            )
            return
    await query.answer()
    try:
        if action == "tz":
            state["local"] = not state.get("local", False)
            await _show_card(update, state, index)
        elif action == "stats":
            await _show_station_stats(update, state, index)
        elif action in {"events", "check"}:
            await _event_choices(update, state, index, checklist=action == "check")
        elif action == "block":
            await _blockers(update, state, index)
        elif action in {"history", "historyai"}:
            await _history(
                update, state, index, update.effective_user.id, ai=action == "historyai"
            )
        elif action == "timeline":
            await _timeline(update, state, index)
        elif action in {"manual", "target", "reason"}:
            await _prompt(update, state, index, action, option)
        elif action == "when":
            await _render(
                update,
                f"{EVENTS[option]} · когда было событие?",
                [
                    [
                        _button(state, "Сейчас", "now", index, option),
                        _button(state, "Ввести время UTC", "manual", index, option),
                    ],
                    [_return_to_card(state, index)],
                ],
            )
        else:
            if action in {"now", "close"}:
                kind = option if action == "now" else "BLOCKER_CLOSE"
                detail = {"process": option} if action == "close" else None
                await _record(
                    turn,
                    kind,
                    update,
                    f"callback:{query.id}",
                    datetime.now(UTC),
                    detail,
                )
            await _show_card(update, state, index)
    except DATA_ERRORS:
        await _render(
            update,
            "GROUND STATUS · Не удалось обработать данные. Откройте карточку и проверьте запись.",
            [[_return_to_card(state, index), _back(state)]],
        )
