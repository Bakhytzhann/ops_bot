import asyncio
import re

from integrations.awc.client import get_weather
from services.flight_display import airport
from services.weather_decode import WEATHER
from services.weather_reports import awc_reports


def station_roles(snapshot):
    route = snapshot.blocks.get("route")
    details = route.details if route else {}
    stations = {}
    for code, role in (
        (snapshot.flight.get("departure"), "DEP"),
        (snapshot.flight.get("arrival"), "ARR"),
        (details.get("alternate"), "ALTN"),
        (details.get("secondAlternate"), "ALTN 2"),
        (details.get("takeOffAlternate"), "TO ALTN"),
    ):
        if isinstance(code, str) and re.fullmatch(r"[A-Z]{4}", code):
            stations.setdefault(code, []).append(role)
    return stations


def station_label(snapshot, code, preferences=None):
    if not code:
        return "нет данных"
    endpoint = next(
        (key for key in ("departure", "arrival") if snapshot.flight.get(key) == code),
        None,
    )
    if endpoint:
        return airport(snapshot.flight, endpoint, preferences)
    return f"{code} (ICAO)" if (preferences or {}).get("codes") == "IATA" else code


async def alternate_reports(stations):
    codes = list(dict.fromkeys(stations))
    batches = [codes[i : i + 4] for i in range(0, len(codes), 4)]
    results = await asyncio.gather(
        *(get_weather(batch, include_taf=True) for batch in batches),
        return_exceptions=True,
    )
    reports, errors = [], []
    for result in results:
        if isinstance(result, Exception):
            errors.append("AWC: нет данных по запасным")
        else:
            rows, failures = awc_reports(result, True)
            reports.extend(rows)
            errors.extend(failures)
    return {"reports": reports, "errors": errors}


def grouped_reports(reports, station, product):
    grouped = {}
    for report in reports:
        raw = str(report.get("raw") or "").strip()
        if (
            report.get("station") != station
            or report.get("product") != product
            or raw in {"", "NIL", "N/A"}
        ):
            continue
        key = " ".join(raw.split())
        entry = grouped.setdefault(key, {**report, "sources": []})
        for field in ("issued_at", "valid_from", "valid_to"):
            if not entry.get(field) and report.get(field):
                entry[field] = report[field]
        source = str(report.get("source") or "").replace("Leon MCP", "Leon")
        if source and source not in entry["sources"]:
            entry["sources"].append(source)
    return list(grouped.values())


def metar_stamp(raw):
    match = re.search(r"\b\d{6}Z\b", raw)
    return match[0] if match else ""


def compact_metar(raw):
    body = re.split(r"\b(?:RMK|TEMPO|BECMG|NOSIG)\b", raw, maxsplit=1)[0]
    result = []
    tokens = body.rstrip("=").split()
    skip = set()
    for i, token in enumerate(tokens):
        if i in skip:
            continue
        if (
            token.isdigit()
            and i + 1 < len(tokens)
            and re.fullmatch(r"\d+/\d+SM", tokens[i + 1])
        ):
            result.append(token + " " + tokens[i + 1])
            skip.add(i + 1)
            continue
        if (
            re.fullmatch(
                r"(?:\d{3}|VRB)\d{2,3}(?:G\d{2,3})?(?:KT|MPS|KMH)|\d{3}V\d{3}", token
            )
            or re.fullmatch(r"\d{4}(?:[NSEW]{1,2})?|[MP]?\d+(?:/\d+)?SM|CAVOK", token)
            or re.fullmatch(
                r"R\d{2}[LCR]?/[MP]?\d{4}(?:V[MP]?\d{4})?(?:FT)?[UDN]?", token
            )
            or re.fullmatch(
                r"(?:FEW|SCT|BKN|OVC)(?:\d{3}|///)(?:CB|TCU)?|VV(?:\d{3}|///)|NSC|NCD|SKC|CLR",
                token,
            )
            or _phenomenon(token)
        ):
            result.append(token)
        elif re.fullmatch(r"(M?\d{2})/(?:M?\d{2}|XX|//)", token):
            temp = token.split("/")[0]
            number = int(temp.replace("M", "-"))
            result.append(
                ("−" if temp.startswith("M") else "+") + str(abs(number)) + "°C"
            )
        elif (
            re.fullmatch(r"Q\d{4}|A\d{4}", token)
            or re.fullmatch(r"\d+", token)
            and i + 1 < len(tokens)
            and "SM" in tokens[i + 1]
        ):
            result.append(token)
    return " · ".join(result) or "нет данных"


def _phenomenon(token):
    value = token.lstrip("+-")
    value = value[2:] if value.startswith("VC") else value
    return bool(
        value
        and len(value) % 2 == 0
        and all(value[i : i + 2] in WEATHER for i in range(0, len(value), 2))
    )


def full_weather(snapshot, preferences=None):
    block = snapshot.blocks.get("weather")
    reports = block.details.get("reports", []) if block else []
    if not reports:
        return block.text if block and block.text else "нет данных"
    lines = []
    for station, roles in station_roles(snapshot).items():
        products = {
            product: grouped_reports(reports, station, product)
            for product in ("METAR", "TAF")
        }
        sources = {
            tuple(entry["sources"])
            for entries in products.values()
            for entry in entries
        }
        shared = next(iter(sources)) if len(sources) == 1 else None
        suffix = " · " + " / ".join(shared) if shared else ""
        lines.append(
            f"{station_label(snapshot, station, preferences)} ({' / '.join(roles)})"
            + suffix
        )
        for product in ("METAR", "TAF"):
            entries = products[product]
            if not entries:
                lines.append(product + ": нет данных")
            for entry in entries:
                lines.append(
                    product
                    + (" · " + " / ".join(entry["sources"]) if shared is None else "")
                )
                raw = entry["raw"]
                if entry.get("metadata") and (
                    not metar_stamp(raw)
                    or product == "TAF"
                    and not re.search(r"\b\d{4}/\d{4}\b", raw)
                ):
                    lines.append(entry["metadata"])
                lines.append(raw)
        lines.append("")
    return "\n".join(lines).strip()
