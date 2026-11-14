"""Deterministic ForeFlight flight lookup, presentation and Leon comparison."""

import math
import re
from datetime import UTC, date, datetime, timedelta
from urllib.parse import urlsplit

from integrations.foreflight.api_client import ForeFlightClient, ForeFlightError
from integrations.leon.operations import resolve_flight


def normalized(value):
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def instant(value):
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value)
        return result.astimezone(UTC) if result.tzinfo else None
    except ValueError:
        return None


def flight_rows(result):
    if not isinstance(result, dict) or not isinstance(result.get("flights"), list):
        raise ForeFlightError("ForeFlight: список рейсов имеет неожиданный формат")
    if result.get("warnings"):
        raise ValueError(
            "ForeFlight вернул предупреждения к списку рейсов. Автоматическое сопоставление остановлено."
        )
    if not all(isinstance(item, dict) for item in result["flights"]):
        raise ForeFlightError("ForeFlight: неожиданный формат записи рейса")
    return result["flights"]


async def find_flight(client, number, day, leon=None):
    start = datetime.combine(date.fromisoformat(day), datetime.min.time(), tzinfo=UTC)
    rows = flight_rows(await client.list_flights(start, start + timedelta(days=1)))
    rows = [
        r
        for r in rows
        if instant(r.get("departureTime")) is not None
        and start <= instant(r["departureTime"]) < start + timedelta(days=1)
    ]
    number = normalized(number)
    matches = [r for r in rows if normalized(r.get("callSign")) == number]
    basis = "номер/позывной и дата"
    if not matches and leon:
        departure = instant(leon.get("departureUTC"))
        matches = [
            r
            for r in rows
            if all(
                (
                    normalized(leon.get("departure")),
                    normalized(leon.get("arrival")),
                    normalized(leon.get("aircraft")),
                )
            )
            and normalized(r.get("departure")) == normalized(leon["departure"])
            and normalized(r.get("destination")) == normalized(leon["arrival"])
            and normalized(r.get("aircraftRegistration"))
            == normalized(leon["aircraft"])
            and departure is not None
            and instant(r.get("departureTime")) is not None
            and abs((instant(r["departureTime"]) - departure).total_seconds()) <= 3600
        ]
        basis = "аэропорты, борт и вылет в пределах 60 минут"
    if not matches:
        raise ValueError(
            f"ForeFlight: рейс {number} на {day} не найден однозначно. Проверьте позывной и дату."
        )
    if len(matches) != 1:
        options = "; ".join(
            f"{r.get('departure', '—')} → {r.get('destination', '—')} {r.get('departureTime', '—')}"
            for r in matches[:5]
        )
        raise ValueError(
            f"ForeFlight: несколько совпадений для {number}: {options}. Уточните рейс в Dispatch."
        )
    fid = matches[0].get("flightId")
    if not isinstance(fid, str):
        raise ForeFlightError("ForeFlight не предоставил flightId")
    return matches[0], basis


def duration(minutes):
    if (
        not isinstance(minutes, (int, float))
        or isinstance(minutes, bool)
        or not math.isfinite(minutes)
        or minutes < 0
    ):
        return "нет данных"
    return f"{int(minutes) // 60:02d}:{int(minutes) % 60:02d} ({minutes:g} мин)"


def _header(number, day, summary):
    return [
        f"ForeFlight {number.upper()} на {day}",
        f"{summary.get('departure') or '—'} → {summary.get('destination') or '—'}; борт {summary.get('aircraftRegistration') or '—'}",
        "Источник: ForeFlight Dispatch. Время UTC.",
    ]


def _comparison(leon, summary, detail, basis):
    data = detail.get("flightData") or {}
    fields = [
        (
            "Вылет (аэропорт)",
            leon.get("departure"),
            data.get("departure") or summary.get("departure"),
            False,
        ),
        (
            "Прилёт (аэропорт)",
            leon.get("arrival"),
            data.get("destination") or summary.get("destination"),
            False,
        ),
        (
            "Борт",
            leon.get("aircraft"),
            data.get("aircraftRegistration") or summary.get("aircraftRegistration"),
            False,
        ),
        (
            "Плановый вылет UTC",
            leon.get("departureUTC"),
            data.get("scheduledTimeOfDeparture") or summary.get("departureTime"),
            True,
        ),
    ]
    lines = ["Сравнение Leon MCP ↔ ForeFlight Dispatch", f"Сопоставление: {basis}."]
    for label, left, right, is_time in fields:
        if not left or not right:
            status = "недостаточно данных"
        elif is_time:
            a, b = instant(left), instant(right)
            status = (
                "недостаточно данных"
                if a is None or b is None
                else "совпадает"
                if a == b
                else f"РАСХОЖДЕНИЕ: {(b - a).total_seconds() / 60:+g} мин"
            )
        else:
            status = (
                "совпадает" if normalized(left) == normalized(right) else "РАСХОЖДЕНИЕ"
            )
        lines.extend(
            [
                f"{label}: {status}",
                f"  Leon: {left or '—'}",
                f"  ForeFlight: {right or '—'}",
            ]
        )
    lines.extend(
        [
            f"Прилёт Leon (план): {leon.get('arrivalUTC') or '—'}",
            f"Прилёт ForeFlight (ETA): {summary.get('arrivalTime') or '—'}",
            "Плановый прилёт и ETA имеют разный смысл; не отмечаются как совпадение/расхождение.",
            "Трасса по точкам из Leon в этом сравнении не получена; сравниваются аэропорты маршрута.",
        ]
    )
    return "\n".join(lines)


async def foreflight_operation(mode: str, number: str, day: str) -> str:
    if mode not in {"flighttime", "route", "briefing", "overflight", "compare"}:
        raise ValueError("Неизвестная команда ForeFlight")
    if not re.fullmatch(r"[A-Z]{2,3}\d{1,6}[A-Z]?", number.upper()):
        raise ValueError("Укажите номер рейса и дату, например ABC1265 на 2026-11-11.")
    leon = await resolve_flight(number, day) if mode == "compare" else None
    async with ForeFlightClient() as client:
        summary, basis = await find_flight(client, number, day, leon)
        fid = summary["flightId"]
        lines = _header(number, day, summary)
        if mode == "briefing":
            document = await client.get_document(fid, "briefing")
            url = document.get("url") if isinstance(document, dict) else None
            if (
                not isinstance(url, str)
                or urlsplit(url).scheme not in {"https", "http"}
                or not urlsplit(url).hostname
                or any(c.isspace() for c in url)
            ):
                raise ForeFlightError(
                    "ForeFlight не предоставил корректную ссылку на брифинг"
                )
            lines.extend(
                [
                    f"Брифинг сформирован: {document.get('timeGenerated') or 'не указано'}",
                    url,
                ]
            )
        elif mode == "overflight":
            data = await client.get_overflight(fid)
            if not isinstance(data, dict):
                raise ForeFlightError("ForeFlight: неожиданный формат данных пролёта")
            for key, label in (
                ("overflownCountries", "Страны"),
                ("overflownFirs", "FIR/UIR"),
            ):
                lines.append(label + ":")
                rows = data.get(key)
                if not rows:
                    lines.append("Источник не предоставил записей.")
                else:
                    for row in rows:
                        lines.append(
                            f"{row.get('identifier') or row.get('isoCode') or ''} {row.get('name') or '—'} ({row.get('airspaceType') or label}): {row.get('entryTime') or '—'} → {row.get('exitTime') or '—'}"
                        )
            lines.append(
                "Это данные маршрута; наличие разрешений на пролёт здесь не проверяется."
            )
        else:
            detail = await client.get_flight(fid)
            if not isinstance(detail, dict):
                raise ForeFlightError("ForeFlight: неожиданный формат карточки рейса")
            if detail.get("errors"):
                lines.append(
                    "ForeFlight вернул ошибки: часть данных рейса может отсутствовать."
                )
            data = detail.get("flightData") or {}
            if mode == "compare":
                lines.append(_comparison(leon, summary, detail, basis))
            elif mode == "flighttime":
                performance = detail.get("performance") or {}
                times = performance.get("times") or {}
                lines.extend(
                    [
                        "Расчётное полётное время (ETE): "
                        + duration(times.get("timeToDestinationMinutes")),
                        "Руление: " + duration(times.get("taxiTimeMinutes")),
                        f"Плановый вылет UTC: {times.get('departureTime') or data.get('scheduledTimeOfDeparture') or '—'}",
                        f"ETA UTC: {times.get('estimatedArrivalTime') or summary.get('arrivalTime') or '—'}",
                    ]
                )
                log = data.get("flightLogTime") or summary.get("flightLogTime") or {}
                off, on = instant(log.get("timeOff")), instant(log.get("timeOn"))
                if off and on and on >= off:
                    lines.append(
                        "Фактическое полётное время (взлёт–посадка): "
                        + duration((on - off).total_seconds() / 60)
                    )
                else:
                    lines.append(
                        "Фактическое полётное время: времена взлёта/посадки не предоставлены."
                    )
                if performance.get("errors") or performance.get("warnings"):
                    lines.append(
                        "В расчёте ForeFlight есть ошибки/предупреждения; проверьте Dispatch."
                    )
            elif mode == "route":
                route = data.get("routeToDestination") or {}
                altitude = route.get("altitude") or {}
                lines.extend(
                    [
                        f"Маршрут: {route.get('route') or summary.get('route') or 'не предоставлен'}",
                        f"Высота/эшелон: {altitude.get('altitude') if altitude.get('altitude') is not None else '—'} {altitude.get('unit') or ''}",
                        f"Запасной: {data.get('alternate') or '—'}; второй: {data.get('secondAlternate') or '—'}",
                        f"Запасной на взлёте: {data.get('takeOffAlternate') or '—'}",
                        f"Правила полёта: {data.get('flightRule') or '—'}",
                    ]
                )
        return "\n".join(lines)
