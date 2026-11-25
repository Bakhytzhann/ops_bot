import re
from datetime import UTC
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from services.ai_analysis import request_analysis
from services.charter_input import airport_timezone
from services.snapshot_models import utc

BriefField = Literal["Flight", "Aircraft", "Passengers", "Price", "Wi-Fi", "Validity"]
BRIEF_FIELDS = {"Flight", "Aircraft", "Passengers", "Price", "Wi-Fi", "Validity"}
INTERNAL_DETAILS = re.compile(
    r"\b(?:floor|margin|ferry|cost|себестоимость|марж\w*|перегон\w*)\b", re.I
)
WIFI = re.compile(r"\b(?:wi[ -]?fi|вай[ -]?фай)\b", re.I)
HOURS = re.compile(r"(?<!\d)(\d{1,3})\s*(?:hours?|h|час(?:а|ов)?|ч)\b", re.I)


def _stamp(value, airport, local):
    if value is None or (stamp := utc(value)) is None:
        return ""
    zone = airport_timezone(airport) if local else UTC
    return stamp.astimezone(zone).strftime(
        "%d %b %Y %H:%M " + ("LT" if local else "UTC")
    )


def _flight_line(leg, *, local):
    departure, arrival = leg["departure"], leg["arrival"]
    start = _stamp(leg.get("departure_utc"), departure, local)
    end = _stamp(leg.get("arrival_utc"), arrival, local)
    stops = [stop["airport"] for stop in leg.get("techstops", [])]
    duration = leg.get("elapsed_minutes") if stops else leg.get("block_minutes")
    minutes = round(duration) if duration is not None else None
    duration_label = "elapsed" if stops else "block"
    times = " → ".join(part for part in (start, end) if part)
    if not times and leg.get("window_start_utc"):
        window_start = _stamp(leg["window_start_utc"], departure, local)
        window_end = _stamp(leg.get("window_end_utc"), departure, local)
        times = "Departure window " + " – ".join(
            part for part in (window_start, window_end) if part
        )
    return " · ".join(
        item
        for item in (
            f"{departure} → {arrival}",
            "Technical stop " + ", ".join(stops) if stops else "",
            times,
            f"Est. {duration_label} {minutes // 60} h {minutes % 60:02} min"
            if minutes is not None
            else "",
        )
        if item
    )


def format_offer_brief(row, passengers, *, local):
    lines = [f"Flight: {_flight_line(leg, local=local)}" for leg in row["legs"]]
    if (aircraft_type := row.get("aircraft_type")) and aircraft_type != "—":
        lines.append(f"Aircraft: {aircraft_type}")
    lines.append(f"Passengers: {passengers}")
    economics = row.get("economics") or {}
    if economics.get("target") is not None and economics.get("currency"):
        lines.append(
            f"Price: {economics['currency']} {economics['target']:,.2f} indicative"
        )
    return "\n".join(lines)


def format_ops_brief(row, passengers):
    lines = [f"CHARTER CHECK · {row['registration']} · {passengers} PAX"]
    if station := row.get("position", {}).get("planned_station"):
        lines.append(f"Planned station: {station}")
    for label, legs in (("FERRY", row["ferry"]), ("PAX", row["legs"])):
        for leg in legs:
            info = [f"{label} · {_flight_line(leg, local=False)}"]
            if route := leg.get("route"):
                info.append(route)
            if leg.get("fuel_kg") is not None:
                info.append(f"Est. burn {leg['fuel_kg']} kg ({leg['fuel_basis']})")
            lines.append(" · ".join(info))
            lines.extend(
                issue for issue in leg.get("issues", []) if isinstance(issue, str)
            )
    for section in ("schedule", "crew"):
        lines.extend(
            issue
            for issue in row.get(section, {}).get("issues", [])
            if isinstance(issue, str)
        )
    for label, key in (("Working crew", "working"), ("Relief crew", "replacements")):
        for scenario in row.get("crew", {}).get(key, []):
            summary = [label, scenario["name"], scenario["status"].replace("_", " ")]
            if station := scenario.get("handover_station"):
                transfer = _stamp(scenario.get("handover_utc"), station, False)
                summary.append(f"Handover {station} {transfer}".strip())
            if scenario.get("remaining_minutes") is not None:
                summary.append(f"Duty remaining {scenario['remaining_minutes']} min")
            lines.append(" · ".join(summary))
            lines.extend(scenario.get("issues", []))
    for port in row.get("airports", []):
        lines.extend(
            issue for issue in port.get("issues", []) if isinstance(issue, str)
        )
    return "\n".join(dict.fromkeys(lines))


class BriefEdits(BaseModel):
    model_config = ConfigDict(extra="forbid")
    remove: list[BriefField] = Field(default_factory=list, max_length=6)
    wifi: bool = False
    valid_for_hours: int | None = Field(default=None, ge=1, le=720)


BRIEF_PROMPT = """Extract the requested edits to the customer charter Offer Brief.
The result is an edit plan, not prose. Put only the field names explicitly requested
for deletion in remove (Russian «убери цену» means Price). wifi is true only when
asked to add Wi-Fi. valid_for_hours is set only when the instruction explicitly
specifies how many hours the offer is valid for. Leave fields untouched otherwise.
Internal rates, cost, margin, ferries and technical details are not customer fields.
"""


def _brief_fields(current_brief):
    parts = []
    for line in current_brief.strip().splitlines():
        key, _, value = line.partition(": ")
        if (
            key not in BRIEF_FIELDS
            or not value.strip()
            or INTERNAL_DETAILS.search(value)
        ):
            raise ValueError("Use the generated customer Offer Brief for editing.")
        parts.append((key, value.strip()))
    return parts


async def refine_offer_brief(current_brief, instruction, actor):
    fields = _brief_fields(current_brief)
    edits = await request_analysis(
        {
            "present_fields": sorted({key for key, _ in fields}),
            "instruction": instruction,
        },
        ("charter", "brief", actor),
        system_prompt=BRIEF_PROMPT,
        response_model=BriefEdits,
    )
    removed = set(edits.remove)
    hours = {int(match) for match in HOURS.findall(instruction)}
    wifi = edits.wifi and WIFI.search(instruction) is not None
    valid_hours = edits.valid_for_hours if edits.valid_for_hours in hours else None
    if wifi:
        removed.add("Wi-Fi")
    if valid_hours:
        removed.add("Validity")
    updated = [(key, value) for key, value in fields if key not in removed]
    if wifi:
        updated.append(("Wi-Fi", "Available"))
    if valid_hours:
        updated.append(("Validity", f"{valid_hours} h"))
    if not updated:
        raise ValueError("Keep at least one item in the Offer Brief.")
    return "\n".join(f"{key}: {value}" for key, value in updated)
