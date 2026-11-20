"""Compact Flight Card from source fields; detailed bulletins remain unchanged."""

import json
import re
from pathlib import Path

from services.flight_display import flight_date, flight_stamp, flight_time
from services.flight_weather import (
    grouped_reports,
    station_label,
    station_roles,
)
from services.flight_weather_brief import CAT_I_REFERENCE, concise_weather
from services.foreflight_operations import duration
from services.notam_filters import notam_sets

_COUNTRY_CODES = {
    country[key].strip().casefold(): country["alpha_3"]
    for country in json.loads(
        Path(__file__).with_name("iso_countries.json").read_text(encoding="utf-8")
    )["countries"]
    for key in ("alpha_2", "alpha_3", "name", "official_name", "common_name")
    if key in country
}


def eet_summary(block):
    if block is None:
        return "нет данных"
    if "eet_minutes" in block.details:
        return duration(block.details["eet_minutes"]).split(" (")[0]
    return block.details.get("eet", "нет данных").split(" (")[0]


def wx_summary(snapshot, preferences=None):
    block = snapshot.blocks.get("weather")
    reports = block.details.get("reports", []) if block else []
    lines = ["WX"]
    for station in station_roles(snapshot):
        label = station_label(snapshot, station, preferences)
        entries = grouped_reports(reports, station, "METAR")
        if not entries:
            lines.extend([f"{label} NO WX REPORT", ""])
            continue
        for entry in entries:
            sources = "/".join(source.upper() for source in entry["sources"])
            raw = entry["raw"]
            product = "METAR"
            for prefix in ("METAR ", "SPECI "):
                if raw.startswith(prefix):
                    product, raw = prefix.strip(), raw[len(prefix) :]
                    break
            if label == station:
                raw = raw.removeprefix(station + " ")
            lines.extend([f"{label} ({sources}) {product} {raw}", ""])
    return "\n".join(lines).rstrip()


def route_summary(snapshot, preferences=None):
    block = snapshot.blocks.get("route")
    countries = block.details.get("overflownCountries") if block else None
    if not isinstance(countries, list) or not countries:
        return "Пролеты: нет данных"
    codes = [
        next(
            (
                _COUNTRY_CODES[value.strip().casefold()]
                for key in ("isoCode", "name")
                if isinstance(country, dict)
                and isinstance(value := country.get(key), str)
                and value.strip().casefold() in _COUNTRY_CODES
            ),
            "нет данных",
        )
        for country in countries
    ]
    return "Пролеты: " + ", ".join(
        code
        for index, code in enumerate(codes)
        if not index or code != codes[index - 1]
    )


def notam_summary(snapshot, preferences=None):
    stations = station_roles(snapshot)
    primary, alternates = [], []
    for station, roles in stations.items():
        groups, recognized = notam_sets(snapshot, station)
        counts = (
            "/".join(str(len(groups[key])) for key in ("A", "R", "C"))
            if recognized
            else "—/—/—"
        )
        entry = f"{station_label(snapshot, station, preferences)} ({counts})"
        (primary if "DEP" in roles or "ARR" in roles else alternates).append(entry)
    return "NOTAM (AFFECT / REVIEW / ACTIVE):\n" + " ".join(
        [
            *primary,
            *(["ALTN: " + " ".join(alternates)] if alternates else []),
        ]
    )


def render_card(snapshot, preferences=None):
    flight = snapshot.flight
    number = flight.get("flightNumber") or "нет данных"
    registration = f" {flight['aircraft']}" if flight.get("aircraft") else ""
    display_preferences = preferences
    departure, arrival = (
        flight_stamp(flight, endpoint, display_preferences)
        for endpoint in ("departure", "arrival")
    )
    offset = (arrival.date() - departure.date()).days if arrival and departure else 0
    sta = flight_time(flight, "arrival", display_preferences) + (
        f" ({offset:+d}d)" if offset else ""
    )
    first = " ".join(
        part
        for part in (
            f"{number}{registration}",
            flight_date(flight, display_preferences),
            station_label(snapshot, flight.get("departure"), preferences),
            flight_time(flight, "departure", display_preferences),
            sta,
            station_label(snapshot, flight.get("arrival"), preferences),
        )
        if part
    )
    lines = [first]
    if flight.get("cancelled"):
        lines.append("ОТМЕНЁН")
    lines.append("ETE " + eet_summary(snapshot.blocks.get("flighttime")))

    crew = snapshot.blocks.get("crew")
    duty = snapshot.blocks.get("duty")
    codes = crew.details.get("codes", []) if crew else []
    crew_line = "CREW " + ("/".join(codes) if codes else "нет данных")
    lines.append(crew_line)

    if duty and duty.text:
        summary = duty.details.get("summary", "")
        compact = [
            part.strip()
            for part in summary.splitlines()
            if any(key in part for key in ("Duty по графику", "Reporting duty interval", "Rest Before"))
        ]
        if compact:
            lines.append("DUTY / REST · " + " | ".join(compact[:4]))
        fdp_lines = [
            line.strip()
            for line in duty.text.splitlines()
            if re.match(r"^(FDP:|nextEOBT:|Rest Before:|Rest After:)", line.strip())
        ]
        if fdp_lines:
            lines.append("FDP / NEXT EOBT · " + " | ".join(fdp_lines[:8]))
    else:
        lines.append("FDP / NEXT EOBT · загружается / нет данных")
    hotels = crew.details.get("hotels", []) if crew else []
    lines.append(
        "HOTEL · " + (
            " | ".join(f"{item['position']}: {item['name']}" for item in hotels)
            if hotels else "нет подтверждённых данных"
        )
    )

    lines.append(concise_weather(snapshot, preferences))
    lines.append(route_summary(snapshot, preferences))
    lines.append("NOTAM · Подготовка (по запросу)")
    lines.append(CAT_I_REFERENCE)
    return "\n\n".join(line for line in lines if line)
