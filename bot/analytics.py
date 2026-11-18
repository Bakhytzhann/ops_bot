"""Telegram analytics periods, read-only source summaries and timing tabs."""

import asyncio
from secrets import token_hex
from time import monotonic

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from bot.admission import require_user
from services.analytics_report import PERIODS, VIEWS, load_analytics, render_analytics

CACHE_SECONDS = 900
PERIOD_ROWS = (("7d", "1m"), ("3m", "6m", "12m"))
VIEW_ROWS = (("management", "commercial"), ("finance", "timings"))


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
    }
    await update.effective_message.reply_text(
        "ANALYTICS · выберите период\n"
        "Сводки: менеджмент, коммерция, финансы, тайминги.\n"
        "Источник: Leon · плановое расписание, не фактические полётные данные.",
        reply_markup=_keyboard(token),
    )


async def analytics_callback(update, context):
    require_user(update)
    query = update.callback_query
    parts = (query.data or "").split(":")
    state = _state(context)
    if (
        len(parts) != 4
        or parts[0] != "ana"
        or parts[2] not in {"period", "view", "refresh"}
        or not state
        or parts[1] != state["token"]
        or (parts[2] in {"period", "refresh"} and parts[3] not in PERIODS)
        or (parts[2] == "view" and parts[3] not in VIEWS)
    ):
        await query.answer("Раздел устарел. Откройте ANALYTICS снова.", show_alert=True)
        return

    action, value = parts[2:]
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
        await query.edit_message_text(
            render_analytics(cached["data"], value),
            reply_markup=_keyboard(state["token"], period=period, view=value),
        )
        return

    period = value
    state["period"] = period
    state["view"] = "management" if action == "period" else state["view"]
    await query.answer()
    cached = state["cache"].get(period)
    if action == "refresh" or not cached or cached["expires"] < monotonic():
        await query.edit_message_text(
            f"ANALYTICS · {PERIODS[period]}\nЗагрузка Leon по интервалам до 31 дня…",
        )
        try:
            data = await asyncio.wait_for(load_analytics(period), timeout=360)
        except (TimeoutError, ValueError, RuntimeError, OSError, TypeError):
            await query.edit_message_text(
                "ANALYTICS · Данные Leon недоступны или не получены. "
                "Повторите выбор периода.",
                reply_markup=_keyboard(state["token"], period=period),
            )
            return
        state["cache"][period] = {
            "data": data,
            "expires": monotonic() + CACHE_SECONDS,
        }
    else:
        data = cached["data"]

    # A newer period selection must not be overwritten by the previous request.
    if _state(context) is not state or state["period"] != period:
        return
    await query.edit_message_text(
        render_analytics(data, state["view"]),
        reply_markup=_keyboard(state["token"], period=period, view=state["view"]),
    )
