"""Read-only fleet availability based on Leon schedule and existing duty services.

A scheduled movement is not an independently verified aircraft position.
No unverified crew availability, MEL clearance or operator policy is inferred.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from secrets import token_hex
from time import monotonic

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from bot.admission import require_user
from integrations.leon.tools import get_flight_crew, get_flight_schedule
from services.crew_format import format_flight_crew
from services.flight_duty import load_duty
from services.snapshot_models import utc, zulu

CACHE_SECONDS = 600
PAGE_SIZE = 6


def build_fleet(schedule, now=None):
    """Group complete, non-cancelled Leon schedule by actual registration."""
    now = now or datetime.now(UTC)
    groups = {}
    for flight in schedule.get("flights") or []:
        if not isinstance(flight, dict) or flight.get("cancelled") is True:
            continue
        registration = flight.get("aircraft")
        departure = utc(flight.get("departureUTC"))
        arrival = utc(flight.get("arrivalUTC"))
        if not isinstance(registration, str) or not registration.strip():
            continue
        if not departure or not arrival or arrival <= departure:
            continue
        groups.setdefault(registration.strip().upper(), []).append(flight)
    result = []
    for registration, flights in sorted(groups.items()):
        flights.sort(key=lambda f: utc(f["departureUTC"]))
        active = next(
            (
                f
                for f in flights
                if utc(f["departureUTC"]) <= now < utc(f["arrivalUTC"])
            ),
            None,
        )
        previous = [f for f in flights if utc(f["arrivalUTC"]) <= now]
        upcoming = [f for f in flights if utc(f["departureUTC"]) > now]
        last = max(previous, key=lambda f: utc(f["arrivalUTC"])) if previous else None
        next_flight = (
            min(upcoming, key=lambda f: utc(f["departureUTC"])) if upcoming else None
        )
        result.append(
            {
                "registration": registration,
                "type": next(
                    (str(f["aircraftType"]) for f in flights if f.get("aircraftType")),
                    "—",
                ),
                "active": active,
                "last": last,
                "next": next_flight,
                "flights": flights,
            }
        )
    return result


def position_label(item):
    if item["active"]:
        f = item["active"]
        return (
            f"В полёте по расписанию: {f.get('departure') or '—'} → "
            f"{f.get('arrival') or '—'}; факт не проверен"
        )
    if item["last"]:
        f = item["last"]
        return f"Последнее плановое прибытие: {f.get('arrival') or '—'}; позиция не подтверждена"
    return "Позиция: нет подтверждённых данных"


def departure_label(flight):
    if not flight:
        return "Следующий рейс: не найден в загруженном периоде"
    return (
        f"Следующий рейс: {flight.get('flightNumber') or '—'} · "
        f"{flight.get('departure') or '—'} → {flight.get('arrival') or '—'} · "
        f"{zulu(flight.get('departureUTC'))}"
    )


def fleet_page(token, state, page=0):
    fleet = state["fleet"]
    pages = max(1, (len(fleet) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(max(0, page), pages - 1)
    lines = [
        "CHARTER CHECK · Fleet Availability",
        f"Leon MCP · {zulu(state['fetched_at'])}",
        "Период: -2 / +7 дней (UTC). Это данные расписания, не live tracking.",
        "",
    ]
    if not state["complete"]:
        lines.append("ВНИМАНИЕ: расписание неполное. Доступность ВС не подтверждена.")
    if not fleet:
        lines.append("Борта не найдены в загруженном расписании.")
    rows = []
    for idx in range(page * PAGE_SIZE, min((page + 1) * PAGE_SIZE, len(fleet))):
        item = fleet[idx]
        lines.extend(
            [
                f"{item['registration']} · {item['type']}",
                position_label(item),
                departure_label(item["next"]),
                "",
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(
                    f"{item['registration']} · подробнее",
                    callback_data=f"char:{token}:detail:{idx}",
                )
            ]
        )
    nav = []
    if page:
        nav.append(
            InlineKeyboardButton("←", callback_data=f"char:{token}:page:{page - 1}")
        )
    nav.append(
        InlineKeyboardButton(
            f"{page + 1}/{pages}", callback_data=f"char:{token}:page:{page}"
        )
    )
    if page + 1 < pages:
        nav.append(
            InlineKeyboardButton("→", callback_data=f"char:{token}:page:{page + 1}")
        )
    rows.append(nav)
    rows.append(
        [
            InlineKeyboardButton(
                "↻ Обновить Leon", callback_data=f"char:{token}:refresh:0"
            )
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                "🧮 Расчёт чартера · E35L DEMO", callback_data="quote:menu:0"
            )
        ]
    )
    return "\n".join(lines).strip(), InlineKeyboardMarkup(rows)


async def load_fleet():
    now = datetime.now(UTC)
    start = (now - timedelta(days=2)).date().isoformat()
    end = (now + timedelta(days=7)).date().isoformat()
    result = await asyncio.wait_for(get_flight_schedule(start, end), timeout=70)
    if not isinstance(result, dict) or not isinstance(result.get("flights"), list):
        raise TypeError("Leon вернул некорректный формат расписания.")
    return {
        "fleet": build_fleet(result, now),
        "complete": result.get("complete") is True,
        "fetched_at": now.isoformat(),
        "expires": monotonic() + CACHE_SECONDS,
        "token": token_hex(5),
    }


async def show_charter(update, context):
    require_user(update)
    try:
        state = await load_fleet()
    except (TimeoutError, ValueError, OSError, RuntimeError, TypeError):
        await update.effective_message.reply_text(
            "CHARTER CHECK · Не удалось получить данные Leon. Попробуйте позже."
        )
        return
    context.user_data["charter_fleet"] = state
    text, markup = fleet_page(state["token"], state)
    await update.effective_message.reply_text(text, reply_markup=markup)


async def detail_text(item, complete):
    lines = [
        f"CHARTER CHECK · {item['registration']} · {item['type']}",
        position_label(item),
        departure_label(item["next"]),
    ]
    next_flight = item["next"]
    if not complete:
        lines.append("Расписание неполное: отсутствие конфликта не подтверждено.")
    if (
        next_flight
        and type(next_flight.get("flightNid")) is int
        and next_flight["flightNid"] > 0
    ):
        try:
            crew = await asyncio.wait_for(
                get_flight_crew(next_flight["flightNid"]), timeout=30
            )
            lines.extend(
                [
                    "",
                    "Назначенный экипаж следующего рейса · Leon",
                    format_flight_crew(
                        next_flight.get("flightNumber") or "—",
                        str(next_flight["departureUTC"])[:10],
                        crew.get("crew"),
                        compact=True,
                        flight_nid=next_flight["flightNid"],
                    ),
                ]
            )
            try:
                block = await asyncio.wait_for(
                    load_duty(next_flight, crew.get("crew")), timeout=60
                )
                lines.extend(["", block.text[:2100]])
            except (TimeoutError, ValueError, OSError, RuntimeError, TypeError):
                lines.append("Duty / FTL: источник недоступен.")
        except (TimeoutError, ValueError, OSError, RuntimeError, TypeError):
            lines.append("Назначенный экипаж: источник недоступен.")
    else:
        lines.append("Экипаж: нет следующего рейса с подтверждённым идентификатором.")
    lines.extend(
        [
            "",
            "Остаток доступного FDP для нового чартера: не рассчитан.",
            "Свободные экипажи: полный реестр доступности не подключён.",
            "MEL / технические ограничения: реестр не подключён.",
            "Внутренняя политика и запреты: утверждённый реестр не подключён.",
            "Наличие окна в расписании не означает готовность к чартеру.",
        ]
    )
    return "\n".join(lines)


async def charter_callback(update, context):
    require_user(update)
    query = update.callback_query
    parts = (query.data or "").split(":")
    state = context.user_data.get("charter_fleet")
    if (
        len(parts) != 4
        or parts[0] != "char"
        or parts[2] not in {"page", "detail", "refresh"}
        or not parts[3].isdigit()
        or not state
        or state["token"] != parts[1]
        or state["expires"] < monotonic()
    ):
        await query.answer(
            "Данные устарели. Откройте CHARTER CHECK снова.", show_alert=True
        )
        return
    index = int(parts[3])
    if parts[2] == "detail" and index >= len(state["fleet"]):
        await query.answer("Борт не найден.", show_alert=True)
        return
    await query.answer()
    if parts[2] == "refresh":
        await show_charter(update, context)
        return
    if parts[2] == "page":
        text, markup = fleet_page(state["token"], state, index)
    else:
        text = await detail_text(state["fleet"][index], state["complete"])
        markup = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "← К списку ВС",
                        callback_data=f"char:{state['token']}:page:{index // PAGE_SIZE}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "↻ Обновить Leon",
                        callback_data=f"char:{state['token']}:refresh:0",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "🧮 Расчёт чартера · E35L DEMO",
                        callback_data="quote:menu:0",
                    )
                ],
            ]
        )
    await update.effective_message.reply_text(text, reply_markup=markup)
