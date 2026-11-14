def format_schedule(result: dict) -> str:
    day = result.get("date", "—")
    end_day = result.get("end_date", day)
    if end_day != day:
        day = f"{day} — {end_day}"
    flights = result.get("flights") or []

    lines = [
        f"Расписание OPS Bot на {day} (UTC)",
        f"Рейсов: {result.get('count', len(flights))}",
        "Источник: Leon MCP",
        "",
    ]

    if not flights:
        lines.append("Рейсов не найдено.")

    for flight in flights:
        number = flight.get("flightNumber") or "—"
        departure = flight.get("departure") or "—"
        arrival = flight.get("arrival") or "—"
        aircraft = flight.get("aircraft") or "—"
        departure_time = flight.get("departureUTC") or "—"
        arrival_time = flight.get("arrivalUTC") or "—"
        status = flight.get("status") or "—"
        nid = flight.get("flightNid") or "—"

        lines.extend(
            [
                f"{number}: {departure} → {arrival}",
                f"Борт: {aircraft}",
                f"Вылет UTC: {departure_time}",
                f"Прилёт UTC: {arrival_time}",
                f"Статус: {status}",
                f"Flight NID: {nid}",
                "",
            ]
        )

    if not result.get("complete", True):
        lines.append("Внимание: расписание загружено не полностью.")

    return "\n".join(lines).strip()
