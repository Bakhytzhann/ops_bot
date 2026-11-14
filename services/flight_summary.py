"""Compact Flight Card from source fields; detailed bulletins remain unchanged."""

import textwrap

from services.flight_display import flight_reference, flight_stamp, flight_time
from services.flight_weather import (
    compact_metar,
    grouped_reports,
    metar_stamp,
    station_label,
    station_roles,
)
from services.foreflight_operations import duration
from services.notam_filters import notam_sets
from services.weather_reports import metar_age_warning


def eet_summary(block):
    if block is None:
        return "нет данных"
    if "eet_minutes" in block.details:
        return duration(block.details["eet_minutes"]).split(" (")[0]
    return block.details.get("eet", "нет данных").split(" (")[0]


def wx_summary(snapshot, preferences=None):
    block = snapshot.blocks.get("weather")
    reports = block.details.get("reports", []) if block else []
    stations = [
        (station, roles, grouped_reports(reports, station, "METAR"))
        for station, roles in station_roles(snapshot).items()
    ]
    signatures = {
        (metar_stamp(entry["raw"]), tuple(entry["sources"]))
        for _, _, entries in stations
        for entry in entries
    }
    shared = next(iter(signatures)) if len(signatures) == 1 else None
    header = ["WX", "METAR" + (" " + shared[0] if shared and shared[0] else "")]
    if shared:
        header.extend(part for part in (" / ".join(shared[1]),) if part)
    lines = [" · ".join(header)]
    for station, roles, entries in stations:
        lines.append(
            station_label(snapshot, station, preferences)
            + " ("
            + " / ".join(roles)
            + ")"
        )
        if not entries:
            lines.append("нет данных")
        for entry in entries:
            if shared is None:
                lines.append(
                    " · ".join(
                        part
                        for part in (
                            metar_stamp(entry["raw"]),
                            " / ".join(entry["sources"]),
                        )
                        if part
                    )
                )
            text = compact_metar(entry["raw"])
            if "устарел" in metar_age_warning(
                entry["raw"], entry.get("metadata") or ""
            ):
                text += " · устарел"
            lines.append(text)
        lines.append("")
    return "\n".join(lines).strip()


def route_summary(snapshot, preferences=None):
    block = snapshot.blocks.get("route")
    route = block.details.get("route") if block else None
    return (
        textwrap.fill(
            "RTE " + route, width=82, break_long_words=False, break_on_hyphens=False
        )
        if route
        else ""
    )


def notam_summary(snapshot, preferences=None):
    lines = []
    seen = set()
    for endpoint in ("departure", "arrival"):
        station = snapshot.flight.get(endpoint)
        if not station or station in seen:
            continue
        seen.add(station)
        groups, recognized = notam_sets(snapshot, station)
        label = station_label(snapshot, station, preferences)
        lines.append(
            f"{label} · AFFECT {len(groups['A'])} · REVIEW {len(groups['R'])} · ACTIVE {len(groups['C'])}"
            if recognized
            else label + " · нет данных"
        )
    return "\n".join(lines)


def render_card(snapshot, preferences=None):
    flight = snapshot.flight
    acft = " / ".join(
        str(flight[key]) for key in ("aircraftType", "aircraft") if flight.get(key)
    )
    first = flight_reference(flight, preferences) + (f" ({acft})" if acft else "")
    departure, arrival = (
        flight_stamp(flight, endpoint, preferences)
        for endpoint in ("departure", "arrival")
    )
    offset = (arrival.date() - departure.date()).days if arrival and departure else 0
    sta = flight_time(flight, "arrival", preferences) + (
        f" ({offset:+d}d)" if offset else ""
    )
    second = f"{station_label(snapshot, flight.get('departure'), preferences)} {flight_time(flight, 'departure', preferences)} → {sta} {station_label(snapshot, flight.get('arrival'), preferences)}"
    eet = eet_summary(snapshot.blocks.get("flighttime"))
    crew = snapshot.blocks.get("crew")
    codes = crew.details.get("codes", []) if crew else []
    third = []
    if eet != "нет данных":
        third.append("полётное " + eet)
    if codes:
        third.append("CREW " + "/".join(codes))
    header = "\n".join(
        [first, second, *(["(" + " · ".join(third) + ")"] if third else [])]
    )
    lines = [header]
    if flight.get("cancelled"):
        lines.append("ОТМЕНЁН")
    lines.extend(
        [
            wx_summary(snapshot, preferences),
            route_summary(snapshot, preferences),
            "NOTAM\n" + notam_summary(snapshot, preferences),
        ]
    )
    return "\n\n".join(line for line in lines if line)
