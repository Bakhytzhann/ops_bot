"""Resolve only flight-assigned Leon handlers; enrich contacts from Airport Directory."""

import asyncio
import re

from integrations.leon.tools import call_leon
from services.snapshot_models import SourceBlock


def _text(value):
    return value.strip() if isinstance(value, str) else ""


def _assigned(row, role):
    """An airport favourite is NOT proof of a flight assignment."""
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
                    return {"name": name, "handlerNid": nid}
        elif isinstance(value, str) and value.strip():
            return {"name": value.strip(), "handlerNid": None}
    # Field names used by some Leon exports/report projections.
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


async def load_flight_handlers(flight):
    nid = flight.get("flightNid")
    if type(nid) is not int or nid <= 0:
        raise ValueError("Leon flightNid missing")
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
        try:
            entries = _directory_rows(
                await call_leon(
                    "get-airport-handling-agents",
                    {"airportCode": station, "codeType": "ICAO"},
                )
            )
        except (OSError, RuntimeError, TimeoutError, ValueError, TypeError):
            entries = []
        match = _match(entries, agent)
        return {
            "station": station,
            "name": agent["name"] or (_text(match.get("name")) if match else ""),
            "phone": _text(match.get("telephone")) if match else "",
            "email": _text(match.get("email")) if match else "",
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
        parts = [
            f"{label} {airport(flight, role, preferences)}: {info['name']}"
        ]
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
