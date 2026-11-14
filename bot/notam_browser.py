"""Complete NOTAM lists with text-size pagination and station/filter controls."""

import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from services.flight_display import flight_reference
from services.flight_weather import station_label
from services.notam_filters import FILTERS, notam_sets

PAGE_BUDGET = 3800
SEPARATOR = "\n\n──────────\n\n"
ACTION = re.compile(r"nl([DA])_([ARCT])")


def valid_action(action):
    return bool(ACTION.fullmatch(action))


def _units(text):
    return len(text.encode("utf-16-le")) // 2


def _chunks(text, budget):
    start = used = 0
    for index, char in enumerate(text):
        size = 2 if ord(char) > 0xFFFF else 1
        if used + size > budget:
            yield text[start:index]
            start, used = index, 0
        used += size
    yield text[start:]


def _record_parts(record, budget):
    body = (record.topic + "\n" if record.topic else "") + record.raw
    complete = record.identifier + "\n" + body
    if _units(complete) <= budget:
        return [complete]
    # Reserve enough digits for every possible part number before splitting.
    bound = len(body)
    label = f"{record.identifier} · PART {bound}/{bound} · CONTINUED\n"
    capacity = budget - _units(label)
    if capacity < 2:
        raise ValueError("Заголовок NOTAM слишком длинный для сообщения.")
    chunks = list(_chunks(body, capacity))
    return [
        f"{record.identifier} · PART {index}/{len(chunks)}"
        + (" · CONTINUED" if index > 1 else "")
        + "\n"
        + chunk
        for index, chunk in enumerate(chunks, 1)
    ]


def _pages(records, budget):
    pages, current, used = [], [], 0
    for offset, (_, record) in enumerate(records, 1):
        for text in _record_parts(record, budget):
            size = _units(text) + (_units(SEPARATOR) if current else 0)
            if current and used + size > budget:
                pages.append(current)
                current, used = [], 0
                size = _units(text)
            current.append((offset, text))
            used += size
    return [*pages, current] if current else pages or [[]]


def render_notams(snapshot, token, position, action, page=0, preferences=None):
    if not valid_action(action):
        raise ValueError("Неизвестный экран NOTAM.")
    endpoint, selected = ACTION.fullmatch(action).groups()
    station = snapshot.flight.get("departure" if endpoint == "D" else "arrival")
    groups, recognized = notam_sets(snapshot, station)
    records = groups[selected]
    role = "DEP" if endpoint == "D" else "ARR"
    count = len(records)
    title = f"NOTAM · {role} {station_label(snapshot, station, preferences)} · {FILTERS[selected]} {count if recognized else '—'}"
    header = flight_reference(snapshot.flight, preferences) + "\n" + title
    # An upper bound on page count keeps the reserved header width stable.
    bound = 1 + sum(
        _units(record.identifier + record.topic + record.raw) + 1
        for _, record in records
    )
    page_line = f"PAGE {bound}/{bound} · {count}–{count} / {count}"
    budget = PAGE_BUDGET - _units(header + "\n" + page_line + "\n\n")
    if budget < 100:
        raise ValueError("Заголовок NOTAM слишком длинный для сообщения.")
    pages = _pages(records, budget)
    if not 0 <= page < len(pages):
        raise ValueError("Страница NOTAM не найдена.")
    entries = pages[page]
    first, last = (entries[0][0], entries[-1][0]) if entries else (0, 0)
    text = f"{header}\nPAGE {page + 1}/{len(pages)} · {first}–{last} / {count}\n\n"
    text += (
        SEPARATOR.join(entry for _, entry in entries)
        if entries
        else ("нет записей" if recognized else "нет данных")
    )

    def button(label, target, number=0):
        return InlineKeyboardButton(
            label, callback_data=f"fc:{token}:{position}:{target}:{number}"
        )

    rows = [
        [
            button(
                "DEP "
                + station_label(
                    snapshot, snapshot.flight.get("departure"), preferences
                ),
                f"nlD_{selected}",
            ),
            button(
                "ARR "
                + station_label(snapshot, snapshot.flight.get("arrival"), preferences),
                f"nlA_{selected}",
            ),
        ],
        [
            button(
                f"{name} {len(groups[key]) if recognized else '—'}",
                f"nl{endpoint}_{key}",
            )
            for key, name in FILTERS.items()
        ],
    ]
    navigation = []
    if page:
        navigation.append(button("PREVIOUS", action, page - 1))
    if page + 1 < len(pages):
        navigation.append(button("NEXT", action, page + 1))
    if navigation:
        rows.append(navigation)
    rows.append([button("BACK · Flight Card", "card")])
    return text, InlineKeyboardMarkup(rows)
