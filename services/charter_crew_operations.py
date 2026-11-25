"""Load the operator's crew and aircraft availability export.

CHARTER_CREW_OPERATIONS_PATH is a JSON path on the server. The top-level
source and timestamp describe the export, and the UTC coverage bounds identify
the period with published roster and aircraft status. A crew's windows combine
its confirmed station, availability, and applicable operator limits for that
window. Populate existing report clocks and sectors used for a crew already
on duty. Supply min_rest_minutes for the applicable release-to-report interval
after the duty, allowing later windows to be compared against a proposed break.

Positioning entries are scheduled crossings available to that crew. ready_utc
includes the transfer before reporting at the replacement station. The counted
duty and FDP values must be derived from the operator's applicable rules. AOG
and maintenance blocks represent published intervals by tail and station.
"""

from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, Field, model_validator

from services.snapshot_models import utc

Station = Annotated[str, Field(pattern=r"^[A-Z0-9]{4}$")]
Timestamp = AwareDatetime


class CrewMember(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    role: str = Field(min_length=1, max_length=40)
    contact: str | None = Field(default=None, max_length=120)


class DutyWindow(BaseModel):
    station: Station
    available_from_utc: Timestamp
    available_until_utc: Timestamp
    report_lead_minutes: int = Field(ge=0, le=720)
    post_duty_minutes: int = Field(ge=0, le=720)
    max_duty_minutes: int = Field(gt=0, le=1440)
    max_fdp_minutes: int = Field(gt=0, le=1440)
    max_sectors: int = Field(gt=0, le=40)
    min_rest_minutes: int | None = Field(default=None, gt=0, le=14400)
    duty_started_utc: Timestamp | None = None
    fdp_started_utc: Timestamp | None = None
    sectors_used: int = Field(default=0, ge=0, le=40)

    @model_validator(mode="after")
    def validate_window(self):
        if self.available_until_utc <= self.available_from_utc:
            raise ValueError("Crew availability window end must follow start")
        if self.max_fdp_minutes > self.max_duty_minutes:
            raise ValueError("Crew FDP limit must be within the duty limit")
        if any(
            start is not None and start > self.available_from_utc
            for start in (self.duty_started_utc, self.fdp_started_utc)
        ):
            raise ValueError("Actual report times must precede window availability")
        return self


class PositioningFlight(BaseModel):
    departure: Station
    arrival: Station
    departure_utc: Timestamp
    arrival_utc: Timestamp
    ready_utc: Timestamp
    flight_number: str = Field(min_length=1, max_length=24)
    counted_duty_minutes: int = Field(ge=0, le=1440)
    counted_fdp_minutes: int = Field(ge=0, le=1440)

    @model_validator(mode="after")
    def validate_flight(self):
        if self.departure == self.arrival or not (
            self.departure_utc < self.arrival_utc <= self.ready_utc
        ):
            raise ValueError("Crew positioning times or route are inconsistent")
        if self.counted_fdp_minutes > self.counted_duty_minutes:
            raise ValueError("Positioning FDP credit must fit within duty credit")
        return self


class RosterCrew(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    contact: str | None = Field(default=None, max_length=120)
    members: list[CrewMember] = Field(default_factory=list, max_length=30)
    aircraft_types: list[Annotated[str, Field(pattern=r"^[A-Z0-9]{2,8}$")]] = Field(
        min_length=1, max_length=20
    )
    assigned_registration: str | None = Field(
        default=None, pattern=r"^[A-Z0-9-]{1,12}$"
    )
    windows: list[DutyWindow] = Field(max_length=100)
    positioning: list[PositioningFlight] = Field(default_factory=list, max_length=100)


class AircraftBlock(BaseModel):
    registration: str = Field(pattern=r"^[A-Z0-9-]{1,12}$")
    kind: Literal["AOG", "MAINTENANCE"]
    start_utc: Timestamp
    end_utc: Timestamp
    station: Station | None = None
    reason: str = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def validate_block(self):
        if self.end_utc <= self.start_utc:
            raise ValueError("Aircraft block end must follow start")
        return self


class CrewOperations(BaseModel):
    source: str = Field(min_length=1, max_length=100)
    updated_at: Timestamp
    coverage_start_utc: Timestamp
    coverage_end_utc: Timestamp
    crews: list[RosterCrew] = Field(max_length=500)
    aircraft_blocks: list[AircraftBlock] = Field(default_factory=list, max_length=3000)

    @model_validator(mode="after")
    def validate_coverage(self):
        if self.coverage_end_utc <= self.coverage_start_utc:
            raise ValueError("Crew feed coverage end must follow start")
        return self


def load_crew_operations(path: str | Path | None) -> CrewOperations | None:
    if not path:
        return None
    with Path(path).open("rb") as source:
        payload = source.read(2 * 1024 * 1024 + 1)
    if len(payload) > 2 * 1024 * 1024:
        raise ValueError("Crew operations feed exceeds 2 MiB")
    return CrewOperations.model_validate_json(payload)


def _crew_deadlines(window, report, positioning):
    duty_credit = positioning.counted_duty_minutes if positioning else 0
    fdp_credit = positioning.counted_fdp_minutes if positioning else 0
    # Actual starts already cover continuous duty. Use the stricter counted
    # positioning result when the two records each impose a bound.
    duty = min(
        (window.duty_started_utc or report)
        + timedelta(minutes=window.max_duty_minutes),
        report + timedelta(minutes=window.max_duty_minutes - duty_credit),
        window.available_until_utc,
    )
    fdp = min(
        (window.fdp_started_utc or report) + timedelta(minutes=window.max_fdp_minutes),
        report + timedelta(minutes=window.max_fdp_minutes - fdp_credit),
    )
    return duty, fdp


def _crew_fit(window, segments, index, positioning=None):
    departure = utc(segments[index]["departure_utc"])
    report = departure - timedelta(minutes=window.report_lead_minutes)
    ready = (
        max(window.available_from_utc, positioning.ready_utc)
        if positioning
        else window.available_from_utc
    )
    if report < ready or report >= window.available_until_utc:
        return None
    duty_end, fdp_end = _crew_deadlines(window, report, positioning)
    for number, segment in enumerate(segments[index:], start=index):
        arrival = utc(segment["arrival_utc"])
        release = arrival + timedelta(minutes=window.post_duty_minutes)
        reason = None
        if number - index + 1 + window.sectors_used > window.max_sectors:
            reason = "Sector limit"
        elif arrival > fdp_end:
            reason = "FDP limit"
        elif release > duty_end:
            reason = "Duty limit or next roster commitment"
        if reason:
            return report, duty_end, fdp_end, number, arrival, reason
    return report, duty_end, fdp_end, len(segments), arrival, None


def _scenario(person, window, segments, index, fit, positioning):
    report, duty_end, fdp_end, limit, _, reason = fit
    last_fit = utc(segments[limit - 1]["arrival_utc"]) if limit > index else report
    availability = (
        max(window.available_from_utc, positioning.ready_utc)
        if positioning
        else window.available_from_utc
    )
    handover = max(report, utc(segments[index - 1]["arrival_utc"])) if index else report
    return {
        "name": person.name,
        "contact": person.contact,
        "members": [member.model_dump() for member in person.members],
        "status": "fits" if reason is None else "limited",
        "source": "roster",
        "report_utc": report.isoformat(),
        "available_from_utc": availability.isoformat(),
        "duty_end_utc": duty_end.isoformat(),
        "fdp_end_utc": fdp_end.isoformat(),
        "duty_at_report_minutes": max(
            0, int((duty_end - report).total_seconds() // 60)
        ),
        "fdp_at_report_minutes": max(0, int((fdp_end - report).total_seconds() // 60)),
        "remaining_minutes": max(
            0,
            int((duty_end - last_fit).total_seconds() // 60)
            - (window.post_duty_minutes if limit > index else 0),
        ),
        "first_limited_segment": limit + 1 if reason else None,
        "handover_station": segments[index]["departure"],
        "handover_utc": handover.isoformat(),
        "positioning": positioning.model_dump(mode="json") if positioning else None,
        "rest_breaks": [],
        "issues": [
            f"{reason} at {segments[limit]['departure']}–{segments[limit]['arrival']}."
        ]
        if reason
        else [],
    }


def _positioning_options(person, window, station, now):
    if window.station == station:
        return [None]
    return [
        flight
        for flight in person.positioning
        if flight.departure == window.station
        and flight.arrival == station
        and max(now, window.available_from_utc) <= flight.departure_utc
        and flight.ready_utc < window.available_until_utc
    ]


def _replacement_scenario(person, window, segments, index, positioning):
    if fit := _crew_fit(window, segments, index, positioning):
        return _scenario(person, window, segments, index, fit, positioning)
    ready = (
        max(window.available_from_utc, positioning.ready_utc)
        if positioning
        else window.available_from_utc
    )
    departure = utc(segments[index]["departure_utc"])
    earliest_departure = ready + timedelta(minutes=window.report_lead_minutes)
    duty_end, fdp_end = _crew_deadlines(window, ready, positioning)
    if earliest_departure <= departure or earliest_departure >= min(duty_end, fdp_end):
        return None
    adjusted = _shift_to_report(segments, index, earliest_departure)
    adjusted_fit = _crew_fit(window, adjusted, index, positioning) if adjusted else None
    if adjusted_fit and adjusted_fit[5] is None:
        result = _scenario(person, window, adjusted, index, adjusted_fit, positioning)
        return {
            **result,
            "status": "projected_relief_later",
            "earliest_continuation_utc": earliest_departure.isoformat(),
            "issues": [
                "Recalculate route, SKED and airport hours at the later departure inside the requested window."
            ],
        }
    return {
        "name": person.name,
        "contact": person.contact,
        "members": [member.model_dump() for member in person.members],
        "status": "available_later",
        "available_from_utc": ready.isoformat(),
        "duty_end_utc": duty_end.isoformat(),
        "fdp_end_utc": fdp_end.isoformat(),
        "duty_at_report_minutes": max(0, int((duty_end - ready).total_seconds() // 60)),
        "fdp_at_report_minutes": max(0, int((fdp_end - ready).total_seconds() // 60)),
        "earliest_continuation_utc": earliest_departure.isoformat(),
        "handover_station": segments[index]["departure"],
        "positioning": positioning.model_dump(mode="json") if positioning else None,
        "issues": ["This crew can report later than the computed departure."],
    }


def _shift_to_report(segments, split, earliest_departure):
    """Shift whole flexible request legs, preserving all explicit leg starts."""
    shifted = segments[:split]
    available = earliest_departure
    for sector in segments[split:]:
        out = utc(sector["departure_utc"])
        arrival = utc(sector["arrival_utc"])
        if out < available:
            stop = utc(sector.get("window_end_utc"))
            if sector.get("time_mode") != "WINDOW" or not stop or available >= stop:
                return None
            arrival += available - out
            out = available
        shifted.append(
            {
                **sector,
                "departure_utc": out.isoformat(),
                "arrival_utc": arrival.isoformat(),
            }
        )
        # Preserve the original turnaround interval between consecutive sectors.
        index = len(shifted)
        if index < len(segments):
            gap = utc(segments[index]["departure_utc"]) - utc(sector["arrival_utc"])
            available = (
                arrival + gap
                if sector.get("request_leg_index")
                == segments[index].get("request_leg_index")
                else arrival
            )
    return shifted


def _working_scenario(person, window, segments, start=0):
    if window.station != segments[start]["departure"]:
        return None
    fit = _crew_fit(window, segments, start)
    if fit is None:
        return None
    scenario = _scenario(person, window, segments, start, fit, None)
    initial = scenario
    _, _, _, split, _, reason = fit
    used_windows = set()
    projected = False
    while reason and split > start:
        release = utc(segments[split - 1]["arrival_utc"]) + timedelta(
            minutes=window.post_duty_minutes
        )
        used_windows.add(id(window))
        if window.min_rest_minutes is None:
            initial["issues"].append(
                "Load the applicable minimum rest before recalculating the same crew after release."
            )
            break
        rest_complete = release + timedelta(minutes=window.min_rest_minutes)
        candidates = []
        earliest = None
        for later in person.windows:
            if (
                id(later) in used_windows
                or later.station != segments[split]["departure"]
            ):
                continue
            ready = max(rest_complete, later.available_from_utc)
            if ready >= later.available_until_utc:
                continue
            departure = ready + timedelta(minutes=later.report_lead_minutes)
            earliest = min(earliest, departure) if earliest else departure
            adjusted = _shift_to_report(segments, split, departure)
            if (
                adjusted
                and (later_fit := _crew_fit(later, adjusted, split))
                and later_fit[3] > split
            ):
                candidates.append((later, later_fit, adjusted, ready))
        if not candidates:
            initial["earliest_continuation_utc"] = (
                earliest.isoformat() if earliest else None
            )
            break
        window, fit, adjusted, ready = max(candidates, key=lambda entry: entry[1][3])
        projected |= adjusted != segments
        segments = adjusted
        scenario = _scenario(person, window, segments, split, fit, None)
        initial["rest_breaks"].append(
            {
                "station": window.station,
                "previous_release_utc": release.isoformat(),
                "available_from_utc": ready.isoformat(),
                "next_departure_utc": segments[split]["departure_utc"],
            }
        )
        start, split, reason = split, fit[3], fit[5]
    if initial["rest_breaks"]:
        initial.update(
            status=("projected_after_rest" if projected else "fits_after_rest")
            if reason is None
            else "limited",
            duty_end_utc=scenario["duty_end_utc"],
            fdp_end_utc=scenario["fdp_end_utc"],
            remaining_minutes=scenario["remaining_minutes"],
            first_limited_segment=scenario["first_limited_segment"],
            issues=[
                *scenario["issues"],
                *(
                    [
                        "Recalculate route, SKED and airport hours at the later departure inside the requested window."
                    ]
                    if projected
                    else []
                ),
            ],
        )
    return initial


def roster_crew_report(evidence, aircraft, aircraft_type, segments, now: datetime):
    if evidence is None:
        return None
    empty = {"status": "partial", "working": [], "replacements": [], "issues": []}
    if (
        not segments
        or any(
            not (start := utc(leg["departure_utc"]))
            or not (end := utc(leg["arrival_utc"]))
            or end <= start
            or leg.get("performance_status") in {"range_limit", "blocked"}
            for leg in segments
        )
        or any(
            left["arrival"] != right["departure"]
            or utc(left["arrival_utc"]) > utc(right["departure_utc"])
            for left, right in zip(segments, segments[1:])
        )
    ):
        return {
            **empty,
            "issues": [
                "Calculate every positioning and passenger sector before checking crew duty."
            ],
        }
    start, end = utc(segments[0]["departure_utc"]), utc(segments[-1]["arrival_utc"])
    if (
        not evidence.coverage_start_utc <= start <= end <= evidence.coverage_end_utc
        or evidence.updated_at > now
    ):
        return {
            **empty,
            "issues": ["Extend the published crew roster to cover the full itinerary."],
        }

    working, replacement = [], []
    assigned = sorted(
        (person for person in evidence.crews if aircraft_type in person.aircraft_types),
        key=lambda person: person.assigned_registration != aircraft,
    )
    for person in assigned:
        if person.assigned_registration == aircraft:
            matches = [
                scenario
                for window in person.windows
                if (scenario := _working_scenario(person, window, segments)) is not None
            ]
            if matches:
                working.extend(matches)
            else:
                following = [
                    window.available_from_utc
                    + timedelta(minutes=window.report_lead_minutes)
                    for window in person.windows
                    if window.station == segments[0]["departure"]
                    and window.available_from_utc
                    + timedelta(minutes=window.report_lead_minutes)
                    > start
                ]
                working.append(
                    {
                        "name": person.name,
                        "contact": person.contact,
                        "members": [member.model_dump() for member in person.members],
                        "status": "limited",
                        "available_from_utc": min(following).isoformat()
                        if following
                        else None,
                        "issues": [
                            "No published working duty window covers the requested report time at this station."
                        ],
                    }
                )
            continue
        for index, leg in enumerate(segments):
            if index and not any(
                scenario["status"] in {"fits", "fits_after_rest"}
                or (scenario.get("first_limited_segment") or 1) > index
                for scenario in working
            ):
                continue
            for window in person.windows:
                for positioning in _positioning_options(
                    person, window, leg["departure"], now
                ):
                    if scenario := _replacement_scenario(
                        person, window, segments, index, positioning
                    ):
                        replacement.append(scenario)
    return {
        "status": "roster",
        "source": evidence.source,
        "as_of_utc": evidence.updated_at.isoformat(),
        "working": working,
        "replacements": replacement,
        "issues": []
        if working
        else ["The published roster lists no working crew for this aircraft."],
    }
