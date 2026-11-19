"""Charter fleet, requests and historical flight statistics."""

import asyncio
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from secrets import token_hex
from time import monotonic

from httpx import HTTPError
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from bot.admission import private_user, require_user
from bot.message_edits import edit_message_text
from config.settings import settings
from integrations.leon.tools import get_flight_crew, get_flight_schedule
from services.charter_intelligence import (
    charter_request_sources,
    list_charter_requests,
    load_charter_request,
    render_charter_request,
)
from services.charter_recommendations import review_charter
from services.crew_format import format_flight_crew
from services.flight_duty import load_duty
from services.journey_history import journey_history, refresh_journey_history
from services.snapshot_models import utc, zulu

CACHE_SECONDS = 600
PAGE_SIZE = 6
HISTORY_PERIODS = (30, 90)
ROUTE_SELECTION = re.compile(
    r"(30|90)-([A-Z0-9]{4})-([A-Z0-9]{4})(?:-([A-Z0-9]{2,4}))?"
)


def can_view_charter_sales(update):
    if not private_user(update):
        return False
    configured = settings.charter_sales_user_ids.split(",")
    user_id = update.effective_user.id
    return user_id in settings.admin_ids or str(user_id) in {
        part.strip() for part in configured
    }


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
    rows.append([charter_button(token, "← Разделы", "menu")])
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
    }


def charter_button(token, label, action, value=0):
    return InlineKeyboardButton(label, callback_data=f"char:{token}:{action}:{value}")


def charter_menu(update, token):
    rows = [[charter_button(token, "Флот и расписание", "refresh")]]
    if can_view_charter_sales(update):
        rows.append([charter_button(token, "Запросы и варианты офферов", "sales")])
    rows.extend(
        [
            [charter_button(token, "Фактические рейсы · Journey Log", "journey", 30)],
            [InlineKeyboardButton("Демо-калькулятор", callback_data="quote:menu:0")],
        ]
    )
    return "CHARTER CHECK · Выберите раздел", InlineKeyboardMarkup(rows)


async def show_charter(update, context):
    require_user(update)
    token = token_hex(5)
    context.user_data["charter_check"] = {
        "token": token,
        "expires": monotonic() + CACHE_SECONDS,
        "revision": 0,
    }
    text, markup = charter_menu(update, token)
    query = getattr(update, "callback_query", None)
    if query:
        await edit_message_text(query, text, reply_markup=markup)
    else:
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
            except (
                HTTPError,
                TimeoutError,
                ValueError,
                OSError,
                RuntimeError,
                TypeError,
            ):
                lines.append("Duty / FTL: источник недоступен.")
        except (HTTPError, TimeoutError, ValueError, OSError, RuntimeError, TypeError):
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


async def fleet_view(state, action, index):
    token = state["token"]
    if action == "refresh":
        state["fleet_data"] = await load_fleet()
        index = 0
    fleet = state.get("fleet_data")
    if not fleet:
        raise ValueError("Fleet snapshot missing")
    if action != "detail":
        return fleet_page(token, fleet, index)
    if index >= len(fleet["fleet"]):
        raise ValueError("Aircraft index invalid")
    text = await detail_text(fleet["fleet"][index], fleet["complete"])
    markup = InlineKeyboardMarkup(
        [
            [charter_button(token, "← К списку ВС", "page", index // PAGE_SIZE)],
            [charter_button(token, "↻ Обновить Leon", "refresh")],
            [charter_button(token, "← Разделы", "menu")],
        ]
    )
    return text, markup


def sales_page(token, requests, page):
    pages = max(1, (len(requests) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(page, pages - 1)
    start = page * PAGE_SIZE
    rows = [
        [charter_button(token, request["label"][:64], "rfq", request["id"])]
        for request in requests[start : start + PAGE_SIZE]
    ]
    nav = []
    if page:
        nav.append(charter_button(token, "←", "quotes", page - 1))
    if page + 1 < pages:
        nav.append(charter_button(token, "→", "quotes", page + 1))
    if nav:
        rows.append(nav)
    rows.extend(
        [
            [charter_button(token, "↻ Обновить Leon Sales", "sales")],
            [charter_button(token, "← Разделы", "menu")],
        ]
    )
    text = f"CHARTER CHECK · Запросы Leon Sales · {page + 1}/{pages}"
    if not requests:
        text += "\nНет запросов в полученной выборке."
    return text, InlineKeyboardMarkup(rows)


async def sales_view(state, action, value, user_id):
    token = state["token"]
    if action == "sales":
        state["requests"] = await list_charter_requests()
        state.pop("request_data", None)
    requests = state.get("requests", [])
    if action in {"sales", "quotes"}:
        return sales_page(token, requests, value)
    page = next(
        (
            idx // PAGE_SIZE
            for idx, request in enumerate(requests)
            if request["id"] == value
        ),
        None,
    )
    if page is None:
        raise ValueError("Request missing from current list")
    rows = [
        [charter_button(token, "← К запросам", "quotes", page)],
        [charter_button(token, "← Разделы", "menu")],
    ]
    if action == "rfqai":
        snapshot = state.get("request_data")
        if not snapshot or snapshot["id"] != value:
            raise ValueError("Request evidence expired")
        text = await review_charter(
            charter_request_sources(snapshot["data"]),
            ("charter", "rfq", user_id, value),
        )
        rows.insert(0, [charter_button(token, "← К офферу", "rfq", value)])
    else:
        data = await load_charter_request(value)
        state["request_data"] = {"id": value, "data": data}
        text = render_charter_request(data)
        rows.insert(0, [charter_button(token, "ИИ · разбор вариантов", "rfqai", value)])
    return text, InlineKeyboardMarkup(rows)


def history_range(days):
    end = datetime.now(UTC).date() - timedelta(days=1)
    return end - timedelta(days=days - 1), end


def sample_text(label, stats, unit, *, scale=1, signed=False):
    count = stats["count"]
    if not count:
        return f"{label} · нет данных"
    number_format = "+.1f" if signed else ".1f"
    return (
        f"{label} · всего {stats['total'] / scale:{number_format}} {unit}, "
        f"среднее {stats['mean'] / scale:{number_format}}, "
        f"медиана {stats['median'] / scale:{number_format}} {unit} · n={count}"
    )


def journey_sources(stats, route):
    fields = (
        "start",
        "end",
        "complete",
        "covered_days",
        "last_synced_at",
        "scheduled_count",
        "actual_count",
        "cancelled_count",
        "open_log_count",
        "missing_log_count",
        "unknown_log_count",
        "reported_ferry_count",
        "reported_unknown_ferry_count",
        "block_minutes",
        "block_delta_minutes",
        "airborne_minutes",
        "distance_nm",
        "fuel",
        "fuel_uplift",
        "fuel_missing_unit_count",
    )
    evidence = {key: stats[key] for key in fields}
    evidence.update(
        {key: stats[key][:8] for key in ("routes", "airports", "aircraft_types")}
    )
    if route:
        evidence["filter"] = {
            "departure": route[0],
            "arrival": route[1],
            "aircraft_type": route[2],
        }
    return {"LEON_JOURNEY_LOG": evidence}


def journey_page(stats, route):
    lines = [
        "CHARTER CHECK · Фактические рейсы Leon Journey Log",
        f"{stats['start']} — {stats['end']} · по датам расписания UTC",
    ]
    if route:
        aircraft = f" · {route[2]}" if route[2] else ""
        lines.append(f"Фактический маршрут {route[0]} → {route[1]}{aircraft}")
    if stats["last_synced_at"]:
        lines.append(f"Обновлено {zulu(stats['last_synced_at'])}")
        if not stats["complete"]:
            lines.append(f"Полностью проверено дней: {stats['covered_days']}.")
    else:
        lines.append("Местная выборка за этот период ещё не загружена.")
        return "\n".join(lines)
    if not route:
        lines.append(
            f"Рейсов в расписании {stats['scheduled_count']} · "
            f"с фактическими данными {stats['actual_count']} · "
            f"отменено {stats['cancelled_count']}"
        )
        lines.append(
            f"Журналов в работе {stats['open_log_count']} · "
            f"отсутствует {stats['missing_log_count']} · "
            f"статус неизвестен {stats['unknown_log_count']}"
        )
    else:
        lines.append(f"Рейсов с фактическими данными {stats['actual_count']}")
    if stats["actual_count"]:
        lines.append(
            f"Из них помечено перегоном в расписании {stats['reported_ferry_count']} · "
            f"без классификации {stats['reported_unknown_ferry_count']}"
        )
    lines.extend(
        [
            "",
            sample_text("Блок-время", stats["block_minutes"], "ч", scale=60),
            sample_text("В воздухе", stats["airborne_minutes"], "ч", scale=60),
            sample_text(
                "Блок-время: факт минус расписание",
                stats["block_delta_minutes"],
                "мин",
                signed=True,
            ),
            sample_text("Дистанция по Journey Log", stats["distance_nm"], "NM"),
        ]
    )
    for field, label in (("fuel", "Расход топлива"), ("fuel_uplift", "Заправлено")):
        fuel = stats[field]
        if fuel:
            lines.extend(
                sample_text(label, sample, unit) for unit, sample in fuel.items()
            )
        else:
            lines.append(f"{label} · нет данных")
    if stats["fuel_missing_unit_count"]:
        lines.append(
            f"Записей о расходе без единицы: {stats['fuel_missing_unit_count']}."
        )
    if not route and stats["airports"]:
        airports = ", ".join(
            f"{item['icao']} ({item['count']})" for item in stats["airports"][:8]
        )
        lines.append(f"\nАэропорты по числу рейсов: {airports}")
    if stats["aircraft_types"] and not (route and route[2]):
        kinds = ", ".join(
            f"{item['type']} ({item['count']})" for item in stats["aircraft_types"][:8]
        )
        lines.append(f"Типы ВС в выборке: {kinds}")
    return "\n".join(lines)


async def journey_view(state, action, days, route, user_id):
    token = state["token"]
    start, end = history_range(days)
    if action == "sync":
        stats = await refresh_journey_history(start, end)
    else:
        filters = (
            {"departure": route[0], "arrival": route[1], "aircraft_type": route[2]}
            if route
            else {}
        )
        stats = await asyncio.to_thread(journey_history.summary, start, end, **filters)
    rows = [
        [
            charter_button(token, f"{period} дней", "journey", period)
            for period in HISTORY_PERIODS
        ],
        [charter_button(token, "↻ Загрузить из Leon", "sync", days)],
        [charter_button(token, "← Разделы", "menu")],
    ]
    if route:
        route_value = "-".join((str(days), *(part for part in route if part)))
    if action in {"journeyai", "routeai"} and stats["actual_count"]:
        text = await review_charter(
            journey_sources(stats, route),
            ("charter", "journey", user_id, days, route),
        )
        if route:
            rows.insert(
                0, [charter_button(token, "← К маршруту", "route", route_value)]
            )
    else:
        text = journey_page(stats, route)
        if stats["actual_count"]:
            ai_action, value = (
                ("routeai", route_value) if route else ("journeyai", days)
            )
            rows.insert(
                0, [charter_button(token, "ИИ · разбор статистики", ai_action, value)]
            )
        if not route:
            route_rows = []
            for item in stats["routes"][:5]:
                dep, arr = item["departure"], item["arrival"]
                value = f"{days}-{dep}-{arr}"
                if ROUTE_SELECTION.fullmatch(value):
                    route_rows.append(
                        [
                            charter_button(
                                token,
                                f"{dep} → {arr} · {item['count']}",
                                "route",
                                value,
                            )
                        ]
                    )
            rows[:0] = route_rows
        elif route[2]:
            value = f"{days}-{route[0]}-{route[1]}"
            rows.insert(
                0, [charter_button(token, "← Все типы на маршруте", "route", value)]
            )
        elif len(stats["aircraft_types"]) > 1:
            type_buttons = []
            for item in stats["aircraft_types"][:5]:
                kind = item["type"]
                value = f"{route_value}-{kind}"
                if ROUTE_SELECTION.fullmatch(value):
                    type_buttons.append(charter_button(token, kind, "route", value))
            if type_buttons:
                rows.insert(0, type_buttons)
    return text, InlineKeyboardMarkup(rows)


async def charter_callback(update, context):
    require_user(update)
    query = update.callback_query
    parts = (query.data or "").split(":", 3)
    state = context.user_data.get("charter_check")
    if (
        len(parts) != 4
        or parts[0] != "char"
        or not state
        or parts[1] != state["token"]
        or state["expires"] < monotonic()
    ):
        await query.answer(
            "Данные устарели. Откройте CHARTER CHECK снова.", show_alert=True
        )
        return
    action, selection = parts[2:]
    sales_actions = {"sales", "quotes", "rfq", "rfqai"}
    journey_actions = {"journey", "sync", "journeyai", "route", "routeai"}
    fleet_actions = {"refresh", "page", "detail"}
    route = None
    if action in {"route", "routeai"}:
        match = ROUTE_SELECTION.fullmatch(selection)
        if match:
            selection = match[1]
            route = match[2], match[3], match[4]
    if (
        action not in sales_actions | journey_actions | fleet_actions | {"menu"}
        or not selection.isascii()
        or not selection.isdigit()
        or len(selection) > 18
        or (action in {"route", "routeai"} and not route)
        or (action in journey_actions and int(selection) not in HISTORY_PERIODS)
    ):
        await query.answer("Раздел недоступен.", show_alert=True)
        return
    if action in sales_actions and not can_view_charter_sales(update):
        await query.answer("Нет доступа к данным Leon Sales.", show_alert=True)
        return
    value = int(selection)
    state["revision"] += 1
    revision = state["revision"]
    next_state = state.copy()
    await query.answer()
    try:
        if action in fleet_actions:
            text, markup = await fleet_view(next_state, action, value)
        elif action in sales_actions:
            text, markup = await sales_view(
                next_state, action, value, update.effective_user.id
            )
        elif action in journey_actions:
            text, markup = await journey_view(
                next_state, action, value, route, update.effective_user.id
            )
        else:
            text, markup = charter_menu(update, state["token"])
    except (
        HTTPError,
        TimeoutError,
        ValueError,
        OSError,
        RuntimeError,
        TypeError,
        sqlite3.Error,
    ):
        text = "CHARTER CHECK · Не удалось загрузить данные раздела. Попробуйте позже."
        markup = InlineKeyboardMarkup(
            [[charter_button(state["token"], "← Разделы", "menu")]]
        )
    if (
        context.user_data.get("charter_check") is state
        and state["revision"] == revision
    ):
        next_state["expires"] = monotonic() + CACHE_SECONDS
        context.user_data["charter_check"] = next_state
        await edit_message_text(query, text, reply_markup=markup)
