"""Scheduled aircraft positions, upcoming flights and station notes."""

import asyncio
from datetime import UTC, datetime, timedelta
from secrets import token_hex
from time import monotonic

from httpx import HTTPError
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from bot.admission import require_user
from integrations.leon.tools import get_flight_schedule
from services.fleet_operations import (
    build_fleet,
    load_position_notes,
)
from services.fleet_operations import current_notes as current_notes
from services.fleet_operations import station_for_aircraft as station_for_aircraft
from services.snapshot_models import zulu
from services.station_notes import notes

PAGE_SIZE = 4
CACHE_SECONDS = 600


def status_lines(item):
    active = item["active"]
    last = item["last"]
    if active:
        return [
            "Плановое выполнение: "
            f"{active.get('departure') or '—'} → {active.get('arrival') or '—'}",
            f"По расписанию: {zulu(active.get('departureUTC'))} → "
            f"{zulu(active.get('arrivalUTC'))}",
            "Фактическая позиция не подтверждена.",
        ]
    if last:
        return [
            f"Последнее плановое прибытие: {last.get('arrival') or '—'} · "
            f"{zulu(last.get('arrivalUTC'))}",
            "Стоянка и фактическое прибытие не подтверждены.",
        ]
    return ["Позиция: нет истории прибытия в загруженном периоде."]


def next_flight_lines(item):
    flight = item["next"]
    if not flight:
        return ["Следующий рейс: нет в периоде выборки."]
    return [
        f"Следующий: {flight.get('flightNumber') or '—'} · "
        f"{flight.get('departure') or '—'} → {flight.get('arrival') or '—'}",
        f"Вылет по Leon: {zulu(flight.get('departureUTC'))}",
    ]


def note_label(row):
    status = (
        "OPS confirmed" if row.get("verified_by") else "Неподтверждённое наблюдение"
    )
    if row.get("pinned"):
        status += " · закреплено"
    if row.get("older"):
        status += " · старше 30 дней"
    return f"{status} · {zulu(row.get('observed_at_utc'))}"


def trim_note(text, length=110):
    text = " ".join(str(text).split())
    return text if len(text) <= length else text[: length - 1] + "…"


def notes_info(state, station):
    if station is None:
        return ["Заметки по позиции: аэропорт не определён."]
    if state["notes_access"] == "unbound":
        return ["Заметки по позиции: нужна подтверждённая привязка сотрудника."]
    if state["notes_access"] != "ready":
        return ["Заметки по позиции: источник недоступен."]
    rows = state["notes"].get(station)
    if rows is None:
        return [f"Заметки {station}: не удалось загрузить."]
    if not rows:
        return [f"Заметки {station}: нет актуальных записей (30 дней)."]
    first = rows[0]
    return [
        f"Заметки {station}: {len(rows)} показано, статус: "
        f"{'OPS confirmed' if first.get('verified_by') else 'report'}",
        trim_note(first["text"], 90),
    ]


def brief_page(state, page=0):
    fleet = state["fleet"]
    pages = max(1, (len(fleet) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    lines = [
        "OPS BRIEF · ФЛОТ / ПОЗИЦИИ",
        f"Leon · получено {zulu(state['fetched_at'])}",
        "Позиции расчётные по расписанию, НЕ live tracking.",
        "История: 21 день назад · план: 7 дней вперёд (UTC).",
    ]
    if not state["complete"]:
        lines.append("ВНИМАНИЕ: расписание загружено не полностью.")
    if not fleet:
        lines.append("Нет бортов с рейсами в периоде выборки.")
    rows = []
    for index in range(page * PAGE_SIZE, min((page + 1) * PAGE_SIZE, len(fleet))):
        item = fleet[index]
        lines.extend(
            [
                "",
                f"{item['registration']} · {item['type']}",
                *status_lines(item),
                *next_flight_lines(item),
                *notes_info(state, station_for_aircraft(item)),
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(
                    f"{item['registration']} · детали / заметки",
                    callback_data=f"opb:{state['token']}:detail:{index}",
                )
            ]
        )
    nav = []
    if page:
        nav.append(
            InlineKeyboardButton(
                "←", callback_data=f"opb:{state['token']}:page:{page - 1}"
            )
        )
    nav.append(
        InlineKeyboardButton(
            f"{page + 1}/{pages}",
            callback_data=f"opb:{state['token']}:page:{page}",
        )
    )
    if page + 1 < pages:
        nav.append(
            InlineKeyboardButton(
                "→", callback_data=f"opb:{state['token']}:page:{page + 1}"
            )
        )
    rows.append(nav)
    rows.append(
        [
            InlineKeyboardButton(
                "↻ Обновить", callback_data=f"opb:{state['token']}:refresh:0"
            )
        ]
    )
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def brief_detail(state, index):
    item = state["fleet"][index]
    station = station_for_aircraft(item)
    lines = [
        f"OPS BRIEF · {item['registration']} · {item['type']}",
        *status_lines(item),
        "",
        *next_flight_lines(item),
        "",
    ]
    lines += [f"ЗАМЕЧАНИЯ И ЗАМЕТКИ · {station or 'ПОЗИЦИЯ НЕИЗВЕСТНА'}"]
    if station is None or state["notes_access"] != "ready":
        lines += notes_info(state, station)
    else:
        rows = state["notes"].get(station)
        if rows is None or not rows:
            lines += notes_info(state, station)
        else:
            for note in rows:
                lines.extend(
                    [
                        "",
                        f"#{note['id']} · {note_label(note)}",
                        note["text"][:700],
                    ]
                )
    lines.extend(
        [
            "",
            "Заметки являются наблюдениями сотрудников, а не подтверждением",
            "текущего состояния ВС или аэродрома. Дата наблюдения указана.",
        ]
    )
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "← К флоту",
                    callback_data=f"opb:{state['token']}:page:{index // PAGE_SIZE}",
                )
            ],
            [
                InlineKeyboardButton(
                    "↻ Обновить",
                    callback_data=f"opb:{state['token']}:refresh:0",
                )
            ],
        ]
    )
    return "\n".join(lines), keyboard


async def load_ops_brief(user_id):
    now = datetime.now(UTC)
    start = (now - timedelta(days=21)).date()
    end = (now + timedelta(days=7)).date()
    schedule = await asyncio.wait_for(
        get_flight_schedule(start.isoformat(), end.isoformat()), timeout=75
    )
    if not isinstance(schedule, dict) or not isinstance(schedule.get("flights"), list):
        raise TypeError("Leon schedule missing")
    fleet = build_fleet(schedule, now)
    notes_access, station_notes = await load_position_notes(fleet, user_id, now, notes)
    return {
        "token": token_hex(5),
        "fleet": fleet,
        "complete": schedule.get("complete") is True,
        "fetched_at": now.isoformat(),
        "expires": monotonic() + CACHE_SECONDS,
        "notes_access": notes_access,
        "notes": station_notes,
    }


async def show_ops_brief(update, context):
    require_user(update)
    try:
        state = await load_ops_brief(update.effective_user.id)
    except (HTTPError, TimeoutError, ValueError, OSError, RuntimeError, TypeError):
        await update.effective_message.reply_text(
            "OPS BRIEF · Не удалось загрузить расписание Leon. Повторите позже."
        )
        return
    context.user_data["ops_brief_state"] = state
    text, markup = brief_page(state)
    await update.effective_message.reply_text(text, reply_markup=markup)


async def ops_brief_callback(update, context):
    require_user(update)
    query = update.callback_query
    parts = (query.data or "").split(":")
    state = context.user_data.get("ops_brief_state")
    if (
        len(parts) != 4
        or parts[0] != "opb"
        or parts[2] not in {"page", "detail", "refresh"}
        or not parts[3].isdigit()
        or not state
        or state["token"] != parts[1]
        or state["expires"] < monotonic()
    ):
        await query.answer(
            "Данные устарели. Откройте OPS BRIEF заново.", show_alert=True
        )
        return
    index = int(parts[3])
    if parts[2] == "detail" and index >= len(state["fleet"]):
        await query.answer("Борт не найден.", show_alert=True)
        return
    await query.answer()
    if parts[2] == "refresh":
        await show_ops_brief(update, context)
        return
    if parts[2] == "page":
        text, markup = brief_page(state, index)
    else:
        text, markup = brief_detail(state, index)
    await update.effective_message.reply_text(text, reply_markup=markup)
