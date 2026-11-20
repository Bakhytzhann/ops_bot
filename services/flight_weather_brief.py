"""Conservative compact METAR facts for Flight Card, not an approach assessment."""

import re

from services.flight_weather import _phenomenon, grouped_reports, station_label


def metar_facts(raw):
    body = re.split(r"\b(?:RMK|TEMPO|BECMG|NOSIG)\b", raw.upper(), maxsplit=1)[0]
    words = body.rstrip("=").split()
    wind = next(
        (
            m.group(0)
            for word in words
            if (
                m := re.fullmatch(
                    r"(?:\d{3}|VRB)\d{2,3}(?:G\d{2,3})?(?:KT|MPS|KMH)", word
                )
            )
        ),
        None,
    )
    vis = next(
        (
            "≥10 km" if word == "9999" else word + " m"
            for word in words
            if re.fullmatch(r"\d{4}", word)
        ),
        None,
    )
    if match := re.search(
        r"(?<!\S)([MP]?(?:\d+ )?\d+/\d+SM|[MP]?\d+SM)(?!\S)", body.rstrip("=")
    ):
        vis = match[1]
    if "CAVOK" in words:
        vis = "≥10 km (CAVOK)"
    rvr = [
        word
        for word in words
        if re.fullmatch(r"R\d{2}[LCR]?/[MP]?\d{4}(?:V[MP]?\d{4})?(?:FT)?[UDN]?", word)
    ]
    vertical = next(
        (word for word in words if re.fullmatch(r"VV(?:\d{3}|///)", word)), None
    )
    ceiling = min(
        (
            int(word[3:6]) * 100
            for word in words
            if re.fullmatch(r"(?:BKN|OVC)\d{3}(?:CB|TCU)?", word)
        ),
        default=None,
    )
    temp = next(
        (
            word.split("/")[0]
            for word in words
            if re.fullmatch(r"M?\d{2}/(?:M?\d{2}|XX|//)", word)
        ),
        None,
    )
    phenomena = [
        word for word in words if _phenomenon(word) and word not in ("NSC", "NCD")
    ]
    facts = {}
    if wind:
        facts["Wind"] = wind
    if vis:
        facts["VIS"] = vis
    if rvr:
        facts["RVR"] = " ".join(rvr)
    if ceiling is not None:
        facts["Ceiling"] = f"{ceiling} ft"
    if vertical:
        facts["VV"] = (
            "NOT VERIFIED" if vertical == "VV///" else f"{int(vertical[2:]) * 100} ft"
        )
    if temp:
        facts["TEMP"] = temp.replace("M", "−") + " °C"
    if phenomena:
        facts["WX"] = " ".join(phenomena)
    return facts


def concise_weather(snapshot, preferences=None):
    reports = (
        snapshot.blocks.get("weather").details.get("reports", [])
        if snapshot.blocks.get("weather")
        else []
    )
    lines = ["WX · CURRENT METAR"]
    for station in (snapshot.flight.get("departure"), snapshot.flight.get("arrival")):
        if not station:
            continue
        entries = grouped_reports(reports, station, "METAR")
        label = station_label(snapshot, station, preferences)
        if not entries:
            lines.append(f"{label}: нет METAR")
        else:
            # Do not present conflicting sources as one observation.
            raw = entries[0]["raw"]
            facts = metar_facts(raw)
            lines.append(
                f"{label}: "
                + (
                    " · ".join(f"{field} {value}" for field, value in facts.items())
                    or "нет данных"
                )
            )
            if len(entries) > 1:
                lines.append("Разные METAR у источников — см. Подготовка.")
    return "\n".join(lines)


CAT_I_REFERENCE = (
    "CAT I · ПАМЯТКА\n"
    "DH не ниже 200 ft · RVR обычно от 550 м / VIS от 800 м.\n"
    "Это справочные критерии категории, не минимум конкретного захода."
)
