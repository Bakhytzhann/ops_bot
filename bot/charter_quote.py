"""Demonstration E35L charter quotation. Never an operational/commercial offer.

Reference hourly market listings: business-jets.kz/price.html and
arenda-samoleta.kz/price-list.html (retrieved November 2026).
All airport, handling, navigation, permit and catering charges are illustrative
scenario assumptions, NOT verified airport tariffs or flight-plan prices.
"""

import math

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

BASE_AIRPORT = (43.3521, 77.0405)
CRUISE_KMH = 780
DEMO_ROUTES = (
    ("Астана", "UACC", 51.02, 71.47, "domestic"),
    ("Шымкент", "UAII", 42.36, 69.48, "domestic"),
    ("Актау", "UATE", 43.86, 51.09, "domestic"),
    ("Атырау", "UATG", 47.12, 51.82, "domestic"),
    ("Актобе", "UATT", 50.25, 57.21, "domestic"),
    ("Усть-Каменогорск", "UASK", 50.04, 82.49, "domestic"),
    ("Ташкент", "UTTT", 41.26, 69.28, "regional"),
    ("Бишкек", "UCFM", 43.06, 74.48, "regional"),
    ("Самарканд", "UTSS", 39.70, 66.98, "regional"),
    ("Душанбе", "UTDD", 38.54, 68.83, "regional"),
    ("Баку", "UBBB", 40.47, 50.05, "international"),
    ("Тбилиси", "UGTB", 41.67, 44.95, "international"),
    ("Дубай", "OMDB", 25.25, 55.36, "international"),
    ("Абу-Даби", "OMAA", 24.43, 54.65, "international"),
    ("Доха", "OTHH", 25.27, 51.61, "international"),
    ("Стамбул", "LTFM", 41.28, 28.75, "international"),
    ("Анталья", "LTAI", 36.90, 30.80, "international"),
    ("Москва (Внуково)", "UUWW", 55.59, 37.26, "international"),
    ("Дели", "VIDP", 28.56, 77.10, "international"),
    ("Урумчи", "ZWWW", 43.91, 87.47, "regional"),
)
# Two-airport ground/handling allowances per sector, both ends together.
FEE_ALLOWANCE = {
    "domestic": 1400,
    "regional": 2100,
    "international": 3300,
}


def haversine_km(a, b):
    lat1, lon1 = map(math.radians, a)
    lat2, lon2 = map(math.radians, b)
    dlat, dlon = lat2 - lat1, lon2 - lon1
    x = math.sin(dlat / 2) ** 2 + (
        math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    )
    return 6371.0 * 2 * math.atan2(math.sqrt(x), math.sqrt(1 - x))


def estimate(index, *, rate=6500, pax=8, reserve=10, return_flight=False):
    """Transparent scenario amounts in USD; not a quotation from suppliers."""
    if not 0 <= index < len(DEMO_ROUTES):
        raise ValueError("Неизвестный маршрут.")
    if rate not in {5500, 6500, 7500}:
        raise ValueError("Недоступная ставка.")
    if pax not in {4, 8, 12} or reserve not in {0, 10, 20}:
        raise ValueError("Недоступный параметр.")
    city, icao, lat, lon, group = DEMO_ROUTES[index]
    direct_km = haversine_km(BASE_AIRPORT, (lat, lon))
    assumed_km = direct_km * 1.08
    sector_hours = max(1.0, math.ceil((assumed_km / CRUISE_KMH + 0.4) * 10) / 10)
    sectors = 2 if return_flight else 1
    flight = sector_hours * sectors * rate
    airport = FEE_ALLOWANCE[group] * sectors
    navigation = round(assumed_km * 0.35 * sectors)
    permits = (0 if group == "domestic" else 500) * sectors
    catering = 85 * pax * sectors
    handling_overnight = 0  # Explicitly excluded until itinerary/parking is known.
    subtotal = flight + airport + navigation + permits + catering
    reserve_amount = round(subtotal * reserve / 100)
    total = math.ceil((subtotal + reserve_amount) / 100) * 100
    return {
        "city": city,
        "icao": icao,
        "direct_km": round(direct_km),
        "route_km": round(assumed_km),
        "hours": sector_hours,
        "sectors": sectors,
        "flight": flight,
        "airport": airport,
        "navigation": navigation,
        "permits": permits,
        "catering": catering,
        "overnight": handling_overnight,
        "reserve_amount": reserve_amount,
        "subtotal": round(subtotal),
        "total": total,
        "range_warning": assumed_km > 5000,
    }


def params(context):
    return context.user_data.setdefault(
        "charter_quote",
        {"index": 0, "rate": 6500, "pax": 8, "reserve": 10, "return_flight": False},
    )


def menu():
    lines = [
        "CHARTER CHECK · E35L DEMO",
        "Выберите маршрут из Алматы (UAAA).",
        "20 примерных направлений для тестового калькулятора.",
        "Расчёт не проверяет пригодность ВС, экипажа, разрешения и MEL.",
    ]
    rows = []
    for index in range(0, len(DEMO_ROUTES), 2):
        rows.append(
            [
                InlineKeyboardButton(
                    f"{DEMO_ROUTES[j][0]} · {DEMO_ROUTES[j][1]}",
                    callback_data=f"quote:route:{j}",
                )
                for j in range(index, min(index + 2, len(DEMO_ROUTES)))
            ]
        )
    rows.append([InlineKeyboardButton("← К флоту", callback_data="quote:fleet:0")])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def quote_view(state):
    data = estimate(**state)
    def money(number):
        return f"${number:,.0f}"

    text = [
        "CHARTER CHECK · E35L · ДЕМО-РАСЧЁТ",
        f"UAAA → {data['icao']} ({data['city']})",
        f"Вариант: {'туда-обратно' if state['return_flight'] else 'в одну сторону'} · "
        f"{data['sectors']} сектор(а)",
        f"Расстояние по ортодромии ~{data['direct_km']} км",
        f"Условный маршрут (+8%) ~{data['route_km']} км",
        f"Время: ~{data['hours']:.1f} ч/сектор · без реального OFP",
        "",
        f"Лётные часы: {money(data['flight'])}",
        f"Аэропорт + handling (оценка): {money(data['airport'])}",
        f"Навигация (оценка): {money(data['navigation'])}",
        f"Разрешения / орг. сборы (оценка): {money(data['permits'])}",
        f"Catering (оценка): {money(data['catering'])}",
        f"Резерв {state['reserve']}%: {money(data['reserve_amount'])}",
        f"ИТОГО, ориентир: {money(data['total'])} USD",
        "",
        f"Параметры: {money(state['rate'])}/ч · {state['pax']} пассажиров",
        "Не включены: перегон, стоянка/ночёвка, НДС и иные налоги,",
        "дополнительное топливо, особый handling, визы и экипажные расходы.",
        "Часы и суммы условные. Это НЕ коммерческое предложение.",
        "Тарифы аэропортов, ATC и handling не подтверждены.",
    ]
    if data["range_warning"]:
        text.extend(["ПРЕДУПРЕЖДЕНИЕ: нужна отдельная проверка дальности и посадок."])
    rows = [
        [
            InlineKeyboardButton(
                "● One-way" if not state["return_flight"] else "One-way",
                callback_data="quote:mode:0",
            ),
            InlineKeyboardButton(
                "● Roundtrip" if state["return_flight"] else "Roundtrip",
                callback_data="quote:mode:1",
            ),
        ],
        [
            InlineKeyboardButton(
                f"{'● ' if state['rate'] == rate else ''}${rate}/ч",
                callback_data=f"quote:rate:{rate}",
            )
            for rate in (5500, 6500, 7500)
        ],
        [
            InlineKeyboardButton(
                f"{'● ' if state['pax'] == pax else ''}{pax} PAX",
                callback_data=f"quote:pax:{pax}",
            )
            for pax in (4, 8, 12)
        ],
        [
            InlineKeyboardButton(
                f"{'● ' if state['reserve'] == pct else ''}+{pct}%",
                callback_data=f"quote:reserve:{pct}",
            )
            for pct in (0, 10, 20)
        ],
        [
            InlineKeyboardButton("← 20 маршрутов", callback_data="quote:menu:0"),
            InlineKeyboardButton("← Флот", callback_data="quote:fleet:0"),
        ],
    ]
    return "\n".join(text), InlineKeyboardMarkup(rows)


async def quote_callback(update, context):
    query = update.callback_query
    parts = (query.data or "").split(":")
    if len(parts) != 3 or parts[0] != "quote":
        await query.answer("Недоступный расчёт.", show_alert=True)
        return
    action, value = parts[1:]
    if action == "fleet":
        await query.answer()
        from bot.charter_check import show_charter

        await show_charter(update, context)
        return
    if action == "menu":
        await query.answer()
        text, keyboard = menu()
        await query.edit_message_text(text, reply_markup=keyboard)
        return
    state = params(context)
    try:
        option = int(value)
        if action == "route" and 0 <= option < len(DEMO_ROUTES):
            state["index"] = option
        elif action == "rate" and option in {5500, 6500, 7500}:
            state["rate"] = option
        elif action == "pax" and option in {4, 8, 12}:
            state["pax"] = option
        elif action == "reserve" and option in {0, 10, 20}:
            state["reserve"] = option
        elif action == "mode" and option in {0, 1}:
            state["return_flight"] = bool(option)
        else:
            raise ValueError("Параметр недоступен.")
    except ValueError:
        await query.answer("Параметр недоступен.", show_alert=True)
        return
    await query.answer()
    text, keyboard = quote_view(state)
    await query.edit_message_text(text, reply_markup=keyboard)
