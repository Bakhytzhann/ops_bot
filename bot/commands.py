"""Telegram catalog; operational execution belongs to application/."""

from dataclasses import dataclass

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup

from application.commands import execute_operation


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

COMMANDS.update(
    {
        "prepare": Command(
            "Flight Preparation / Flight Card",
            "Укажите рейс и дату: ABC1265 на 2026-11-11.",
        ),
        "duty": Command("DUTY / REST", "Укажите рейс и дату: ABC1265 на 2026-11-11."),
        "notams": Command(
            "NOTAM по рейсу", "Укажите рейс и дату: ABC1265 на 2026-11-11."
        ),
        "report": Command("Bot Report", "Укажите рейс и дату: ABC1265 на 2026-11-11."),
    }
)

MENU_GROUPS = {
    "preparation": (
        "Предполётная подготовка",
        (
            "search",
            "prepare",
            "crew",
            "roster",
            "duty",
            "ftl",
            "violations",
            "weather",
            "notams",
            "route",
            "briefing",
            "flighttime",
            "overflight",
        ),
    ),
    "reports": (
        "Аналитика / Отчёты",
        ("report", "flights"),
    ),
}


def help_text():
    return "\n".join(
        [
            "OPS Bot · предполётные операции",
            "/sked — SKED → Flight Card; Next 7d / Next 30d — следующий период.",
            "/menu — основные разделы. Выберите действие и ответьте на запрос параметров.",
            *[f"/{name} — {command.title}" for name, command in COMMANDS.items()],
            "/cancel — отменить ввод; /help — справка.",
            "Даты и времена — UTC. Доступ только в личных чатах после одобрения.",
            "Администраторы: /access — заявки и отзыв доступа.",
        ]
    )


def menu_keyboard(group=None):
    if group is None:
        rows = [[InlineKeyboardButton("SKED", callback_data="op:week")]]
        rows.append([InlineKeyboardButton("FLEET DOCS", callback_data="docs:home")])
        rows += [
            [InlineKeyboardButton(title, callback_data=f"op:group_{key}")]
            for key, (title, _) in MENU_GROUPS.items()
        ]
    else:
        buttons = [
            InlineKeyboardButton(COMMANDS[name].title, callback_data=f"op:{name}")
            for name in MENU_GROUPS[group][1]
        ]
        rows = [[InlineKeyboardButton("SKED", callback_data="op:week")]]
        rows += [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
        rows.append([InlineKeyboardButton("⌂ Меню", callback_data="op:menu")])
    rows.append([InlineKeyboardButton("Справка", callback_data="op:help")])
    return InlineKeyboardMarkup(rows)


def telegram_commands():
    return [
        BotCommand("start", "Начать / запросить доступ"),
        BotCommand("menu", "Основные разделы"),
        BotCommand("sked", "SKED — выбрать рейс"),
        BotCommand("help", "Справка"),
        BotCommand("cancel", "Отменить ввод"),
    ]


async def execute_command(name, arguments):
    if name not in COMMANDS:
        raise ValueError("Неизвестная команда. Откройте /menu.")
    return (await execute_operation(name, arguments)).text
