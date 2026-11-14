"""Deterministic UTC calendar periods for schedule requests."""

import calendar
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

MAX_SCHEDULE_DAYS = 31
MONTHS = {
    "январь": 1,
    "января": 1,
    "февраль": 2,
    "февраля": 2,
    "март": 3,
    "марта": 3,
    "апрель": 4,
    "апреля": 4,
    "май": 5,
    "мая": 5,
    "июнь": 6,
    "июня": 6,
    "июль": 7,
    "июля": 7,
    "август": 8,
    "августа": 8,
    "сентябрь": 9,
    "сентября": 9,
    "октябрь": 10,
    "октября": 10,
    "ноябрь": 11,
    "ноября": 11,
    "декабрь": 12,
    "декабря": 12,
}
MONTH_PATTERN = "|".join(MONTHS)
DATE_PATTERN = re.compile(
    rf"(?<![\w./-])(?:20\d{{2}}-\d{{2}}-\d{{2}}|"
    rf"\d{{1,2}}[./]\d{{1,2}}[./]20\d{{2}}|"
    rf"\d{{1,2}}\s+(?:{MONTH_PATTERN})(?:\s+20\d{{2}})?)(?![\w./-])"
)


@dataclass(frozen=True)
class SchedulePeriod:
    start: date
    end: date  # Inclusive calendar date.

    def __post_init__(self):
        if self.end < self.start:
            raise ValueError("Конец периода раньше начала. Укажите обе даты с годом.")
        if (self.end - self.start).days >= MAX_SCHEDULE_DAYS:
            raise ValueError("Можно запросить расписание максимум за 31 день.")


def _parse_day(value: str, year: int) -> date:
    if re.fullmatch(r"20\d{2}-\d{2}-\d{2}", value):
        return date.fromisoformat(value)
    if re.fullmatch(r"\d{1,2}[./]\d{1,2}[./]20\d{2}", value):
        day, month, explicit_year = map(int, re.split(r"[./]", value))
        return date(explicit_year, month, day)
    parts = value.split()
    return date(
        int(parts[2]) if len(parts) == 3 else year, MONTHS[parts[1]], int(parts[0])
    )


def parse_schedule_period(
    text: str, *, today: date | None = None
) -> SchedulePeriod | None:
    """Return a bounded period; reject invalid or ambiguous range expressions."""
    today = today or datetime.now(UTC).date()
    text = text.lower().replace("ё", "е")
    try:
        # Shared month: 'с 16 по 30 ноября', '16–30 ноября 2026'.
        shared = re.search(
            rf"\b(?:с\s+)?(\d{{1,2}})\s*(?:по|до|[-–—])\s*"
            rf"(\d{{1,2}})\s+({MONTH_PATTERN})(?:\s+(20\d{{2}}))?\b",
            text,
        )
        dates = list(DATE_PATTERN.finditer(text))
        if shared and len(dates) <= 1:
            first, last, month, year = shared.groups()
            year = int(year) if year else today.year
            return SchedulePeriod(
                date(year, MONTHS[month], int(first)),
                date(year, MONTHS[month], int(last)),
            )

        if dates:
            if len(dates) > 2:
                raise ValueError("Укажите одну дату или один диапазон дат.")
            # A year at the end applies to both named dates unless explicitly supplied.
            years = re.findall(r"\b20\d{2}\b", text)
            year = int(years[-1]) if years else today.year
            days = [_parse_day(match.group(), year) for match in dates]
            if len(days) == 2:
                connector = text[dates[0].end() : dates[1].start()].strip()
                if connector not in ("по", "до", "-", "–", "—"):
                    raise ValueError("Укажите диапазон в формате «с даты по дату».")
                return SchedulePeriod(*days)
            if re.search(r"\b(?:с|по|до)\s*$", text[: dates[0].start()]):
                raise ValueError("Укажите обе границы периода: «с даты по дату».")
            return SchedulePeriod(days[0], days[0])

        month_match = re.search(rf"\b({MONTH_PATTERN})(?:\s+(20\d{{2}}))?\b", text)
        if month_match:
            month_name, year = month_match.groups()
            month = MONTHS[month_name]
            year = int(year) if year else today.year
            last = calendar.monthrange(year, month)[1]
            if re.search(r"\bперв\w*\s+половин\w*\b", text):
                first, last = 1, 15
            elif re.search(r"\bвтор\w*\s+половин\w*\b", text):
                first = 16
            elif re.search(r"\b(?:весь|всю|за|на)\s+$", text[: month_match.start()]):
                first = 1
            else:
                raise ValueError(
                    "Уточните дату, половину месяца или запросите весь месяц."
                )
            return SchedulePeriod(date(year, month, first), date(year, month, last))

        relative = re.findall(r"\b(?:послезавтра|завтра|сегодня)\b", text)
        if len(relative) > 1:
            raise ValueError("Для диапазона укажите две календарные даты.")
        if relative:
            day = today + timedelta(
                days={"сегодня": 0, "завтра": 1, "послезавтра": 2}[relative[0]]
            )
            return SchedulePeriod(day, day)
        return None
    except ValueError as exc:
        if str(exc).startswith(("Конец", "Можно", "Укажите", "Уточните", "Для")):
            raise
        raise ValueError("Некорректная дата. Проверьте день, месяц и год.") from exc
