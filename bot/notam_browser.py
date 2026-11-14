"""Snapshot-scoped NOTAM station/filter lists and complete record views."""

import hashlib
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from services.flight_display import flight_reference
from services.flight_weather import station_label
from services.notam_filters import FILTERS, notam_sets

PAGE_SIZE = 5
ACTION = re.compile(r"n([lv])([DA])_([ARCT])(?:_(\d+)_([0-9a-f]{8}))?")


def valid_action(action):
    match = ACTION.fullmatch(action)
    return bool(match and ((match[1] == "v") == (match[4] is not None)))


def render_notams(snapshot, token, position, action, page=0, preferences=None):
    if not valid_action(action):
        raise ValueError("Неизвестный экран NOTAM.")
    match = ACTION.fullmatch(action)
    view, endpoint, selected, record_index, fingerprint = match.groups()
    station = snapshot.flight.get("departure" if endpoint == "D" else "arrival")
    groups, recognized = notam_sets(snapshot, station)
    records = groups[selected]

    def button(label, target, number=0):
        return InlineKeyboardButton(
            label, callback_data=f"fc:{token}:{position}:{target}:{number}"
        )

    def record_action(index, record):
        digest = hashlib.sha256(record.raw.encode()).hexdigest()[:8]
        return f"nv{endpoint}_{selected}_{index}_{digest}"

    title = f"NOTAM · {endpoint.replace('D', 'DEP').replace('A', 'ARR')} {station_label(snapshot, station, preferences)} · {FILTERS[selected]}"
    header = flight_reference(snapshot.flight, preferences) + "\n" + title
    rows = []
    if view == "l":
        pages = max(1, (len(records) + PAGE_SIZE - 1) // PAGE_SIZE)
        if not 0 <= page < pages:
            raise ValueError("Страница NOTAM не найдена.")
        start = page * PAGE_SIZE
        end = min(start + PAGE_SIZE, len(records))
        text = f"{header}\nPAGE {page + 1}/{pages} · {start + 1 if records else 0}–{end} / {len(records)}"
        rows.append(
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
                    + station_label(
                        snapshot, snapshot.flight.get("arrival"), preferences
                    ),
                    f"nlA_{selected}",
                ),
            ]
        )
        rows.append(
            [
                button(
                    f"{name} {len(groups[key]) if recognized else '—'}",
                    f"nl{endpoint}_{key}",
                )
                for key, name in FILTERS.items()
            ]
        )
        for index, record in records[start:end]:
            label = record.identifier + (
                " · " + " ".join(record.topic.split())[:65] if record.topic else ""
            )
            rows.append([button(label, record_action(index, record))])
        if not recognized:
            text += "\nнет данных"
        elif not records:
            text += "\nнет записей"
        navigation = []
        if page:
            navigation.append(button("PREV", action, page - 1))
        if page + 1 < pages:
            navigation.append(button("NEXT", action, page + 1))
        if navigation:
            rows.append(navigation)
        rows.append([button("BACK · Flight Card", "card")])
    else:
        offset = next(
            (i for i, (index, _) in enumerate(records) if index == int(record_index)),
            None,
        )
        if offset is None:
            raise ValueError(
                "NOTAM изменён или отсутствует в фильтре. Откройте список заново."
            )
        _, record = records[offset]
        if record_action(int(record_index), record).rsplit("_", 1)[1] != fingerprint:
            raise ValueError("NOTAM изменён. Откройте список заново.")
        chunks = [
            record.raw[i : i + 3300] for i in range(0, len(record.raw), 3300)
        ] or ["нет данных"]
        if not 0 <= page < len(chunks):
            raise ValueError("Страница текста NOTAM не найдена.")
        text = f"{header}\n{record.identifier} · {offset + 1} / {len(records)}\n\n{chunks[page]}"
        if len(chunks) > 1:
            text += f"\n\nTEXT PAGE {page + 1}/{len(chunks)}"
            rows.append(
                ([button("TEXT PREV", action, page - 1)] if page else [])
                + (
                    [button("TEXT NEXT", action, page + 1)]
                    if page + 1 < len(chunks)
                    else []
                )
            )
        navigation = []
        if offset:
            navigation.append(button("PREV", record_action(*records[offset - 1])))
        if offset + 1 < len(records):
            navigation.append(button("NEXT", record_action(*records[offset + 1])))
        navigation.append(
            button("BACK", f"nl{endpoint}_{selected}", offset // PAGE_SIZE)
        )
        rows.append(navigation)
        rows.append([button("Flight Card", "card")])
    return text, InlineKeyboardMarkup(rows)
