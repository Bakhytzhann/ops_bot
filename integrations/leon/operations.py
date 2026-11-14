"""Read-only operational workflows using verified Leon MCP schemas."""

import re
from datetime import date

from config.access import require_access
from integrations.leon.tools import call_leon, get_flight_schedule
from services.schedule_dates import SchedulePeriod


def records(data):
    """Accept a single record or a list, rejecting server error payloads."""
    if isinstance(data, dict):
        if data.get("error") or data.get("errors"):
            raise RuntimeError("Leon returned an error response")
        return [data]
    if isinstance(data, list) and all(isinstance(item, dict) for item in data):
        if any(item.get("error") or item.get("errors") for item in data):
            raise RuntimeError("Leon returned an error response")
        return data
    raise TypeError("Unexpected Leon response structure")


async def resolve_flight(number: str, day: str) -> dict:
    require_access()
    number = re.sub(r"[\s-]", "", number).upper()
    if not re.fullmatch(r"[A-Z]{2,3}\d{1,6}[A-Z]?", number):
        raise ValueError("Укажите номер рейса, например ABC1265, и дату.")
    schedule = await get_flight_schedule(day)
    if not schedule["complete"]:
        raise ValueError("Расписание неполное: нельзя однозначно определить рейс.")
    matches = [
        f
        for f in schedule["flights"]
        if re.sub(r"[\s-]", "", f.get("flightNumber") or "").upper() == number
    ]
    if not matches:
        raise ValueError(f"Рейс {number} на {day} не найден в Leon.")
    if len(matches) > 1:
        choices = "; ".join(
            f"{f['departure']} → {f['arrival']} {f['departureUTC']}" for f in matches
        )
        raise ValueError(
            f"Найдено несколько рейсов {number}: {choices}. Уточните запрос в Leon."
        )
    if type(matches[0].get("flightNid")) is not int or matches[0]["flightNid"] <= 0:
        raise RuntimeError("Leon returned an invalid flightNid")
    return matches[0]


async def get_flight_watch(number: str, date: str) -> dict:
    require_access()
    flight = await resolve_flight(number, date)
    data = await call_leon(
        "get-flight-watch-by-flight-nids", {"flightNids": [flight["flightNid"]]}
    )
    return {"source": "Leon MCP", "flight": flight, "data": records(data)}


async def get_flight_weather_leon(flight: dict) -> list:
    require_access()
    return records(
        await call_leon(
            "get-weather-for-flight-list", {"flightNids": [flight["flightNid"]]}
        )
    )


async def resolve_crew(query: str) -> dict:
    require_access()
    if not query.strip() or len(query) > 100:
        raise ValueError("Укажите имя или код сотрудника.")
    data = records(await call_leon("get-crew-by-wildcard", {"wildcard": query.strip()}))
    candidates = []
    for item in data:
        login_value = item.get("login")
        login = login_value if isinstance(login_value, dict) else {}
        contact = item.get("contact") or {}
        nid = login.get("loginNid")
        contact_nid = contact.get("contactNid")
        if (type(nid) is int and nid > 0) or (
            type(contact_nid) is int and contact_nid > 0
        ):
            candidates.append(
                {
                    "loginNid": nid,
                    "contactNid": contact_nid,
                    "code": contact.get("personCode")
                    or login.get("code")
                    or (login_value if isinstance(login_value, str) else None),
                    "name": contact.get("knownAs")
                    or login.get("knownAs")
                    or " ".join(
                        str(contact.get(k) or login.get(k) or "")
                        for k in ("name", "surname")
                    ).strip(),
                }
            )
    exact = [
        c
        for c in candidates
        if str(c["code"] or "").casefold() == query.strip().casefold()
    ]
    if not candidates:
        raise ValueError("Сотрудник не найден в Leon. Уточните имя или код.")
    if len(exact) == 1:
        selected = exact[0]
    elif len(candidates) == 1:
        selected = candidates[0]
    else:
        choices = "; ".join(
            f"{c['name']} ({c['code'] or 'без кода'})" for c in candidates[:10]
        )
        raise ValueError(
            f"Найдено несколько сотрудников: {choices}. Укажите точный код."
        )
    if type(selected["loginNid"]) is not int or selected["loginNid"] <= 0:
        profiles = records(
            await call_leon(
                "get-contact-profiles-by-nid-list",
                {"contactsNids": [selected["contactNid"]]},
            )
        )
        matches = [p for p in profiles if p.get("contactNid") == selected["contactNid"]]
        profile_login = matches[0].get("login") if len(matches) == 1 else None
        nid = profile_login.get("loginNid") if isinstance(profile_login, dict) else None
        if type(nid) is not int or nid <= 0:
            raise ValueError(
                "Leon не предоставил loginNid сотрудника; график и FTL не запрошены."
            )
        selected["loginNid"] = nid
    return {k: selected[k] for k in ("loginNid", "code", "name")}


async def get_crew_operations(
    crew: str, start_date: str, end_date: str, mode: str = "roster"
) -> dict:
    require_access()
    period = SchedulePeriod(
        date.fromisoformat(start_date), date.fromisoformat(end_date)
    )
    if mode not in {"roster", "ftl", "violations"}:
        raise ValueError("Unknown crew operation")
    member = await resolve_crew(crew)
    interval = {"start": f"{period.start}T00:00:00Z", "end": f"{period.end}T23:59:59Z"}
    if mode == "roster":
        name = "get-duty-list"
        arguments = {
            "loginNidList": [member["loginNid"]],
            "timeInterval": interval,
            "dutyDefinitionNidList": None,
        }
    else:
        name = "get-crew-ftl-sheet" if mode == "ftl" else "get-crew-ftl-violations"
        arguments = {"crewMember": member["loginNid"], "timeInterval": interval}
    data = await call_leon(name, arguments)
    records(data)  # Reject malformed/error responses before formatting.
    return {
        "source": "Leon MCP",
        "crew": member,
        "start": start_date,
        "end": end_date,
        "mode": mode,
        "data": data,
    }
