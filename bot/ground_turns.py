"""Shared ground-handling cards and event entry in private Telegram chats."""

import asyncio
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from secrets import token_hex

from httpx import HTTPError
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from bot.admission import require_user
from bot.message_edits import edit_message_text
from services.ground_flights import load_ground_flights
from services.ground_timing import choose_remark, station_remarks, turn_timing
from services.ground_turns import ground_turns
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


def _variance(label, value):
    return f"{label} {value:+g} мин" if value is not None else ""


def _card_text(turn, timing, remark):
    lines = [f"GROUND STATUS · {_label(turn)}"]
    if turn.get("inbound_number") and turn.get("outbound_number"):
        lines.append(f"{turn['inbound_number']} → {turn['outbound_number']}")
    lines.extend(
        [
            f"Исходный план ухода {_stamp(timing['baseline_out'])} · цель {_stamp(timing['target'])}",
            f"На стоянке {_stamp(timing['in'])}",
            f"Готовность не раньше {_stamp(timing['ready_not_before'])} · "
            f"доложена готовность {_stamp(timing['ready'])}",
            f"Факт ухода {_stamp(timing['out'])}",
        ]
    )
    if turn.get("estimated_landing") and not turn.get("actual_in"):
        lines.append(f"Ожидаемая посадка {_stamp(turn['estimated_landing'])}")
    if turn.get("estimated_out") and not timing["out"]:
        lines.append(f"Оценка ухода Leon {_stamp(turn['estimated_out'])}")
    if timing.get("unknown_blockers"):
        lines.append("У части препятствий срок неизвестен или устарел.")
    if timing["risk_minutes"] is not None and timing["risk_minutes"] > 0:
        lines.append(f"Риск к цели +{timing['risk_minutes']:g} мин")
    variances = [
        _variance("Уход от плана", timing.get("departure_delta_minutes")),
        _variance("Привезено", timing["inbound_delta_minutes"]),
        _variance("На станции", timing["station_delta_minutes"]),
    ]
    if measured := [part for part in variances if part]:
        lines.append(" · ".join(measured))
    if timing["after_ready_minutes"] is not None:
        lines.append(
            f"От доклада готовности до ухода {timing['after_ready_minutes']:g} мин"
        )
    if timing.get("movement_conflicts"):
        lines.append(
            "Времена Leon и отметки сотрудников расходятся. Сверьте события в журнале."
        )
    for blocker in timing["blockers"]:
        process = PROCESSES.get(blocker["process"], blocker["process"])
        lines.append(
            f"Ждём · {process} · {blocker.get('reason') or 'причина уточняется'}"
            f" · прогноз {_stamp(blocker.get('expected_at'))}"
        )
    if not timing["blockers"]:
        lines.append("Открытых препятствий в журнале нет.")
    if remark:
        lines.extend(("", f"Опыт станции · {remark}"))
    return "\n".join(lines)


async def _show_card(update, state, index):
    turn = state["cards"][index]
    await asyncio.to_thread(ground_turns.observe, turn)
    events, baseline, remarks = await asyncio.gather(
        asyncio.to_thread(ground_turns.events, turn["key"]),
        asyncio.to_thread(ground_turns.baseline, turn["key"]),
        asyncio.to_thread(
            station_remarks,
            turn["station"],
            turn.get("aircraft_type"),
            exclude_turn=turn["key"],
        ),
    )
    timing = turn_timing(turn, events, baseline=baseline)
    rows = [
        [
            _button(state, "Отметить этап", "events", index),
            _button(state, "Сообщить задержку", "block", index),
        ],
        [
            _button(state, "Чек-лист", "check", index),
            _button(state, "Опыт станции", "history", index),
        ],
        [
            _button(state, "Журнал", "timeline", index),
            _button(state, "↻ Статус", "open", index),
        ],
    ]
    if turn.get("outbound_id"):
        rows.append(
            [_button(state, "Уточнить цель UTC", "target", index), _back(state)]
        )
    else:
        rows.append([_back(state)])
    await _render(update, _card_text(turn, timing, remarks[0] if remarks else ""), rows)


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
    await query.answer()
    try:
        if action in {"events", "check"}:
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
