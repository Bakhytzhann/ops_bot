import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from secrets import token_hex
from time import monotonic

from httpx import HTTPError
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from bot.admission import require_user
from services.analytics_ai import analyze_metrics
from services.analytics_insights import build_insights
from services.analytics_report import PERIODS, VIEWS, load_analytics, render_analytics
from services.journey_history import load_journey_insights

CACHE_SECONDS = 900
PERIOD_ROWS = (("7d", "1m"), ("3m", "6m", "12m"))
VIEW_ROWS = (("management", "commercial"), ("finance", "timings"))
INSIGHT_PERIODS = {
    "7d": ("7 дней", 7),
    "30d": ("30 дней", 30),
    "90d": ("90 дней", 90),
    "year": ("Год", 365),
}
DATA_ERRORS = (HTTPError, TimeoutError, ValueError, RuntimeError, OSError, TypeError)
MENU_TEXT = (
    "ANALYTICS · выберите период\n"
    "Источник: Leon · плановое расписание, не фактические полётные данные."
)


def _keyboard(token, *, period=None, view=None):
    rows = [
        [
            InlineKeyboardButton(
                f"{'● ' if key == period else ''}{PERIODS[key]}",
                callback_data=f"ana:{token}:period:{key}",
            )
            for key in group
        ]
        for group in PERIOD_ROWS
    ]
    rows.append(
        [
            InlineKeyboardButton(
                "ИИ · Метрики и рекомендации", callback_data=f"ana:{token}:insight:7d"
            )
        ]
    )
    if period:
        rows += [
            [
                InlineKeyboardButton(
                    f"{'● ' if name == view else ''}{VIEWS[name]}",
                    callback_data=f"ana:{token}:view:{name}",
                )
                for name in group
            ]
            for group in VIEW_ROWS
        ]
        rows.append(
            [
                InlineKeyboardButton(
                    "↻ Обновить данные",
                    callback_data=f"ana:{token}:refresh:{period}",
                )
            ]
        )
    return InlineKeyboardMarkup(rows)


def _insight_keyboard(token, period):
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    f"{'● ' if key == period else ''}{label}",
                    callback_data=f"ana:{token}:insight:{key}",
                )
                for key, (label, _) in INSIGHT_PERIODS.items()
            ],
            [
                InlineKeyboardButton(
                    "↻ Обновить Journey Log",
                    callback_data=f"ana:{token}:insight_refresh:{period}",
                )
            ],
            [
                InlineKeyboardButton(
                    "← К аналитике", callback_data=f"ana:{token}:insight_back:0"
                )
            ],
        ]
    )


def insight_dates(period, *, today=None):
    today = today or datetime.now(UTC).date()
    days = INSIGHT_PERIODS[period][1]
    return today - timedelta(days=days), today - timedelta(days=1)


async def _insight_report(period, user_id, *, refresh):
    start, end = insight_dates(period)
    stats = await load_journey_insights(start, end, refresh=refresh)
    report = build_insights(stats)
    return await analyze_metrics(report, ("analytics", user_id, period))


def _state(context):
    return context.user_data.get("analytics_state")


async def show_analytics(update, context):
    require_user(update)
    token = token_hex(5)
    context.user_data["analytics_state"] = {
        "token": token,
        "period": None,
        "view": "management",
        "cache": {},
        "revision": 0,
    }
    await update.effective_message.reply_text(MENU_TEXT, reply_markup=_keyboard(token))


async def analytics_callback(update, context):
    require_user(update)
    query = update.callback_query
    parts = (query.data or "").split(":")
    state = _state(context)
    if (
        len(parts) != 4
        or parts[0] != "ana"
        or parts[2]
        not in {
            "period",
            "view",
            "refresh",
            "insight",
            "insight_refresh",
            "insight_back",
        }
        or not state
        or parts[1] != state["token"]
        or (parts[2] in {"period", "refresh"} and parts[3] not in PERIODS)
        or (parts[2] == "view" and parts[3] not in VIEWS)
        or (
            parts[2] in {"insight", "insight_refresh"}
            and parts[3] not in INSIGHT_PERIODS
        )
        or (parts[2] == "insight_back" and parts[3] != "0")
    ):
        await query.answer("Раздел устарел. Откройте ANALYTICS снова.", show_alert=True)
        return

    action, value = parts[2:]
    state["revision"] += 1
    revision = state["revision"]

    def current():
        return _state(context) is state and state["revision"] == revision

    if action in {"insight", "insight_refresh"}:
        markup = _insight_keyboard(state["token"], value)
        await query.answer()
        if not current():
            return
        await query.edit_message_text(
            f"ANALYTICS · Метрики и рекомендации · {INSIGHT_PERIODS[value][0]}\n"
            "Проверяю покрытие Journey Log…",
            reply_markup=markup,
        )
        try:
            text = await _insight_report(
                value, update.effective_user.id, refresh=action == "insight_refresh"
            )
        except (*DATA_ERRORS, sqlite3.Error):
            text = (
                "ANALYTICS · Не удалось прочитать статистику Journey Log. "
                "Попробуйте обновить данные или выберите другой период."
            )
        if current():
            await query.edit_message_text(text, reply_markup=markup)
        return
    if action == "insight_back":
        await query.answer()
        period = state["period"]
        cached = state["cache"].get(period)
        if current():
            await query.edit_message_text(
                render_analytics(cached["data"], state["view"])
                if cached
                else MENU_TEXT,
                reply_markup=_keyboard(
                    state["token"],
                    period=period if cached else None,
                    view=state["view"] if cached else None,
                ),
            )
        return
    if action == "view":
        if not state["period"]:
            await query.answer("Сначала выберите период.", show_alert=True)
            return
        period = state["period"]
        cached = state["cache"].get(period)
        if not cached:
            await query.answer("Выберите период заново.", show_alert=True)
            return
        state["view"] = value
        await query.answer()
        if current():
            await query.edit_message_text(
                render_analytics(cached["data"], value),
                reply_markup=_keyboard(state["token"], period=period, view=value),
            )
        return

    period = value
    state["period"] = period
    state["view"] = "management" if action == "period" else state["view"]
    await query.answer()
    if not current():
        return
    cached = state["cache"].get(period)
    if action == "refresh" or not cached or cached["expires"] < monotonic():
        await query.edit_message_text(
            f"ANALYTICS · {PERIODS[period]}\nЗагрузка Leon по интервалам до 31 дня…",
        )
        try:
            data = await asyncio.wait_for(load_analytics(period), timeout=360)
        except DATA_ERRORS:
            if current():
                await query.edit_message_text(
                    "ANALYTICS · Данные Leon недоступны или не получены. "
                    "Повторите выбор периода.",
                    reply_markup=_keyboard(state["token"], period=period),
                )
            return
        if not current():
            return
        state["cache"][period] = {
            "data": data,
            "expires": monotonic() + CACHE_SECONDS,
        }
    else:
        data = cached["data"]

    if not current():
        return
    await query.edit_message_text(
        render_analytics(data, state["view"]),
        reply_markup=_keyboard(state["token"], period=period, view=state["view"]),
    )
