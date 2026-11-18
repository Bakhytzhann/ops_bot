"""Source-backed operational analytics over Leon schedule, not actual flight logs.

Every financial/OTP item without a verified source is intentionally unavailable.
Multi-month intervals use <=31-day Leon windows and disclose partial coverage.
"""

import asyncio
import calendar
from collections import Counter, defaultdict
from datetime import UTC, date, datetime, timedelta
from statistics import median

from integrations.leon.tools import get_flight_schedule
from services.snapshot_models import utc

PERIODS = {
    "7d": "7 дней",
    "1m": "1 месяц",
    "3m": "3 месяца",
    "6m": "6 месяцев",
    "12m": "12 месяцев",
}
VIEWS = {
    "management": "Менеджмент",
    "commercial": "Коммерция",
    "finance": "Финансы",
    "timings": "Тайминги",
}
TIGHT_TURN_MINUTES = 120  # Descriptive dashboard threshold, not a legal minimum.


def period_start(key, today):
    if key == "7d":
        return today - timedelta(days=6)
    if key not in PERIODS:
        raise ValueError("Неизвестный аналитический период.")
    months = int(key[:-1])
    month_index = today.year * 12 + today.month - 1 - months
    year, month_zero = divmod(month_index, 12)
    month = month_zero + 1
    day = min(today.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def month_windows(start, end):
    """Cover every calendar day once, respecting SKED's 31-day API limit."""
    if start > end:
        raise ValueError("Некорректный период.")
    cursor = start
    while cursor <= end:
        last = min(cursor + timedelta(days=30), end)
        yield cursor, last
        cursor = last + timedelta(days=1)


def minutes_label(value):
    if value is None:
        return "нет данных"
    value = round(value)
    hours, mins = divmod(value, 60)
    return f"{hours}:{mins:02d}"


def _sorted_counts(counter, count=5):
    return sorted(counter.items(), key=lambda item: (-item[1], str(item[0])))[:count]


def aggregate(flights, now):
    """Aggregate scheduled fields only, never infer actual departure/arrival."""
    seen = set()
    valid = []
    cancelled = 0
    excluded = 0
    unassigned = 0
    for flight in flights:
        if not isinstance(flight, dict):
            excluded += 1
            continue
        dep = utc(flight.get("departureUTC"))
        if dep is None or dep > now:
            excluded += 1
            continue
        nid = flight.get("flightNid")
        identity = (
            ("nid", nid)
            if type(nid) is int and nid > 0
            else (
                "fields",
                flight.get("flightNumber"),
                flight.get("departureUTC"),
                flight.get("aircraft"),
                flight.get("departure"),
                flight.get("arrival"),
            )
        )
        if identity in seen:
            continue
        seen.add(identity)
        if flight.get("cancelled") is True:
            cancelled += 1
            continue
        reg = (flight.get("aircraft") or "").strip()
        if not reg:
            unassigned += 1
        arr = utc(flight.get("arrivalUTC"))
        block_minutes = None
        if arr and 0 < (arr - dep).total_seconds() <= 48 * 3600:
            block_minutes = round((arr - dep).total_seconds() / 60)
        valid.append(
            {
                "reg": reg,
                "flight": flight,
                "departure": dep,
                "arrival": arr,
                "duration": block_minutes,
            }
        )

    routes = Counter()
    destinations = Counter()
    tails = Counter()
    hours_by_tail = defaultdict(int)
    hours_by_month = defaultdict(int)
    legs_by_month = Counter()
    departures = Counter()
    durations = []
    by_tail = defaultdict(list)
    for item in valid:
        flight, reg = item["flight"], item["reg"]
        depart_code = flight.get("departure")
        arrival_code = flight.get("arrival")
        if depart_code and arrival_code:
            routes[f"{depart_code} → {arrival_code}"] += 1
        if arrival_code:
            destinations[arrival_code] += 1
        if reg:
            tails[reg] += 1
            by_tail[reg].append(item)
        month = item["departure"].strftime("%Y-%m")
        legs_by_month[month] += 1
        departures[item["departure"].hour // 6] += 1
        if item["duration"] is not None:
            durations.append(item["duration"])
            hours_by_month[month] += item["duration"]
            if reg:
                hours_by_tail[reg] += item["duration"]

    turns = []
    overlaps = []
    for reg, legs in by_tail.items():
        legs.sort(key=lambda item: item["departure"])
        for previous, following in zip(legs, legs[1:]):
            if previous["arrival"] is None:
                continue
            delta = (following["departure"] - previous["arrival"]).total_seconds() / 60
            entry = {
                "tail": reg,
                "minutes": round(delta),
                "airport": previous["flight"].get("arrival") or "—",
                "date": following["departure"].strftime("%d %b"),
            }
            if delta < 0:
                overlaps.append(entry)
            else:
                turns.append(entry)
    turns.sort(key=lambda item: item["minutes"])
    longest = sorted(
        (item for item in valid if item["duration"] is not None),
        key=lambda item: item["duration"],
        reverse=True,
    )[:3]
    return {
        "legs": len(valid),
        "cancelled": cancelled,
        "unassigned": unassigned,
        "excluded": excluded,
        "tails": tails,
        "routes": routes,
        "destinations": destinations,
        "minutes": sum(durations),
        "known_durations": len(durations),
        "average": round(sum(durations) / len(durations)) if durations else None,
        "median": round(median(durations)) if durations else None,
        "hours_by_tail": dict(hours_by_tail),
        "hours_by_month": dict(hours_by_month),
        "legs_by_month": legs_by_month,
        "departures": departures,
        "turns": turns,
        "overlaps": overlaps,
        "longest": longest,
    }


async def load_analytics(period, *, now=None, loader=None):
    """Attempt all 31-day windows; never turn a failed window into zero flights."""
    if period not in PERIODS:
        raise ValueError("Неизвестный аналитический период.")
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("Требуется время UTC с часовым поясом.")
    now = now.astimezone(UTC)
    start, end = period_start(period, now.date()), now.date()
    windows = list(month_windows(start, end))
    source = loader or get_flight_schedule
    slots = asyncio.Semaphore(3)

    async def get_window(first, last):
        async with slots:
            try:
                result = await asyncio.wait_for(
                    source(first.isoformat(), last.isoformat()), timeout=60
                )
                if (
                    not isinstance(result, dict)
                    or not isinstance(result.get("flights"), list)
                ):
                    raise TypeError("Неправильный ответ Leon")
                return result
            except Exception:  # noqa: BLE001 -- failed source window stays explicitly partial
                return None

    results = await asyncio.gather(
        *(get_window(first, last) for first, last in windows)
    )
    available = [
        flight
        for result in results
        if result is not None
        for flight in result["flights"]
    ]
    complete_windows = sum(
        result is not None and result.get("complete") is True
        for result in results
    )
    available_windows = sum(result is not None for result in results)
    if available_windows == 0:
        raise RuntimeError("Leon не предоставил данных ни за один интервал.")
    metrics = aggregate(available, now)
    return {
        **metrics,
        "start": start,
        "end": end,
        "period": period,
        "fetched_at": datetime.now(UTC),
        "complete": complete_windows == len(windows),
        "total_windows": len(windows),
        "received_windows": available_windows,
        "complete_windows": complete_windows,
    }


def _top_lines(counter, *, unit="рейсов", limit=5):
    ranked = _sorted_counts(counter, limit)
    return [f"• {key}: {value} {unit}" for key, value in ranked] or ["Нет данных."]


def render_analytics(result, view):
    if view not in VIEWS:
        raise ValueError("Неизвестный раздел аналитики.")
    period = PERIODS[result["period"]]
    lines = [
        f"ANALYTICS · {VIEWS[view].upper()} · {period}",
        f"{result['start'].isoformat()} — {result['end'].isoformat()} (UTC)",
        "Источник: Leon · только расписание; не фактический журнал полётов.",
        f"Охват: {result['complete_windows']}/{result['total_windows']} интервалов",
    ]
    if not result["complete"]:
        lines.extend(
            [
                "ВНИМАНИЕ: неполная выборка. Все значения ниже — только по полученным записям.",
                "Нельзя считать отсутствие рейса в данных подтверждённым.",
            ]
        )
    lines.append("")
    if view == "management":
        lines.extend(
            [
                f"Плановых рейсов: {result['legs']}",
                f"Отмен по признаку Leon: {result['cancelled']}",
                f"Бортов с рейсами: {len(result['tails'])}",
                f"Плановое время рейсов: {minutes_label(result['minutes'])}",
                f"Без назначенного борта: {result['unassigned']}",
                "",
                "Наиболее загруженные борта (плановые рейсы):",
                *_top_lines(result["tails"]),
                "",
                "Количество рейсов по месяцам:",
                *_top_lines(
                    result["legs_by_month"], unit="рейсов",
                    limit=12,
                ),
            ]
        )
    elif view == "commercial":
        lines.extend(
            [
                f"Плановых рейсов: {result['legs']}",
                f"Уникальных маршрутов: {len(result['routes'])}",
                f"Уникальных аэропортов назначения: {len(result['destinations'])}",
                "",
                "Популярные направления:",
                *_top_lines(result["destinations"]),
                "",
                "Маршруты по частоте:",
                *_top_lines(result["routes"]),
                "",
                "Тип перевозки, пассажиры, выручка по клиентам: нет подтверждённых данных.",
            ]
        )
    elif view == "finance":
        lines.extend(
            [
                "Плановая база для финансового анализа:",
                f"Количество плановых рейсов: {result['legs']}",
                f"Суммарное плановое время: {minutes_label(result['minutes'])}",
                f"Рейсов без расчётного времени: "
                f"{result['legs'] - result['known_durations']}",
                "",
                "Лётная нагрузка по бортам (плановые часы):",
                *_top_lines(
                    {k: minutes_label(v) for k, v in result["hours_by_tail"].items()},
                    unit="", limit=10,
                ),
                "",
                "Выручка, инвойсы, фактические расходы, прибыль и маржа:",
                "нет данных из подтверждённой финансовой системы.",
                "Демонстрационные тарифы CHARTER CHECK сюда не подмешиваются.",
            ]
        )
    else:
        turns = result["turns"]
        lines.extend(
            [
                f"Рейсов с плановым временем: {result['known_durations']}/{result['legs']}",
                f"Плановые блок-часы (сумма): {minutes_label(result['minutes'])}",
                f"Средняя длительность: {minutes_label(result['average'])}",
                f"Медиана длительности: {minutes_label(result['median'])}",
                f"Плановых интервалов между рейсами: {len(turns)}",
                f"Интервалов до {TIGHT_TURN_MINUTES} мин: "
                f"{sum(t['minutes'] < TIGHT_TURN_MINUTES for t in turns)}",
                f"Перекрытий по расписанию: {len(result['overlaps'])}",
                "",
                "Короткие интервалы между рейсами (план):",
                *(
                    [
                        f"• {t['tail']} · {t['airport']} · {t['date']}: "
                        f"{minutes_label(t['minutes'])}"
                        for t in turns[:5]
                    ]
                    or ["Нет данных."]
                ),
                "",
                "Распределение плановых вылетов (UTC):",
                *[
                    f"• {n * 6:02d}:00–{(n + 1) * 6:02d}:00: "
                    f"{result['departures'][n]} рейсов"
                    for n in range(4)
                ],
                "",
                "Фактические OUT/OFF/ON/IN и отклонения от расписания: нет данных.",
                "Порог 120 мин — аналитический фильтр, не норматив turnaround.",
            ]
        )
    if result["excluded"]:
        lines.append(
            f"Пропущено {result['excluded']} записей без корректного прошедшего времени вылета."
        )
    return "\n".join(lines)
