import asyncio
import logging
import math
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from time import monotonic
from typing import Literal

from pydantic import BaseModel, Field

from application import operations
from config.settings import get_settings
from integrations.awc.client import get_weather
from integrations.leon.operations import get_flight_weather_leon
from integrations.leon.tools import get_flight_crew
from services.crew_format import crew_identity
from services.flight_display import flight_stamp
from services.flight_duty import assigned_members, load_duty
from services.flight_foreflight import load_foreflight
from services.flight_foreflight_fast import load_fast_foreflight
from services.flight_handlers import load_flight_handlers
from services.flight_summary import route_countries
from services.flight_weather import grouped_reports
from services.flight_weather_brief import metar_facts
from services.foreflight_operations import (
    ForeFlightClient,
    flights_for_day,
    match_flight,
)
from services.notam_filters import notam_sets
from services.snapshot_models import FlightSnapshot, SourceBlock, utc
from services.source_cache import SourceCache
from services.weather_reports import awc_reports, leon_reports

logger = logging.getLogger(__name__)
SELECTION_TTL = 300
REFRESH_INTERVAL = 5
Status = Literal["READY", "PARTIAL", "UNAVAILABLE", "STALE"]


class FlightNotFound(Exception):
    pass


class FlightChanged(Exception):
    pass


class ScheduleUnavailable(Exception):
    pass


class SourceState(BaseModel):
    status: Status = "UNAVAILABLE"
    fetched_at: datetime | None = None
    fresh_until: datetime | None = None


class FlightLeg(BaseModel):
    id: int
    version: str
    number: str | None
    departure: str | None
    arrival: str | None
    std: datetime
    sta: datetime | None
    departure_iata: str | None
    arrival_iata: str | None
    std_local: str | None
    sta_local: str | None
    aircraft: str | None
    aircraft_type: str | None
    cancelled: bool
    ferry: bool


class Schedule(SourceState):
    date: date
    end_date: date
    complete: bool
    flights: list[FlightLeg]


class Plan(SourceState):
    ete_minutes: int | None = None
    route: str | None = None
    alternate: str | None = None
    overflight: list[str | None] | None = None


class Handler(BaseModel):
    role: Literal["DEP", "ARR"]
    station: str
    name: str
    phone: str | None
    email: str | None


class Handling(SourceState):
    handlers: list[Handler] = Field(default_factory=list)


class FlightCard(Plan):
    flight: FlightLeg
    plan_status: Literal["available", "partial", "unavailable"]
    handling: list[Handler] = Field(default_factory=list)


class DutyMetric(BaseModel):
    name: str
    actual: str | None
    limit: str | None
    limit_type: str | None = None
    offence: bool


class DutyAssignment(BaseModel):
    start: datetime | None = None
    end: datetime | None = None
    reporting: bool = False
    duty_duration: str | None = None
    next_eobt: datetime | None = None
    fdp_remaining: str | None = None
    metrics: list[DutyMetric] = Field(default_factory=list)
    violations: list[str] = Field(default_factory=list)


class CrewMember(BaseModel):
    name: str | None
    position: str | None
    role: Literal["CPT", "FO"] | None
    hotel: str | None
    duty: DutyAssignment | None


class CrewPreparation(SourceState):
    available: bool
    members: list[CrewMember] = Field(default_factory=list)


class WeatherReport(BaseModel):
    product: Literal["METAR", "TAF"]
    station: str
    raw: str
    sources: list[str]
    issued_at: datetime | None = None
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    status: Status = "PARTIAL"
    fresh_until: datetime | None = None
    facts: dict[str, str] = Field(default_factory=dict)


class WeatherPreparation(SourceState):
    partial: bool
    stations: list[str]
    reports: list[WeatherReport]


class FlightNotam(BaseModel):
    id: str
    station: str
    status: Literal["affects", "review"]
    raw: str


class NotamPreparation(SourceState):
    stations: list[str] = Field(default_factory=list)
    recognized: list[str] = Field(default_factory=list)
    notams: list[FlightNotam] = Field(default_factory=list)
    briefing_url: str | None = None
    briefing_time: datetime | None = None
    departure_offset_minutes: float | None = None


@dataclass(frozen=True)
class _Read:
    data: object
    fetched_at: datetime | None
    stale: bool = False
    fresh_until: datetime | None = None

    def state(self, status: Status):
        return {
            "status": "STALE" if self.stale and status != "UNAVAILABLE" else status,
            "fetched_at": self.fetched_at,
            "fresh_until": self.fresh_until,
        }


def _text(value):
    return (value.strip() or None) if isinstance(value, str) else None


def _local(row, endpoint):
    stamp = flight_stamp(row, endpoint, {"time": "LT"})
    return stamp.isoformat() if stamp else None


def _version(row):
    return sha256(repr(sorted(row.items())).encode()).hexdigest()[:24]


def _leg(row, departure):
    return FlightLeg(
        id=row["flightNid"],
        version=_version(row),
        number=_text(row.get("flightNumber")),
        departure=_text(row.get("departure")),
        arrival=_text(row.get("arrival")),
        std=departure,
        sta=utc(row.get("arrivalUTC")),
        departure_iata=_text(row.get("departureIATA")),
        arrival_iata=_text(row.get("arrivalIATA")),
        std_local=_local(row, "departure"),
        sta_local=_local(row, "arrival"),
        aircraft=_text(row.get("aircraft")),
        aircraft_type=_text(row.get("aircraftType")),
        cancelled=row.get("cancelled") is True,
        ferry=row.get("ferry") is True,
    )


def _role(position):
    if not position:
        return None
    name = " ".join(position.upper().split())
    if name in {"CPT", "CAPTAIN", "PIC", "PILOT IN COMMAND"}:
        return "CPT"
    if name in {
        "FO",
        "F/O",
        "FIRST OFFICER",
        "SIC",
        "SECOND IN COMMAND",
        "CO-PILOT",
        "COPILOT",
    }:
        return "FO"
    return None


def _hotel(member, flight_id):
    hotel = member.get("hotel")
    return (
        _text(hotel.get("name"))
        if isinstance(hotel, dict) and hotel.get("flightNid") == flight_id
        else None
    )


def _report_status(report, product, now):
    issued = report.get("issued_at")
    if product == "TAF":
        start, end = report.get("valid_from"), report.get("valid_to")
        if end and end <= now:
            return "STALE"
        return "READY" if issued and start and end and start <= now < end else "PARTIAL"
    if not issued or issued > now + timedelta(minutes=10):
        return "PARTIAL"
    return (
        "STALE"
        if now - issued > timedelta(hours=get_settings().weather_stale_hours)
        else "READY"
    )


def _report_expiry(report, product):
    if product == "TAF":
        return report.get("valid_to")
    issued = report.get("issued_at")
    return (
        issued + timedelta(hours=get_settings().weather_stale_hours) if issued else None
    )


class FlightViews:
    def __init__(self):
        self.cache = SourceCache(256)
        self.slots = asyncio.Semaphore(5)

    async def _read(
        self, source, key, ttl, loader, *, refresh=False, stale=True, bounded=True
    ):
        prior = self.cache.peek(key)
        now = datetime.now(UTC)

        async def request():
            if not bounded:
                return await loader()
            async with self.slots:
                return await loader()

        async def load():
            try:
                value = await asyncio.wait_for(
                    request(), get_settings().snapshot_timeout
                )
                fetched = datetime.now(UTC)
                return _Read(
                    value, fetched, fresh_until=fetched + timedelta(seconds=ttl)
                )
            except Exception as exc:
                logger.warning("%s unavailable (%s)", source, type(exc).__name__)
                if stale and prior and prior[1].data is not None:
                    age = (now - prior[1].fetched_at).total_seconds()
                    if age < ttl * 3:
                        return replace(prior[1], stale=True)
                return _Read(None, None)

        negative = prior and (prior[1].stale or prior[1].data is None)
        return await self.cache.get(
            key,
            min(ttl, REFRESH_INTERVAL) if negative else ttl,
            load,
            refresh=refresh,
            refresh_interval=REFRESH_INTERVAL,
        )

    async def _window(self, day, end, *, refresh=False):
        async def load():
            if day == end:
                response = await operations.flight_schedule(day.isoformat())
            else:
                response = await operations.flight_schedule(
                    day.isoformat(), end.isoformat()
                )
            flights = {}
            duplicates = set()
            complete = response["complete"]
            for row in response["flights"]:
                departure = utc(row.get("departureUTC"))
                if departure and not day <= departure.date() <= end:
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

        result = await self._read(
            "Leon schedule",
            ("schedule", day, end),
            get_settings().snapshot_schedule_ttl,
            load,
            refresh=refresh,
        )
        if result.data is None:
            raise ScheduleUnavailable
        return result

    async def schedule(self, day, end=None, *, refresh=False):
        end = end or day
        result = await self._window(day, end, refresh=refresh)
        flights, complete = result.data
        ordered = sorted(
            (leg for leg, _ in flights.values()), key=lambda leg: (leg.std, leg.id)
        )
        return Schedule(
            date=day,
            end_date=end,
            complete=complete,
            flights=ordered,
            **result.state("READY" if complete else "PARTIAL"),
        )

    async def _select(self, day, flight_id, week=None, version=None):
        start, end = (week, week + timedelta(days=6)) if week else (day, day)
        if not start <= day <= end:
            raise FlightNotFound
        prior = self.cache.peek(("schedule", start, end)) if version else None
        result = (
            prior[1]
            if prior
            and prior[1].data is not None
            and monotonic() - prior[0] < SELECTION_TTL
            else await self._window(start, end)
        )
        flights, complete = result.data
        if (pair := flights.get(flight_id)) and pair[0].std.date() == day:
            if version and version != pair[0].version:
                raise FlightChanged
            return pair
        if not complete:
            raise ScheduleUnavailable
        raise FlightNotFound

    async def selected_leg(self, day, flight_id, **selection):
        leg, _ = await self._select(day, flight_id, **selection)
        return leg

    async def _foreflight_match(self, row, day, *, refresh):
        settings = get_settings()

        async def schedule():
            async with ForeFlightClient() as client:
                return await flights_for_day(client, day.isoformat())

        async def load():
            result = await self._read(
                "ForeFlight schedule",
                ("foreflight-schedule", day),
                settings.snapshot_schedule_ttl,
                schedule,
                refresh=refresh,
                stale=False,
            )
            if result.data is None:
                raise ValueError("ForeFlight schedule unavailable")
            return match_flight(result.data, row["flightNumber"], day.isoformat(), row)

        result = await self._read(
            "ForeFlight leg",
            ("foreflight-match", row["flightNid"], _version(row)),
            settings.snapshot_briefing_ttl,
            load,
            refresh=refresh,
            stale=False,
            bounded=False,
        )
        if result.data is None:
            raise ValueError("ForeFlight leg unavailable")
        return result.data

    async def plan(self, day, flight_id, *, week=None, version=None, refresh=False):
        flight, row = await self._select(day, flight_id, week, version)

        async def load():
            match = await self._foreflight_match(row, day, refresh=refresh)
            async with self.slots:
                return await load_fast_foreflight(row, match=match)

        result = await self._read(
            "ForeFlight plan",
            ("plan", flight.id, flight.version),
            get_settings().snapshot_briefing_ttl,
            load,
            refresh=refresh,
            bounded=False,
        )
        blocks = result.data or {}
        route = blocks.get("route", SourceBlock("ForeFlight"))
        perf = blocks.get("performance", SourceBlock("ForeFlight"))
        countries = blocks.get("overflight", SourceBlock("ForeFlight"))
        minutes = perf.details.get("eet_minutes")
        ete = (
            int(minutes)
            if type(minutes) in (int, float) and math.isfinite(minutes) and minutes >= 0
            else None
        )
        text = _text(route.details.get("route"))
        alternate = _text(route.details.get("alternate"))
        overflight = route_countries(countries.details.get("overflownCountries"))
        status = "UNAVAILABLE"
        if text or alternate or ete is not None or overflight:
            status = (
                "READY"
                if text
                and ete is not None
                and overflight
                and all(overflight)
                and route.status == perf.status == "ready"
                else "PARTIAL"
            )
        return Plan(
            ete_minutes=ete,
            route=text,
            alternate=alternate,
            overflight=overflight,
            **result.state(status),
        )

    async def handling(self, day, flight_id, *, week=None, version=None, refresh=False):
        flight, row = await self._select(day, flight_id, week, version)
        result = await self._read(
            "Leon handling",
            ("handling", flight.id, flight.version),
            get_settings().snapshot_crew_ttl,
            lambda: load_flight_handlers(row, use_report=True, refresh=refresh),
            refresh=refresh,
        )
        handlers = []
        details = result.data.details if result.data else {}
        for key, role in (("departure", "DEP"), ("arrival", "ARR")):
            if (info := details.get(key)) and info.get("name"):
                handlers.append(
                    Handler(
                        role=role,
                        station=info["station"],
                        name=info["name"],
                        phone=_text(info.get("phone")),
                        email=_text(info.get("email")),
                    )
                )
        status = "UNAVAILABLE"
        if handlers:
            status = (
                "READY"
                if len(handlers) == 2 and all(h.phone and h.email for h in handlers)
                else "PARTIAL"
            )
        return Handling(handlers=handlers, **result.state(status))

    async def card(self, day, flight_id, *, week=None, version=None):
        flight, _ = await self._select(day, flight_id, week, version)
        plan, handling = await asyncio.gather(
            self.plan(day, flight_id, week=week, version=version),
            self.handling(day, flight_id, week=week, version=version),
        )
        return FlightCard(
            flight=flight,
            plan_status={
                "READY": "available",
                "PARTIAL": "partial",
                "STALE": "partial",
                "UNAVAILABLE": "unavailable",
            }[plan.status],
            handling=handling.handlers,
            **plan.model_dump(),
        )

    async def crew(
        self,
        day,
        flight_id,
        *,
        week=None,
        version=None,
        refresh=False,
        assignments_only=False,
    ):
        flight, row = await self._select(day, flight_id, week, version)
        key = (flight.id, flight.version)
        settings = get_settings()
        crew = await self._read(
            "Leon crew",
            ("crew", *key),
            settings.snapshot_crew_ttl,
            lambda: get_flight_crew(flight_id),
            refresh=refresh,
        )
        if crew.data is None:
            return CrewPreparation(available=False)
        assigned = assigned_members(crew.data["crew"], flight_id)
        pilots = [
            (member, role)
            for member in assigned
            if (role := _role(_text(crew_identity(member)[1]))) is not None
        ]
        pilot_assignments = {
            "flightNid": flight_id,
            "crewMemberList": [member for member, _ in pilots],
        }
        duty = (
            await self._read(
                "Leon duty",
                ("duty", *key, repr(pilot_assignments)),
                settings.snapshot_ftl_ttl,
                lambda: load_duty(row, pilot_assignments, include_next_duty=False),
                refresh=refresh,
            )
            if pilots and not assignments_only
            else _Read(None, None)
        )
        assignments = duty.data.details.get("assignments", []) if duty.data else []
        members = []
        for index, (member, role) in enumerate(pilots):
            name, position = crew_identity(member)
            assignment = assignments[index] if index < len(assignments) else None
            members.append(
                CrewMember(
                    name=_text(name),
                    position=_text(position),
                    role=role,
                    hotel=_hotel(member, flight_id),
                    duty=DutyAssignment(**assignment) if assignment else None,
                )
            )
        status = "UNAVAILABLE"
        if members:
            complete = (
                len(members) == 2
                and {"CPT", "FO"} == {member.role for member in members}
                and (
                    assignments_only
                    or all(
                        member.duty
                        and member.duty.reporting
                        and member.duty.duty_duration is not None
                        and member.duty.fdp_remaining is not None
                        and {"Rest Before", "Rest After"}
                        <= {
                            metric.name
                            for metric in member.duty.metrics
                            if metric.actual is not None
                        }
                        for member in members
                    )
                )
            )
            status = "READY" if complete else "PARTIAL"
        state = (duty if duty.fetched_at else crew).state(status)
        if duty.fresh_until and crew.fresh_until:
            state["fresh_until"] = min(duty.fresh_until, crew.fresh_until)
        if crew.stale and members:
            state["status"] = "STALE"
        return CrewPreparation(available=True, members=members, **state)

    async def weather(
        self,
        day,
        flight_id,
        *,
        week=None,
        version=None,
        refresh=False,
        include_taf=True,
    ):
        flight, row = await self._select(day, flight_id, week, version)
        stations = list(dict.fromkeys(filter(None, (flight.departure, flight.arrival))))
        products = ("METAR", "TAF") if include_taf else ("METAR",)
        ttl = get_settings().snapshot_weather_ttl

        async def leon():
            items = await get_flight_weather_leon(row)
            return leon_reports(
                [
                    item
                    for item in items
                    if item.get("flightNid", flight_id) == flight_id
                ],
                include_taf=True,
            )

        async def awc(product):
            result = await get_weather(stations, product=product.lower())
            reports, errors = awc_reports(result, include_taf=True)
            if errors:
                raise RuntimeError("AWC unavailable")
            return reports

        if not stations:
            return WeatherPreparation(partial=True, stations=[], reports=[])
        results = await asyncio.gather(
            self._read(
                "Leon weather",
                ("leon-wx", flight.id, flight.version),
                ttl,
                leon,
                refresh=refresh,
            ),
            *(
                self._read(
                    f"AWC {product}",
                    ("awc", product, *sorted(stations)),
                    ttl,
                    lambda product=product: awc(product),
                    refresh=refresh,
                )
                for product in products
            ),
        )
        now = datetime.now(UTC)
        combined = [report for result in results for report in (result.data or [])]
        reports = [
            WeatherReport(
                station=station,
                product=product,
                raw=report["raw"],
                sources=report["sources"],
                issued_at=report.get("issued_at"),
                valid_from=report.get("valid_from"),
                valid_to=report.get("valid_to"),
                status=_report_status(report, product, now),
                fresh_until=_report_expiry(report, product),
                facts=metar_facts(report["raw"]) if product == "METAR" else {},
            )
            for station in stations
            for product in products
            for report in grouped_reports(combined, station, product)
        ]
        reports.sort(
            key=lambda r: r.issued_at or datetime.min.replace(tzinfo=UTC), reverse=True
        )
        ready = {
            (report.station, report.product)
            for report in reports
            if report.status == "READY"
        }
        status = "UNAVAILABLE"
        if reports:
            if not ready and all(report.status == "STALE" for report in reports):
                status = "STALE"
            else:
                status = (
                    "READY"
                    if len(ready) == len(stations) * len(products)
                    else "PARTIAL"
                )
        return WeatherPreparation(
            partial=status != "READY",
            status=status,
            stations=stations,
            reports=reports,
            fetched_at=max(
                (result.fetched_at for result in results if result.fetched_at),
                default=None,
            ),
            fresh_until=min(
                (result.fresh_until for result in results if result.fresh_until),
                default=None,
            ),
        )

    async def notams(self, day, flight_id, *, week=None, version=None, refresh=False):
        flight, row = await self._select(day, flight_id, week, version)
        plan = self.cache.peek(("plan", flight.id, flight.version))
        block = plan[1].data.get("route") if plan and plan[1].data else None
        stations = list(
            dict.fromkeys(
                filter(
                    None,
                    (
                        flight.departure,
                        flight.arrival,
                        *(
                            block.details.get(key)
                            for key in (
                                "alternate",
                                "secondAlternate",
                                "takeOffAlternate",
                            )
                            if block
                        ),
                    ),
                )
            )
        )

        async def load():
            match = await self._foreflight_match(row, day, refresh=refresh)
            async with self.slots:
                return await load_foreflight(
                    row, match=match, briefing_only=True, stations=stations
                )

        result = await self._read(
            "ForeFlight briefing",
            ("briefing", flight.id, flight.version, *stations),
            get_settings().snapshot_briefing_ttl,
            load,
            refresh=refresh,
            stale=False,
            bounded=False,
        )
        blocks = result.data or {}
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
        details = briefing.details if briefing else {}
        return NotamPreparation(
            stations=stations,
            recognized=recognized,
            notams=notams,
            briefing_url=details.get("url"),
            briefing_time=utc(details.get("generated")),
            departure_offset_minutes=details.get("departure_offset_minutes"),
            **result.state(
                "PARTIAL" if details.get("url") or recognized else "UNAVAILABLE"
            ),
        )
