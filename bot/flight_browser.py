"""Shared SKED sessions and Flight Cards scoped to the originating chat/topic."""

import re
from datetime import UTC, datetime, timedelta
from secrets import token_hex
from time import monotonic

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, MessageEntity

from application.operations import flight_schedule as get_flight_schedule
from application.operations import prepare_flight as load_flight_snapshot
from bot.admission import require_user
from bot.message_edits import edit_message_text
from bot.notam_browser import render_notams, valid_action
from services.flight_display import airport, flight_date, flight_reference, flight_time
from services.schedule_dates import parse_schedule_period
from services.snapshot import render_snapshot
from services.snapshot_models import SourceBlock, utc

PAGE_SIZE = 12
CACHE_TTL = 15 * 60
MAX_LISTS = 64


def _scope(update):
    return (
        update.effective_chat.id,
        getattr(update.effective_message, "message_thread_id", None),
    )


def invalidate_screen(update, context):
    for listing in (
        getattr(context, "bot_data", {}).get("weekly_flight_lists", {}).values()
    ):
        if listing.get("scope") == _scope(update):
            listing["screen_revision"] = listing.get("screen_revision", 0) + 1


def _cache(context):
    cache = context.bot_data.setdefault("weekly_flight_lists", {})
    for key in list(cache):
        if cache[key]["expires"] <= monotonic():
            del cache[key]
    return cache


def flight_label(flight, preferences=None):
    return (
        f"{flight.get('flightNumber') or 'нет данных'} {flight_date(flight, preferences)} "
        f"{airport(flight, 'departure', preferences)} "
        f"{flight_time(flight, 'departure', preferences)} "
        f"{flight_time(flight, 'arrival', preferences)} "
        f"{airport(flight, 'arrival', preferences)}"
        + (" · отменён" if flight.get("cancelled") else "")
    )


def preference_buttons(token, preferences, *, position=None):
    callback = (
        f"sv:{token}:view" if position is None else f"fc:{token}:{position}:view:0"
    )
    local = preferences.get("time") == "LT" and preferences.get("codes") == "IATA"
    return [
        InlineKeyboardButton(
            "UTC · ICAO" if local else "LT · IATA", callback_data=callback
        )
    ]


def schedule_heading(start, end):
    first = datetime.fromisoformat(start).date()
    last = datetime.fromisoformat(end).date()
    months = (
        "JAN",
        "FEB",
        "MAR",
        "APR",
        "MAY",
        "JUN",
        "JUL",
        "AUG",
        "SEP",
        "OCT",
        "NOV",
        "DEC",
    )
    prefix = f"{first.year % 100:02}{months[first.month - 1]}"
    if first == last:
        period = f"{prefix} {first.day:02}"
    elif (first.year, first.month) == (last.year, last.month):
        period = f"{prefix} {first.day:02}–{last.day:02}"
    else:
        period = f"{prefix} {first.day:02}–{last.year % 100:02}{months[last.month - 1]} {last.day:02}"
    return f"{period} (UTC)"


def render_page(token, listing, page=0):
    flights = listing["flights"]
    preferences = listing.get("preferences", {})
    pages = max(1, (len(flights) + PAGE_SIZE - 1) // PAGE_SIZE)
    if not 0 <= page < pages:
        raise ValueError("Страница списка не найдена. Откройте SKED.")
    listing["page"] = page
    start = page * PAGE_SIZE
    rows = []
    for i, f in enumerate(flights[start : start + PAGE_SIZE], start):
        rows.append(
            [
                InlineKeyboardButton(
                    flight_label(f, preferences), callback_data=f"fl:{token}:{i}"
                )
            ]
        )
    navigation = []
    if page:
        navigation.append(
            InlineKeyboardButton("←", callback_data=f"wk:{token}:{page - 1}")
        )
    if page + 1 < pages:
        navigation.append(
            InlineKeyboardButton("→", callback_data=f"wk:{token}:{page + 1}")
        )
    if navigation:
        rows.append(navigation)
    rows.append(
        [
            InlineKeyboardButton(
                "UTC · ICAO" if preferences.get("time") == "LT" else "LT · IATA",
                callback_data=f"sv:{token}:view",
            )
        ]
    )
    rows.append(
        [
            InlineKeyboardButton("7 дней", callback_data=f"sk:{token}:7"),
            InlineKeyboardButton("30 дней", callback_data=f"sk:{token}:30"),
        ]
    )
    rows.append(
        [
            InlineKeyboardButton("↻ Обновить", callback_data=f"sk:{token}:refresh"),
        ]
    )
    state = (
        "Выберите рейс"
        if flights
        else "нет рейсов"
        if listing["complete"]
        else "нет данных"
    )
    text = (
        f"{'Поиск: ' if listing.get('search') else ''}{schedule_heading(listing['start'], listing['end'])}\n"
        f"{state} · Страница {page + 1}/{pages}"
    )
    if not listing["complete"]:
        text += "\nРасписание неполное: нет данных по части рейсов."
    text += "\nРежим: " + ("LT / IATA" if preferences.get("time") == "LT" else "UTC / ICAO")
    return text, InlineKeyboardMarkup(rows)


async def show_week(
    update, context, *, today=None, search=None, days=3, preferences=None, edit=False
):
    require_user(update)
    today = today or datetime.now(UTC).date()
    end = today + timedelta(days=days - 1)
    term = None
    if search:
        import re

        term, _, dates = search.strip().partition(" ")
        term = term.upper()
        if not re.fullmatch(r"[A-Z0-9-]{3,15}", term):
            raise ValueError(
                "Укажите номер, регистрацию или ICAO одним словом, затем дату или период."
            )
        period = parse_schedule_period(dates) if dates.strip() else None
        if dates.strip() and period is None:
            raise ValueError("Не распознана дата поиска.")
        if period:
            today, end = period.start, period.end
    try:
        result = await get_flight_schedule(today.isoformat(), end.isoformat())
    except PermissionError:
        raise
    except Exception:  # noqa: BLE001 -- failed source is distinct from an empty schedule
        result = {"flights": [], "complete": False}
    valid = [
        f
        for f in result["flights"]
        if type(f.get("flightNid")) is int and f["flightNid"] > 0
    ]
    flights = sorted(
        valid,
        key=lambda f: (
            utc(f.get("departureUTC")) or datetime.max.replace(tzinfo=UTC),
            str(f.get("flightNumber") or ""),
        ),
    )
    complete = result["complete"] and len(valid) == len(result["flights"])
    token = token_hex(6)
    if term:

        def normalized(value):
            return str(value or "").replace("-", "").upper()

        flights = [
            f
            for f in flights
            if any(
                normalized(f.get(key)) == normalized(term)
                for key in (
                    "flightNumber",
                    "aircraft",
                    "departure",
                    "arrival",
                    "departureIATA",
                    "arrivalIATA",
                )
            )
        ]
    cache = _cache(context)
    while len(cache) >= MAX_LISTS:
        cache.pop(next(iter(cache)))
    listing = {
        "scope": _scope(update),
        "expires": monotonic() + CACHE_TTL,
        "start": today.isoformat(),
        "end": end.isoformat(),
        "complete": complete,
        "flights": flights,
        "search": term,
        "preferences": dict(preferences or {}),
    }
    cache[token] = listing
    text, markup = render_page(token, listing)
    if edit:
        await edit_message_text(update.callback_query, text, reply_markup=markup)
    else:
        await update.effective_message.reply_text(text, reply_markup=markup)


def is_card_notice_callback(data):
    parts = (data or "").split(":")
    return (
        len(parts) == 5
        and parts[0] == "fc"
        and parts[3] == "briefing"
    )


async def handle_selection(update, context):
    require_user(update)
    query = update.callback_query
    parts = (query.data or "").split(":")
    if (
        len(parts) not in (3, 5)
        or parts[0] not in {"wk", "fl", "fc", "sv", "sk"}
        or (parts[0] not in {"sv", "sk"} and not parts[2].isdigit())
    ):
        raise ValueError("Некорректная кнопка рейса. Обновите расписание.")
    kind, token, position = parts[:3]
    if (kind == "fc") != (len(parts) == 5):
        raise ValueError("Некорректная кнопка карточки.")
    listing = _cache(context).get(token)
    if not listing or listing["scope"] != _scope(update):
        raise ValueError(
            "Список устарел или открыт в другой теме. Откройте SKED снова."
        )
    if is_card_notice_callback(query.data):
        index = int(position)
        snapshot = listing.get("snapshots", {}).get(index)
        navigation = (
            (getattr(context, "user_data", None) or {})
            .get("flight_navigation", {})
            .get(_scope(update))
        )
        if (
            parts[4] != "0"
            or index >= len(listing["flights"])
            or snapshot is None
            or snapshot.flight.get("flightNid")
            != listing["flights"][index].get("flightNid")
            or (navigation and navigation.get("return_to") != (token, index))
        ):
            raise ValueError("Карточка устарела. Откройте рейс из SKED.")
        await query.answer(
            "Брифинг ForeFlight недоступен. Нажмите REFRESH.", show_alert=True
        )
        return
    preferences = listing.setdefault("preferences", {})
    if kind in {"sv", "sk"}:
        listing["screen_revision"] = listing.get("screen_revision", 0) + 1
        if kind == "sv":
            if position not in {"view", "time", "codes"}:
                raise ValueError("Неизвестный переключатель.")
            toggle_preference(preferences, position)
            text, markup = render_page(token, listing, listing.get("page", 0))
            await edit_message_text(query, text, reply_markup=markup)
        else:
            if position not in {"7", "30", "refresh"}:
                raise ValueError("Неизвестный период SKED.")
            start = datetime.fromisoformat(listing["start"]).date()
            end = datetime.fromisoformat(listing["end"]).date()
            days = (end - start).days + 1 if position == "refresh" else int(position)
            if position != "refresh":
                start = end + timedelta(days=1)
            search = listing.get("search")
            await show_week(
                update,
                context,
                today=start,
                days=days,
                preferences=preferences,
                edit=True,
                search=search,
            )
        return
    position = int(position)
    revision = listing["screen_revision"] = listing.get("screen_revision", 0) + 1
    if kind == "wk":
        text, markup = render_page(token, listing, position)
        if (
            getattr(update.effective_message, "text", None) != text
            or getattr(update.effective_message, "reply_markup", None) != markup
        ):
            await edit_message_text(query, text, reply_markup=markup)
    else:
        if position >= len(listing["flights"]):
            raise ValueError("Рейс не найден в списке. Обновите расписание.")
        action, page = (parts[3], parts[4]) if kind == "fc" else ("card", "0")
        if not page.isdigit() or (
            not valid_action(action)
            and action
            not in {
                "card",
                "refresh",
                "crew",
                "weather",
                "duty",
                "notams",
                "dep",
                "arr",
                "route",
                "eet",
                "view",
                "time",
                "codes",
            }
        ):
            raise ValueError("Неизвестное действие карточки.")
        import asyncio

        if action in {"view", "time", "codes"}:
            toggle_preference(preferences, action)
            action = listing.get("action", "card")
        snapshots = listing.setdefault("snapshots", {})
        snapshot = snapshots.get(position)
        if kind == "fl" or snapshot is None or action == "refresh":
            progress_lock = asyncio.Lock()
            last_progress = [""]

            async def progress(partial):
                require_user(update)
                async with progress_lock:
                    text = render_snapshot(partial, preferences)
                    if (
                        listing["screen_revision"] == revision
                        and text != last_progress[0]
                    ):
                        await edit_message_text(
                            query,
                            text[:3800],
                            disable_web_page_preview=True,
                            entities=card_entities(
                                text[:3800], partial, preferences
                            ),
                        )
                        last_progress[0] = text

            snapshot = await load_flight_snapshot(
                listing["flights"][position],
                refresh=action == "refresh",
                on_update=None if getattr(update, "main_screen", None) else progress,
            )
            if position in snapshots:
                listing.setdefault("previous", {})[position] = snapshots[position]
            snapshots[position] = snapshot
        require_user(update)
        context.user_data.setdefault("flight_navigation", {})[_scope(update)] = {
            "selected_flight_id": snapshot.flight["flightNid"],
            "return_to": (token, position),
            "selected_station": snapshot.flight.get(
                "departure" if action == "dep" else "arrival"
            )
            if action in {"dep", "arr"}
            else None,
        }
        if listing["screen_revision"] != revision:
            return
        if (
            action == "weather"
            and "weather_taf" not in snapshot.blocks
            and not snapshot.blocks.get("weather", SourceBlock("WX")).details.get(
                "include_taf"
            )
        ):
            from application.operations import (
                selected_weather as selected_flight_weather,
            )

            try:
                                result = await asyncio.wait_for(
                    selected_flight_weather(
                        snapshot.flight,
                        snapshot.flight["departureUTC"][:10],
                        include_taf=True,
                    ),
                    timeout=30,
                )
                snapshot.blocks["weather_taf"] = SourceBlock(
                    "Leon / AWC",
                    result["answer"],
                    "partial" if result.get("partial") else "ready",
                )
            except (TimeoutError, OSError):
                snapshot.blocks["weather_taf"] = SourceBlock(
                    "WX", "TAF: источник недоступен; METAR на основном экране."
                )
            if listing["screen_revision"] != revision:
                return
        if action in {"notams", "dep", "arr"}:
            action = "nlA_A" if action == "arr" else "nlD_A"
        listing["action"] = action
        page = int(page)
        if valid_action(action):
            text, markup = render_notams(
                snapshot, token, position, action, page, preferences
            )
        else:
            text = card_detail(snapshot, action, preferences)
            pages = [text[i : i + 3800] for i in range(0, len(text), 3800)] or [
                "нет данных"
            ]
            if page >= len(pages):
                raise ValueError("Страница карточки не найдена.")
            text = pages[page]
            markup = card_keyboard(
                snapshot, token, position, action, page, len(pages), preferences
            )
        # Telegram rejects identical edits; avoid unnecessary errors on repeated taps.
        if (
            getattr(update.effective_message, "text", None) != text
            or getattr(update.effective_message, "reply_markup", None) != markup
        ):
            await edit_message_text(
                query,
                text,
                reply_markup=markup,
                disable_web_page_preview=True,
                **(
                    {"entities": card_entities(text, snapshot, preferences)}
                    if action in {"card", "refresh"}
                    else {}
                ),
            )


def card_entities(text, snapshot, preferences=None):
    from services.flight_weather import station_label, station_roles

    stations = station_roles(snapshot)
    tokens = {
        snapshot.flight.get("flightNumber"),
        flight_date(snapshot.flight, preferences),
    }
    tokens.update(stations)
    tokens.update(
        station_label(snapshot, station, preferences).split()[0] for station in stations
    )
    tokens.discard(None)
    tokens.discard("нет данных")
    choices = "|".join(
        [re.escape(token) for token in sorted(tokens, key=len, reverse=True)]
        + [r"\d{4}z", r"\d{6}Z"]
    )
    pattern = rf"(?<!\w)(?:{choices})(?!\w)"
    return [
        MessageEntity(
            type="bold",
            offset=len(text[: match.start()].encode("utf-16-le")) // 2,
            length=len(match.group().encode("utf-16-le")) // 2,
        )
        for match in re.finditer(pattern, text)
    ]


def card_keyboard(
    snapshot, token, position, action="card", page=0, pages=1, preferences=None
):
    def button(label, target, number=0):
        return InlineKeyboardButton(
            label, callback_data=f"fc:{token}:{position}:{target}:{number}"
        )

    rows = [
        [button("DUTY / REST", "duty"), button("WX / TAF", "weather")],
        [button("RTE / ALTN", "route"), button("NOTAM", "notams")],
        preference_buttons(token, preferences or {}, position=position)
        + [
            InlineKeyboardButton(
                "Briefing", url=snapshot.blocks["briefing"].details["url"]
            )
            if snapshot.blocks.get("briefing")
            and snapshot.blocks["briefing"].details.get("url")
            else button("Briefing", "briefing")
        ],
    ]
    if pages > 1:
        rows.append(
            ([button("←", action, page - 1)] if page else [])
            + ([button("→", action, page + 1)] if page + 1 < pages else [])
        )
    if action not in {"card", "refresh"}:
        rows.append([button("← К рейсу", "card")])
    rows.append(
        [
            button("REFRESH", "refresh"),
            InlineKeyboardButton(
                "← SKED", callback_data=f"wk:{token}:{position // PAGE_SIZE}"
            ),
        ]
    )
    return InlineKeyboardMarkup(rows)


def card_detail(snapshot, action, preferences=None):
    if action in {"card", "refresh"}:
        return render_snapshot(snapshot, preferences)
    if action == "weather":
        from services.flight_weather import full_weather

        content = full_weather(snapshot, preferences)
    elif action == "route":
        from services.flight_weather import station_label

        block = snapshot.blocks.get("route")
        details = block.details if block else {}
        lines = ["RTE / ALTN", details.get("route") or "нет данных"]
        for key, label in (
            ("alternate", "ALTN"),
            ("secondAlternate", "ALTN 2"),
            ("takeOffAlternate", "TO ALTN"),
        ):
            if details.get(key):
                lines.append(
                    label + " " + station_label(snapshot, details[key], preferences)
                )
        content = "\n".join(lines)
    else:
        block = snapshot.blocks.get("flighttime" if action == "eet" else action)
        content = block.text if block else "нет данных"
    return flight_reference(snapshot.flight, preferences) + "\n\n" + content


def toggle_preference(preferences, key):
    if key not in {"view", "time", "codes"}:
        raise ValueError("Неизвестный переключатель.")
    local = preferences.get("time") == "LT" and preferences.get("codes") == "IATA"
    preferences.update(time="Z" if local else "LT", codes="ICAO" if local else "IATA")
