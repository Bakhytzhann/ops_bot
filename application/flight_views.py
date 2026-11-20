"""Read models for SKED, Flight Card and Preparation."""

import asyncio
import logging
import math
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

from application import operations
from config.settings import get_settings
from integrations.leon.tools import get_flight_crew
from services.crew_format import crew_identity
from services.flight_duty import assigned_members, load_duty
from services.flight_foreflight import load_foreflight
from services.flight_foreflight_fast import load_fast_foreflight
from services.flight_handlers import load_flight_handlers
from services.flight_weather import grouped_reports
from services.notam_filters import notam_sets
from services.snapshot_models import FlightSnapshot, SourceBlock, utc
from services.source_cache import SourceCache

logger = logging.getLogger(__name__)


class FlightNotFound(Exception):
    pass


class ScheduleUnavailable(Exception):
    pass


class FlightLeg(BaseModel):
    id: int
    number: str | None
    departure: str | None
    arrival: str | None
    std: datetime
    sta: datetime | None
    aircraft: str | None
    aircraft_type: str | None
    cancelled: bool
    ferry: bool


class Schedule(BaseModel):
    date: date
    complete: bool
    flights: list[FlightLeg]


class Handler(BaseModel):
    station: str
    name: str
    phone: str | None
    email: str | None


class FlightCard(BaseModel):
    flight: FlightLeg
    ete_minutes: int | None = None
    route: str | None = None
    alternate: str | None = None
    plan_status: Literal["available", "partial", "unavailable"] = "unavailable"
    handling: list[Handler] = Field(default_factory=list)


class DutyMetric(BaseModel):
    name: str
    actual: str | None
    limit: str | None
    offence: bool


class DutyAssignment(BaseModel):
    start: datetime | None = None
    end: datetime | None = None
    reporting: bool = False
    metrics: list[DutyMetric] = Field(default_factory=list)


class CrewMember(BaseModel):
    name: str | None
    position: str | None
    duty: DutyAssignment | None


class CrewPreparation(BaseModel):
    available: bool
    members: list[CrewMember] = Field(default_factory=list)


class WeatherReport(BaseModel):
    product: Literal["METAR", "TAF"]
    station: str
    raw: str
    sources: list[str]


class WeatherPreparation(BaseModel):
    partial: bool
    fetched_at: datetime | None
    reports: list[WeatherReport]


class FlightNotam(BaseModel):
    id: str
    station: str
    status: Literal["affects", "review"]
    raw: str


class NotamPreparation(BaseModel):
    stations: list[str] = Field(default_factory=list)
    recognized: list[str] = Field(default_factory=list)
    notams: list[FlightNotam] = Field(default_factory=list)
    briefing_url: str | None = None
    briefing_time: datetime | None = None
    departure_offset_minutes: float | None = None


def _text(value):
    return value.strip() or None if isinstance(value, str) else None


def _leg(row, departure):
    return FlightLeg(
        id=row["flightNid"],
        number=_text(row.get("flightNumber")),
        departure=_text(row.get("departure")),
        arrival=_text(row.get("arrival")),
        std=departure,
        sta=utc(row.get("arrivalUTC")),
        aircraft=_text(row.get("aircraft")),
        aircraft_type=_text(row.get("aircraftType")),
        cancelled=row.get("cancelled") is True,
        ferry=row.get("ferry") is True,
    )


def _version(row):
    return tuple(
        str(row.get(key))
        for key in (
            "flightNid",
            "flightNumber",
            "departureUTC",
            "arrivalUTC",
            "departure",
            "arrival",
            "aircraft",
        )
    )


class FlightViews:
    def __init__(self):
        self.cache = SourceCache()

    async def _read(self, key, ttl, loader):
        async def load():
            return await asyncio.wait_for(loader(), get_settings().snapshot_timeout)

        return await self.cache.get(key, ttl, load)

    async def _optional(self, source, key, ttl, loader, default=None):
        try:
            return await self._read(key, ttl, loader)
        except Exception as exc:  # noqa: BLE001 -- upstream boundary
            logger.warning("%s unavailable (%s)", source, type(exc).__name__)
            return default

    async def _schedule(self, day):
        response = await self._optional(
            "Leon schedule",
            ("schedule", day),
            get_settings().snapshot_schedule_ttl,
            lambda: operations.flight_schedule(day.isoformat()),
        )
        if response is None:
            raise ScheduleUnavailable
        complete = response["complete"]
        flights = {}
        duplicates = set()
        for row in response["flights"]:
            departure = utc(row.get("departureUTC"))
            if departure and departure.date() != day:
                continue
            nid = row.get("flightNid")
            if type(nid) is not int or nid <= 0 or not departure:
                complete = False
                continue
            if nid in flights:
                if row != flights[nid][1]:
                    duplicates.add(nid)
                    complete = False
                continue
            flights[nid] = (_leg(row, departure), row)
        for nid in duplicates:
            flights.pop(nid)
        return flights, complete

    async def schedule(self, day):
        flights, complete = await self._schedule(day)
        ordered = sorted((leg for leg, _ in flights.values()), key=lambda leg: leg.std)
        return Schedule(date=day, complete=complete, flights=ordered)

    async def _select(self, day, flight_id):
        flights, complete = await self._schedule(day)
        if flight_id in flights:
            return flights[flight_id]
        if not complete:
            raise ScheduleUnavailable
        raise FlightNotFound

    async def card(self, day, flight_id):
        flight, row = await self._select(day, flight_id)
        settings = get_settings()
        plan, handling = await asyncio.gather(
            self._optional(
                "ForeFlight plan",
                ("plan", *_version(row)),
                settings.snapshot_briefing_ttl,
                lambda: load_fast_foreflight(row),
                {},
            ),
            self._optional(
                "Leon handling",
                ("handling", *_version(row)),
                settings.snapshot_crew_ttl,
                lambda: load_flight_handlers(row),
            ),
        )
        route = plan.get("route", SourceBlock("ForeFlight"))
        time = plan.get("flighttime", SourceBlock("ForeFlight"))
        minutes = time.details.get("eet_minutes")
        ete = (
            int(minutes)
            if type(minutes) in (int, float) and math.isfinite(minutes) and minutes >= 0
            else None
        )
        text = _text(route.details.get("route"))
        alternate = _text(route.details.get("alternate"))
        status = "unavailable"
        if text or alternate or ete is not None:
            status = (
                "available"
                if text and ete is not None and route.status == time.status == "ready"
                else "partial"
            )
        return FlightCard(
            flight=flight,
            ete_minutes=ete,
            route=text,
            alternate=alternate,
            plan_status=status,
            handling=[
                Handler(
                    station=info["station"],
                    name=info["name"],
                    phone=_text(info.get("phone")),
                    email=_text(info.get("email")),
                )
                for info in (handling.details.values() if handling else [])
                if info and info.get("name")
            ],
        )

    async def crew(self, day, flight_id):
        _, row = await self._select(day, flight_id)
        settings = get_settings()
        crew = await self._optional(
            "Leon crew",
            ("crew", *_version(row)),
            settings.snapshot_crew_ttl,
            lambda: get_flight_crew(flight_id),
        )
        if crew is None:
            return CrewPreparation(available=False)
        assigned = assigned_members(crew["crew"], flight_id)
        duty = (
            await self._optional(
                "Leon duty",
                ("duty", *_version(row), repr(crew["crew"])),
                settings.snapshot_ftl_ttl,
                lambda: load_duty(row, crew["crew"]),
            )
            if assigned
            else None
        )
        assignments = duty.details.get("assignments", []) if duty else []
        members = []
        for index, member in enumerate(assigned):
            name, position = crew_identity(member)
            assignment = assignments[index] if index < len(assignments) else None
            members.append(
                CrewMember(
                    name=_text(name),
                    position=_text(position),
                    duty=DutyAssignment(**assignment) if assignment else None,
                )
            )
        return CrewPreparation(available=True, members=members)

    async def weather(self, day, flight_id):
        flight, row = await self._select(day, flight_id)
        data = await self._optional(
            "Flight weather",
            ("weather", *_version(row)),
            get_settings().snapshot_weather_ttl,
            lambda: operations.selected_weather(row, day.isoformat(), include_taf=True),
            {},
        )
        stations = dict.fromkeys(filter(None, (flight.departure, flight.arrival)))
        reports = [
            WeatherReport(
                station=station,
                product=product,
                raw=report["raw"],
                sources=report["sources"],
            )
            for station in stations
            for product in ("METAR", "TAF")
            for report in grouped_reports(data.get("reports", []), station, product)
        ]
        products = {(report.station, report.product) for report in reports}
        return WeatherPreparation(
            partial=not stations
            or len(products) != 2 * len(stations)
            or data.get("partial", False),
            fetched_at=utc(data.get("fetched_at")),
            reports=reports,
        )

    async def notams(self, day, flight_id):
        flight, row = await self._select(day, flight_id)
        stations = list(dict.fromkeys(filter(None, (flight.departure, flight.arrival))))
        blocks = await self._optional(
            "ForeFlight briefing",
            ("briefing", *_version(row)),
            get_settings().snapshot_briefing_ttl,
            lambda: load_foreflight(row),
            {},
        )
        snapshot = FlightSnapshot(row, blocks)
        recognized = []
        notams = []
        for station in stations:
            groups, found = notam_sets(snapshot, station)
            if found:
                recognized.append(station)
            for key, status in (("A", "affects"), ("R", "review")):
                notams.extend(
                    FlightNotam(
                        id=record.identifier,
                        station=station,
                        status=status,
                        raw=record.raw,
                    )
                    for _, record in groups[key]
                )
        briefing = blocks.get("briefing")
        return NotamPreparation(
            stations=stations,
            recognized=recognized,
            notams=notams,
            briefing_url=briefing.details.get("url") if briefing else None,
            briefing_time=utc(briefing.details.get("generated")) if briefing else None,
            departure_offset_minutes=briefing.details.get("departure_offset_minutes")
            if briefing
            else None,
        )
