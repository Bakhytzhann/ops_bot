import asyncio
import re

from config.settings import get_settings
from integrations.leon.tools import call_leon
from services.snapshot_models import SourceBlock, utc
from services.source_cache import SourceCache

_report_cache = SourceCache(16)
_REPORT_FIELDS = {
    "departure": ("ADEPHandlerName", "ADEP_handler_phone", "ADEPHandlerEmail"),
    "arrival": ("ADESHandlerName", "ADES_handler_phone", "ADESHandlerEmail"),
}


def _text(value):
    return value.strip() if isinstance(value, str) else ""


def _assigned(row, role):
    suffix = "Adep" if role == "departure" else "Ades"
    keys = (
        ("handlingAdep", "adepHandler", "departureHandler", "handlerAdep")
        if role == "departure"
        else ("handlingAdes", "adesHandler", "arrivalHandler", "handlerAdes")
    )
    for key in keys:
        value = row.get(key)
        if isinstance(value, dict):
            candidate = value.get("handler") or value.get("handlingAgent") or value
            if isinstance(candidate, dict):
                name = _text(candidate.get("name"))
                nid = candidate.get("handlerNid")
                if name or (type(nid) is int and nid > 0):
                    return {
                        "name": name,
                        "handlerNid": nid,
                        "phone": _text(
                            candidate.get("telephone") or candidate.get("phone")
                        ),
                        "email": _text(candidate.get("email")),
                    }
        elif isinstance(value, str) and value.strip():
            return {"name": value.strip(), "handlerNid": None}
    for key in (f"handlerName{suffix}", f"{role}HandlerName"):
        name = _text(row.get(key))
        if name:
            return {"name": name, "handlerNid": None}
    return None


def _directory_rows(response):
    if isinstance(response, list):
        return [row for row in response if isinstance(row, dict)]
    if isinstance(response, dict):
        for key in ("handlers", "handlingAgents", "result", "data"):
            if isinstance(response.get(key), list):
                return [row for row in response[key] if isinstance(row, dict)]
    return []


def _same_name(a, b):
    return bool(
        a
        and b
        and re.sub(r"\s+", " ", a).casefold() == re.sub(r"\s+", " ", b).casefold()
    )


def _match(directory, assigned):
    nid = assigned.get("handlerNid")
    if type(nid) is int and nid > 0:
        found = [entry for entry in directory if entry.get("handlerNid") == nid]
    else:
        found = [
            entry
            for entry in directory
            if _same_name(_text(entry.get("name")), assigned.get("name"))
        ]
    return found[0] if len(found) == 1 else None


async def _report_handlers(flight, nid, refresh):
    departure = utc(flight.get("departureUTC"))
    if departure is None:
        raise ValueError("Leon departure missing")
    day = departure.date()

    async def load():
        return await call_leon(
            "get-report-wizard-flight-scope-report",
            {
                "dateFilter": {"start": f"{day}T00:00:00Z", "end": f"{day}T23:59:59Z"},
                "columnList": [
                    "unique_id",
                    *(key for fields in _REPORT_FIELDS.values() for key in fields),
                ],
                **dict.fromkeys(
                    (
                        "acftNidList",
                        "crewMemberNidList",
                        "adepLocationNidList",
                        "adesLocationNidList",
                        "airportLocationNidList",
                        "isCanceled",
                        "permitsCountryList",
                    )
                ),
            },
        )

    report = await _report_cache.get(
        day,
        get_settings().snapshot_crew_ttl,
        load,
        refresh=refresh,
        refresh_interval=5,
    )
    rows = report.get("records") if isinstance(report, dict) else None
    if not isinstance(rows, list):
        raise TypeError("Leon handling report unavailable")
    matches = [
        row
        for row in rows
        if isinstance(row, dict) and str(row.get("unique_id")) == str(nid)
    ]
    result = {}
    if len(matches) == 1:
        row = matches[0]
        for role, (name, phone, email) in _REPORT_FIELDS.items():
            if flight.get(role) and (company := _text(row.get(name))) not in (
                "",
                "-",
                "—",
                "N/A",
            ):
                result[role] = {
                    "station": flight[role],
                    "name": company,
                    "phone": _text(row.get(phone)),
                    "email": _text(row.get(email)),
                }
    return SourceBlock(
        "Leon", status="ready" if result else "unavailable", details=result
    )


async def load_flight_handlers(flight, *, use_report=False, refresh=False):
    nid = flight.get("flightNid")
    if type(nid) is not int or nid <= 0:
        raise ValueError("Leon flightNid missing")
    if use_report:
        return await _report_handlers(flight, nid, refresh)
    payload = await call_leon("get-flights-by-flight-nids", {"flightNids": [nid]})
    if isinstance(payload, dict):
        payload = payload.get(
            "flightList", payload.get("flightsList", payload.get("result", payload))
        )
    if isinstance(payload, dict) and payload.get("flightNid") == nid:
        payload = [payload]
    if not isinstance(payload, list):
        raise TypeError("Leon flight detail response unavailable")
    matched = [
        row for row in payload if isinstance(row, dict) and row.get("flightNid") == nid
    ]
    if len(matched) != 1:
        raise ValueError("Leon flight detail mismatch")
    row = matched[0]
    assigned = {role: _assigned(row, role) for role in ("departure", "arrival")}

    async def enrich(role):
        agent = assigned[role]
        station = flight.get(role)
        if (
            not agent
            or not isinstance(station, str)
            or not re.fullmatch(r"[A-Z]{4}", station)
        ):
            return None
        phone, email = agent.get("phone", ""), agent.get("email", "")
        match = None
        if not (agent["name"] and phone and email):
            try:
                entries = _directory_rows(
                    await call_leon(
                        "get-airport-handling-agents",
                        {"airportCode": station, "codeType": "ICAO"},
                    )
                )
                match = _match(entries, agent)
            except (OSError, RuntimeError, TimeoutError, ValueError, TypeError):
                pass
        return {
            "station": station,
            "name": agent["name"] or (_text(match.get("name")) if match else ""),
            "phone": phone or (_text(match.get("telephone")) if match else ""),
            "email": email or (_text(match.get("email")) if match else ""),
        }

    outbound, inbound = await asyncio.gather(enrich("departure"), enrich("arrival"))
    return SourceBlock(
        "Leon",
        "Handling assigned by flight",
        "ready",
        details={"departure": outbound, "arrival": inbound},
    )


def format_handlers(block, flight, preferences=None):
    from services.flight_display import airport

    lines = []
    entries = block.details if block else {}
    for role, label in (("departure", "DEP"), ("arrival", "ARR")):
        info = entries.get(role)
        if not isinstance(info, dict) or not info.get("name"):
            continue
        parts = [f"{label} {airport(flight, role, preferences)}: {info['name']}"]
        if info.get("phone"):
            parts.append(info["phone"])
        if info.get("email"):
            parts.append(info["email"])
        lines.append(" · ".join(parts))
    return (
        "HANDLING\n" + "\n".join(lines)
        if lines
        else "HANDLING · не назначен / нет данных"
    )
