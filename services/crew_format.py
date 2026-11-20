"""Display assigned crew names/positions without contact or document data."""


def crew_identity(member):
    contact = member.get("contact") or {}
    name = (
        contact.get("knownAs")
        or " ".join(str(contact.get(k) or "") for k in ("name", "surname")).strip()
        or member.get("name")
        or member.get("knownAs")
    )
    position = member.get("position") or member.get("primaryPosition")
    if isinstance(position, dict):
        position = position.get("name")
    return name, position


def format_flight_crew(number, day, data, *, compact=False, flight_nid=None):
    rows = data if isinstance(data, list) else [data]
    lines = [] if compact else [f"Экипаж {number} на {day}", "Источник: Leon MCP"]
    count = 0
    for flight in rows:
        if not isinstance(flight, dict):
            continue
        if flight_nid is not None and flight.get("flightNid") != flight_nid:
            continue
        for member in flight.get("crewMemberList") or []:
            name, position = crew_identity(member)
            if not name:
                lines.append("Назначение получено, но формат имени не распознан.")
            else:
                lines.append(f"{position or 'позиция не указана'}: {name}")
            count += 1
    if not count:
        lines.append(
            "нет данных" if compact else "Leon не предоставил назначенный экипаж."
        )
    return "\n".join(lines)
