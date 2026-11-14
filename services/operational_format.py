"""Deterministic presentation; never ask the LLM to alter retrieved facts."""

from services.weather_reports import format_awc_weather, format_leon_weather

__all__ = [
    "format_awc_weather",
    "format_crew_operations",
    "format_leon_weather",
    "format_watch",
]

WATCH_FIELDS = {
    "ctotIso": "CTOT",
    "totIso": "Target take-off",
    "etdIso": "ETD",
    "nextInformation": "Следующая информация",
    "bloffIso": "Off-block",
    "toIso": "Фактический взлёт",
    "eetIso": "EET",
    "etaIso": "ETA",
    "ldgIso": "Посадка",
    "blonIso": "On-block",
    "tobt": "TOBT",
    "tsat": "TSAT",
    "delayCode": "Код задержки",
    "regCause": "Причина регулирования",
}


def format_watch(result):
    f = result["flight"]
    lines = [
        f"Flight Watch {f['flightNumber']}: {f['departure']} → {f['arrival']}",
        "Источник: Leon MCP. Время UTC.",
        f"План: {f['departureUTC']} → {f['arrivalUTC']}",
    ]
    for item in result["data"]:
        watch = item.get("flightWatch") or {}
        if not isinstance(watch, dict):
            continue
        values = [
            f"{label}: {watch[key]}"
            for key, label in WATCH_FIELDS.items()
            if watch.get(key) is not None
        ]
        lines.extend(values or ["Flight Watch не заполнен в Leon."])
    if not result["data"]:
        lines.append("Flight Watch не найден в Leon.")
    return "\n".join(lines)


def _display(value, indent=0):
    """Readable bounded tree for roster fields whose shape can vary by duty type."""
    if value is None:
        return "—"
    if isinstance(value, list):
        return "\n".join(_display(v, indent) for v in value) or "Нет записей."
    if isinstance(value, dict):
        # Identity/contact/travel document fields are never emitted by this renderer.
        allowed = {
            "startTime",
            "endTime",
            "startTimeIso",
            "endTimeIso",
            "start",
            "end",
            "startDate",
            "endDate",
            "date",
            "type",
            "name",
            "label",
            "code",
            "status",
            "availability",
            "dutyDefinition",
            "definition",
            "legend",
            "cdrType",
            "duty",
            "duties",
            "dutyList",
            "roster",
            "rosterDuties",
            "aircraft",
            "registration",
            "airport",
            "location",
            "duration",
            "description",
            "onDays",
            "offDays",
            "standby",
            "training",
            "aircraftShifts",
            "offs",
            "dutyEntries",
        }
        labels = {
            "startTime": "Начало",
            "endTime": "Конец",
            "type": "Тип",
            "name": "Название",
            "status": "Статус",
            "duration": "Длительность",
            "startTimeIso": "Начало",
            "endTimeIso": "Конец",
        }
        parts = [
            f"{' ' * indent}{labels.get(k, k)}: {_display(v, indent + 2)}"
            for k, v in value.items()
            if k in allowed
        ]
        return (
            "\n".join(parts) or "Получена запись без поддерживаемых полей отображения."
        )
    return str(value)


def _limits(value, path=""):
    if isinstance(value, dict):
        if "actual" in value and "limit" in value:
            yield (
                f"{value.get('label') or path}: факт {value['actual']} мин; "
                f"лимит {value['limit']} мин ({value.get('limitType') or '—'}); "
                f"нарушение: {'ДА' if value.get('offence') is True else 'нет' if value.get('offence') is False else 'не указано'}"
            )
        else:
            for key, child in value.items():
                yield from _limits(child, key)
    elif isinstance(value, list):
        for child in value:
            yield from _limits(child, path)


def format_crew_operations(result):
    c = result["crew"]
    lines = [
        f"{c['name']} ({c['code'] or 'без кода'}): {result['start']} — {result['end']}",
        "Источник: Leon MCP. Время UTC.",
    ]
    data = result["data"]
    if result["mode"] == "roster":
        lines.extend(["График дежурств (не список рейсов):", _display(data)])
    else:
        if isinstance(data, dict) and isinstance(data.get("ftl"), dict):
            key = (
                "ftlViolationsForCrewMember"
                if result["mode"] == "violations"
                else "ftlCalculationsForCrewMember"
            )
            data = data["ftl"].get(key)
        rows = data.get("calculations") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            return "\n".join([*lines, "Неожиданный формат FTL: расчёты не распознаны."])
        if not rows:
            lines.append(
                "Leon не вернул дней с нарушениями."
                if result["mode"] == "violations"
                else "Leon не вернул расчётов FTL за период."
            )
        for row in rows:
            lines.append(
                f"Дата: {row.get('date', '—')}; dayOffStatus: {row.get('dayOffStatus', '—')}"
            )
            lines.extend(_limits(row))
            for duty in row.get("rosterDuties") or []:
                lines.append(
                    f"{duty.get('type', '—')}: {duty.get('startTimeIso', '—')} — {duty.get('endTimeIso', '—')}"
                )
        lines.append("FTL показан по расчётам Leon; это не решение о допуске к рейсу.")
    text = "\n".join(lines)
    return (
        text
        if len(text) <= 18000
        else text[:18000] + "\nВывод сокращён. Запросите меньший период."
    )
