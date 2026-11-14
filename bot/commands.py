"""Single catalog for Telegram menus, help and model-free command execution."""

from dataclasses import dataclass

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup

from config.access import require_access
from integrations.leon.operations import resolve_flight
from integrations.leon.tools import get_flight_crew, get_flight_schedule
from services.foreflight_operations import foreflight_operation
from services.operational_routes import FLIGHT_PATTERN, route_operation
from services.schedule_dates import parse_schedule_period


@dataclass(frozen=True)
class Command:
    title: str
    prompt: str
    prefix: str = ""


COMMANDS = {
    "search": Command(
        "Поиск рейса / борта / ICAO",
        "Укажите номер рейса, регистрацию или ICAO и при необходимости дату: ABC1265; UAAA завтра; UP-ABC на 2026-11-12.",
    ),
    "flights": Command(
        "Расписание Leon", "Укажите дату или период: сегодня; с 16 по 30 ноября 2026."
    ),
    "watch": Command(
        "Flight Watch", "Укажите рейс и дату: ABC1265 на 2026-11-11.", "Flight Watch"
    ),
    "weather": Command(
        "Погода / METAR / TAF",
        "Укажите рейс и дату или ICAO: ABC1265 на 2026-11-11; EGLL. Для прогноза добавьте TAF.",
        "погода",
    ),
    "crew": Command("Экипаж рейса", "Укажите рейс и дату: ABC1265 на 2026-11-11."),
    "roster": Command(
        "График экипажа",
        "Укажите имя в кавычках или код и период: «Иванов» на весь ноябрь 2026.",
        "график пилота",
    ),
    "ftl": Command(
        "FTL",
        "Укажите имя в кавычках или код и период: код JKO с 1 по 15 ноября 2026.",
        "FTL",
    ),
    "violations": Command(
        "Нарушения FTL",
        "Укажите сотрудника и период: код JKO на весь ноябрь 2026.",
        "нарушения FTL",
    ),
    "flighttime": Command(
        "Полётное время", "Укажите позывной ForeFlight и дату: ABC1265 на 2026-11-11."
    ),
    "route": Command(
        "Маршрут ForeFlight",
        "Укажите позывной ForeFlight и дату: ABC1265 на 2026-11-11.",
    ),
    "briefing": Command(
        "Брифинг ForeFlight",
        "Укажите позывной ForeFlight и дату: ABC1265 на 2026-11-11.",
    ),
    "overflight": Command(
        "Страны и FIR", "Укажите позывной ForeFlight и дату: ABC1265 на 2026-11-11."
    ),
    "compare": Command(
        "Сравнить с Leon", "Укажите номер рейса Leon и дату: ABC1265 на 2026-11-11."
    ),
}

AI_DESCRIPTION = (
    "С ИИ: /ai вопрос — объяснение терминов и предоставленных сводок, перевод, "
    "текстовые пояснения и черновики. ИИ может ошибаться; он не подтверждает "
    "актуальные данные и не принимает решения о безопасности полёта. "
    "Для рейсов и документов используйте команды выше."
)


def help_text():
    return "\n".join(
        [
            "OPS Bot — команды без модели",
            "/sked — SKED на 3 дня → Flight Card; Next 7d / Next 30d — следующий период. /week — совместимый вход.",
            "/menu — SKED. Дополнительные операции доступны командами с параметрами:",
            *[f"/{name} — {command.title}" for name, command in COMMANDS.items()],
            "",
            "Пример: /route ABC1265 на 2026-11-11",
            "Без параметров команда попросит ввод. Ответьте на сообщение с подсказкой.",
            "Даты и времена — UTC. График и FTL: до 31 дня.",
            "Команды доступны разрешённым пользователям в личном чате.",
            "/cancel — отменить ввод; /help — справка; /menu — кнопки.",
            "",
            AI_DESCRIPTION,
            "Пишите вопросы в личный чат: знакомые операции идут без модели, остальные — к ИИ.",
        ]
    )


MENU_GROUPS = {
    "flight": ("Поиск / Рейс", ("search", "flights", "crew", "weather", "flighttime")),
    "watch": ("Мониторинг", ("watch", "compare")),
    "duty": ("FTL / Смена", ("roster", "ftl", "violations")),
    "tools": ("Инструменты", ("route", "briefing", "overflight")),
}


def menu_keyboard(group=None):
    if group is None:
        return InlineKeyboardMarkup(
            [[InlineKeyboardButton("SKED", callback_data="op:week")]]
        )
    buttons = [
        InlineKeyboardButton(c.title, callback_data=f"op:{name}")
        for name, c in COMMANDS.items()
        if name in MENU_GROUPS[group][1]
    ]
    rows = [[InlineKeyboardButton("SKED", callback_data="op:week")]]
    rows.extend(buttons[i : i + 2] for i in range(0, len(buttons), 2))
    rows.append(
        [
            InlineKeyboardButton("Вопрос к ИИ", callback_data="op:ai"),
            InlineKeyboardButton("Что умеет бот / ИИ", callback_data="op:help"),
        ]
    )
    rows.append([InlineKeyboardButton("⌂ Меню", callback_data="op:menu")])
    return InlineKeyboardMarkup(rows)


def telegram_commands():
    return [
        BotCommand("sked", "SKED на 3 дня — выбрать рейс"),
        BotCommand("help", "Все команды и справка"),
        BotCommand("cancel", "Отменить ввод"),
    ]


def single_flight(arguments):
    match = FLIGHT_PATTERN.search(arguments)
    period = parse_schedule_period(arguments)
    if not match or not period or period.start != period.end:
        raise ValueError("Укажите один рейс и одну дату (UTC): ABC1265 на 2026-11-11.")
    return "".join(match.group().split()).replace(
        "-", ""
    ).upper(), period.start.isoformat()


async def execute_command(name, arguments):
    """Return text directly; this function never imports or calls the model."""
    require_access()
    if name not in COMMANDS:
        raise ValueError("Неизвестная команда. Откройте /menu.")
    if name == "flights":
        period = parse_schedule_period(arguments)
        if not period:
            raise ValueError(COMMANDS[name].prompt)
        # Imported at call time to share the existing schedule presentation.
        from services.schedule_format import format_schedule

        return format_schedule(
            await get_flight_schedule(period.start.isoformat(), period.end.isoformat())
        )
    if name in {"flighttime", "route", "briefing", "overflight", "compare"}:
        number, day = single_flight(arguments)
        return await foreflight_operation(name, number, day)
    if name == "crew":
        number, day = single_flight(arguments)
        flight = await resolve_flight(number, day)
        result = await get_flight_crew(flight["flightNid"])
        from services.crew_format import format_flight_crew

        return format_flight_crew(number, day, result["crew"])
    return await route_operation(f"{COMMANDS[name].prefix} {arguments}")


def natural_foreflight_command(question):
    """Recognize supported ForeFlight intents before generic schedule/model routes."""
    import re

    if not FLIGHT_PATTERN.search(question) and not re.search(
        r"рейс|foreflight|форефлайт", question, re.IGNORECASE
    ):
        return None
    for pattern, command in [
        (r"сравн|расхожд|свер[ьи]", "compare"),
        (r"брифинг|briefing", "briefing"),
        (
            r"прол[её]т.*(?:стран|fir|фир)|страны.*(?:fir|фир)|\bfir\b|\bфир\b",
            "overflight",
        ),
        (r"пол[её]тно[её]\s+время|время\s+пол[её]та|\bete\b", "flighttime"),
        (r"маршрут|\broute\b", "route"),
    ]:
        if re.search(pattern, question, re.IGNORECASE):
            return command
    return None
