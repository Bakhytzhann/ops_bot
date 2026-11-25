from httpx import HTTPError

from integrations.leon.operations import records
from integrations.leon.tools import get_flight_crew
from services.flight_duty import assigned_members, load_duty
from services.snapshot_models import utc


def _rest_metric(metrics, name):
    metric = next((metric for metric in metrics if metric["name"] == name), None)
    if not metric or (metric["actual"] is None and metric["limit"] is None):
        return None
    return {"actual": metric["actual"], "limit": metric["limit"]}


def _crew_readout(member, duty):
    position = member.get("position") or member.get("primaryPosition")
    if isinstance(position, dict):
        position = position.get("name")
    role = position.strip() if isinstance(position, str) else ""
    duty = duty or {}
    metrics = duty.get("metrics", [])
    return {
        "role": role or "Crew",
        "fdp_remaining": duty.get("fdp_remaining"),
        "rest_before": _rest_metric(metrics, "Rest Before"),
        "rest_after": _rest_metric(metrics, "Rest After"),
        "next_eobt": duty.get("next_eobt"),
        "violations": duty.get("violations", []),
    }


def crew_flight(flight: dict | None) -> dict | None:
    """Build the scheduled flight context for the Leon FTL readout."""
    if not flight or flight.get("cancelled") is True:
        return None
    flight_id = flight.get("flightNid")
    departure = utc(flight.get("departureUTC"))
    arrival = utc(flight.get("arrivalUTC"))
    if (
        type(flight_id) is not int
        or flight_id <= 0
        or not departure
        or not arrival
        or arrival <= departure
    ):
        return None

    return {
        "id": flight_id,
        "number": flight.get("flightNumber"),
        "departure": flight.get("departure"),
        "arrival": flight.get("arrival"),
        "departure_utc": departure,
        "arrival_utc": arrival,
    }


async def charter_crew(flight: dict | None) -> dict:
    """Read Leon FTL for the crew assigned to the matched scheduled flight."""
    context = crew_flight(flight)
    result = {"status": "confirmation_required", "flight": context, "members": []}
    if context is None:
        return result
    flight_id = context["id"]
    try:
        crew = records((await get_flight_crew(flight_id)).get("crew"))
        members = [
            member
            for member in assigned_members(crew, flight_id)
            if isinstance(member, dict)
        ]
        if not members:
            return result
        duty = await load_duty(
            flight,
            {"flightNid": flight_id, "crewMemberList": members},
            include_next_duty=False,
        )
    except (HTTPError, OSError, RuntimeError, ValueError, TypeError):
        return {**result, "status": "unavailable"}

    assignments = duty.details.get("assignments", [])
    result["members"] = [
        _crew_readout(member, assignments[index] if index < len(assignments) else None)
        for index, member in enumerate(members)
    ]
    result["status"] = (
        "available"
        if any(
            member["fdp_remaining"] is not None
            or member["rest_before"] is not None
            or member["rest_after"] is not None
            or member["next_eobt"] is not None
            or member["violations"]
            for member in result["members"]
        )
        else "unavailable"
    )
    return result
