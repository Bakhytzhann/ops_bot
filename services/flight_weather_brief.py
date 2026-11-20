"""Conservative compact METAR facts for Flight Card, not an approach assessment."""

import re

from services.flight_weather import grouped_reports, station_label


def _weather(raw):
    body = re.split(r"\bRMK\b", raw.upper(), maxsplit=1)[0]
    words = body.split()
    wind = next(
        (
            m.group(0)
            for word in words
            if (m := re.fullmatch(r"(?:\d{3}|VRB)\d{2,3}(?:G\d{2,3})?(?:KT|MPS)", word))
        ),
        "—",
    )
    vis = next(
        (word + "m" for word in words if re.fullmatch(r"\d{4}", word)),
        "—",
    )
    if "CAVOK" in words:
        vis = "≥10km (CAVOK)"
    rvr = [
        word
        for word in words
        if re.fullmatch(r"R\d{2}[LCR]?/[MP]?\d{4}(?:V[MP]?\d{4})?(?:FT)?[UDN]?", word)
    ]
    vertical = next((word for word in words if re.fullmatch(r"VV\d{3}", word)), None)
    ceiling = next(
        (word for word in words if re.fullmatch(r"(?:BKN|OVC)\d{3}(?:CB|TCU)?", word)),
        None,
    )
    temp = next(
        (word for word in words if re.fullmatch(r"M?\d{2}/(?:M?\d{2}|//)", word)),
        None,
    )
    phenomena = [
        word
        for word in words
        if re.fullmatch(
            r"(?:\+|-|VC)?(?:TS|SH|FZ)?(?:RA|SN|DZ|SG|PL|GR|GS|BR|FG|HZ|FU|DU|SA)+",
            word,
        )
    ]
    parts = [f"Wind {wind}", f"VIS {vis}"]
    if rvr:
        parts.append("RVR " + " ".join(rvr[:2]))
    if vertical:
        parts.append("VV " + str(int(vertical[2:]) * 100) + "ft")
    elif ceiling:
        parts.append("Ceiling " + str(int(ceiling[3:6]) * 100) + "ft")
    if temp:
        parts.append("T/DP " + temp.replace("M", "−") + "°C")
    if phenomena:
        parts.append("WX " + " ".join(phenomena[:3]))
    return " · ".join(parts)


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
            lines.append(f"{label}: {_weather(raw)}")
            if len(entries) > 1:
                lines.append("Разные METAR у источников — см. Подготовка.")
    return "\n".join(lines)


CAT_I_REFERENCE = (
    "CAT I · ПАМЯТКА\n"
    "DH не ниже 200 ft · RVR обычно от 550 м / VIS от 800 м.\n"
    "Это справочные критерии категории, не минимум конкретного захода."
)
