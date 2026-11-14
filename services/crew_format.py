"""Display assigned crew names/positions without contact or document data."""


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
            contact = member.get("contact") or {}
            name = (
                contact.get("knownAs")
                or " ".join(
                    str(contact.get(k) or "") for k in ("name", "surname")
                ).strip()
            )
            name = name or member.get("name") or member.get("knownAs")
            if not name:
                lines.append("Назначение получено, но формат имени не распознан.")
            else:
                position = member.get("position") or member.get("primaryPosition") or {}
                position = (
                    position.get("name") if isinstance(position, dict) else position
                )
                lines.append(f"{position or 'позиция не указана'}: {name}")
            count += 1
    if not count:
        lines.append(
            "нет данных" if compact else "Leon не предоставил назначенный экипаж."
        )
    return "\n".join(lines)
