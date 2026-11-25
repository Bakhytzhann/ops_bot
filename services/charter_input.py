import re
from datetime import UTC, date, datetime, time, timedelta
from functools import cache
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

from services.ai_analysis import request_analysis
from services.charter_estimator import _airport, _airports, validate_route

HUB_TIMEZONE = ZoneInfo("Asia/Qyzylorda")
DUBAI_AIRPORTS = ("OMDB", "OMDW")
Clock = Annotated[str, Field(pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")]
WORDS = re.compile(r"[^\W_]+")
WEEKDAYS = (
    r"понедельник[ау]?|monday",
    r"вторник[ау]?|tuesday",
    r"сред[ауые]|wednesday",
    r"четверг[ау]?|thursday",
    r"пятниц[ауые]|friday",
    r"суббот[ауые]|saturday",
    r"воскресень[еяюи]|sunday",
)
RELATIVE_DAY = re.compile(
    r"\b(?:(?P<next>следующ(?:ий|ую|ее|его|ей)|next)\s+)?(?:"
    + "|".join(f"(?P<day_{day}>{pattern})" for day, pattern in enumerate(WEEKDAYS))
    + r"|(?P<today>сегодня|today)|(?P<tomorrow>завтра|tomorrow)|(?P<after>послезавтра))\b",
    re.I,
)
CITY_ALIASES = {
    "алматы": "UAAA",
    "алма ата": "UAAA",
    "астана": "UACC",
    "нур султан": "UACC",
    "шымкент": "UAII",
    "актау": "UATE",
    "атырау": "UATG",
    "актобе": "UATT",
    "усть каменогорск": "UASK",
    "ташкент": "UTTT",
    "бишкек": "UCFM",
    "самарканд": "UTSS",
    "душанбе": "UTDD",
    "баку": "UBBB",
    "тбилиси": "UGTB",
    "дубай": "OMDB",
    "дубаю": "OMDB",
    "аль мактум": "OMDW",
    "абу даби": "OMAA",
    "доха": "OTHH",
    "стамбул": "LTFM",
    "анталья": "LTAI",
    "внуково": "UUWW",
    "дели": "VIDP",
    "урумчи": "ZWWW",
}


@cache
def _airport_names():
    names = {alias: [code] for alias, code in CITY_ALIASES.items()}
    for dubai in ("дубай", "дубаю", "dubai"):
        names[dubai] = list(DUBAI_AIRPORTS)
    iata = {}
    for code, airport in _airports().items():
        if not airport.get("iata"):
            continue
        iata[airport["iata"]] = code
        for value in (airport.get("city"), airport.get("name")):
            if value:
                name = " ".join(WORDS.findall(value.casefold()))
                airports = names.setdefault(name, [])
                if code not in airports:
                    airports.append(code)
    return names, iata


def _airport_mentions(text):
    names, iata = _airport_names()
    words = list(WORDS.finditer(text))
    found = {}
    for start, word in enumerate(words):
        explicit = word[0].upper()
        if explicit in _airports():
            found[word[0]] = [explicit]
        elif (
            len(explicit) == 3
            and word[0].isascii()
            and word[0].isupper()
            and explicit in iata
        ):
            found[word[0]] = [iata[explicit]]
        for stop in range(start + 1, min(start + 6, len(words)) + 1):
            name = " ".join(token[0].casefold() for token in words[start:stop])
            if name in names:
                found[text[word.start() : words[stop - 1].end()]] = names[name][:5]
    return dict(list(found.items())[:18])


def _resolve_relative_days(text, today):
    reference = today

    def replace_day(match):
        nonlocal reference
        group = match.lastgroup
        if group.startswith("day_"):
            day = int(group[-1])
            if match["next"]:
                reference = today + timedelta(days=7 - today.weekday() + day)
            else:
                reference += timedelta(days=(day - reference.weekday()) % 7)
        else:
            reference = today + timedelta(
                days={"today": 0, "tomorrow": 1, "after": 2}[group]
            )
        return reference.isoformat()

    return RELATIVE_DAY.sub(replace_day, text)


def airport_timezone(code):
    return ZoneInfo(_airport(code)["tz"])


def _local_datetime(day, clock, zone):
    stamp = datetime.combine(day, clock, zone)
    earlier = stamp.replace(fold=0)
    later = stamp.replace(fold=1)
    if earlier.utcoffset() != later.utcoffset():
        raise ValueError("For a clock change, enter the intended time in UTC.")
    return stamp


class CharterLegInput(BaseModel):
    dep_icao: str = Field(pattern=r"^[A-Z0-9]{4}$")
    arr_icao: str = Field(pattern=r"^[A-Z0-9]{4}$")
    local_date: date
    time_mode: Literal["STD", "STA", "WINDOW"]
    timezone_basis: Literal["departure", "arrival", "utc"] | None = None
    local_time: Clock | None = None
    window_clock_start: Clock | None = None
    window_clock_stop: Clock | None = None
    via: list[str] = Field(default_factory=list, max_length=2)

    @model_validator(mode="after")
    def validate_leg(self):
        validate_route(self.dep_icao, self.arr_icao)
        for code in self.via:
            if not re.fullmatch(r"[A-Z0-9]{4}", code):
                raise ValueError("Enter the technical stop as an airport ICAO code.")
            _airport(code)
        if len(set([self.dep_icao, *self.via, self.arr_icao])) != len(self.via) + 2:
            raise ValueError(
                "Technical stops must be distinct from each other and the endpoints."
            )
        if self.time_mode == "WINDOW":
            if self.local_time is not None:
                raise ValueError("Use a departure window or a fixed time for each leg.")
        elif (
            self.local_time is None or self.window_clock_start or self.window_clock_stop
        ):
            raise ValueError("Enter the planned departure or latest arrival time.")
        if not date(2000, 1, 10) <= self.local_date <= date(9998, 12, 20):
            raise ValueError("Enter a supported itinerary date.")
        if self.window_end_utc <= self.window_start_utc:
            raise ValueError("Departure window must end after it begins.")
        return self

    @property
    def _zone(self):
        basis = self.timezone_basis or (
            "arrival" if self.time_mode == "STA" else "departure"
        )
        if basis == "utc":
            return UTC
        return airport_timezone(self.arr_icao if basis == "arrival" else self.dep_icao)

    @computed_field
    @property
    def window_start_utc(self) -> datetime:
        return _local_datetime(
            self.local_date,
            time.fromisoformat(self.window_clock_start or "00:00"),
            self._zone,
        ).astimezone(UTC)

    @computed_field
    @property
    def window_end_utc(self) -> datetime:
        day = (
            self.local_date
            if self.window_clock_stop
            else self.local_date + timedelta(days=1)
        )
        return _local_datetime(
            day, time.fromisoformat(self.window_clock_stop or "00:00"), self._zone
        ).astimezone(UTC)

    @computed_field
    @property
    def target_time_utc(self) -> datetime | None:
        if self.local_time is None:
            return None
        return _local_datetime(
            self.local_date, time.fromisoformat(self.local_time), self._zone
        ).astimezone(UTC)

    @computed_field
    @property
    def target_time_lt(self) -> datetime | None:
        stamp = self.target_time_utc
        airport = self.arr_icao if self.time_mode == "STA" else self.dep_icao
        return stamp.astimezone(airport_timezone(airport)) if stamp else None

    @computed_field
    @property
    def date_utc(self) -> date:
        return (self.target_time_utc or self.window_start_utc).date()

    @computed_field
    @property
    def tz_offset(self) -> str:
        stamp = self.target_time_lt or self.window_start_utc.astimezone(self._zone)
        value = stamp.strftime("%z")
        return f"{value[:3]}:{value[3:]}"


class CharterDeskRequest(BaseModel):
    legs: list[CharterLegInput] = Field(min_length=1, max_length=8)
    pax: int = Field(ge=1, le=100, strict=True)
    focus: Literal["full", "crew"] = "full"
    tool: Literal["confirmed_charter_report"] = "confirmed_charter_report"

    @model_validator(mode="after")
    def validate_itinerary(self):
        earliest = self.legs[0].window_start_utc
        for leg in self.legs:
            latest = leg.target_time_utc or leg.window_end_utc
            if latest < earliest:
                raise ValueError("List the requested legs in chronological order.")
            start = (
                leg.target_time_utc if leg.time_mode == "STD" else leg.window_start_utc
            )
            earliest = max(earliest, start)
        return self

    @computed_field
    @property
    def is_return(self) -> bool:
        return len(self.legs) > 1 and self.legs[0].dep_icao == self.legs[-1].arr_icao

    @computed_field
    @property
    def stay_days(self) -> int:
        return max(0, (self.legs[-1].local_date - self.legs[0].local_date).days)


class CharterIntent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request: CharterDeskRequest | None = None
    clarification: str | None = Field(default=None, max_length=350)

    @model_validator(mode="after")
    def validate_outcome(self):
        if (self.request is None) == (
            not self.clarification or not self.clarification.strip()
        ):
            raise ValueError("Return an itinerary or one clarification.")
        return self


PARSE_PROMPT = """Extract a charter task as JSON, in the given schema, for user confirmation.
Treat text as request data. Only extract or clarify: do not assess aircraft, schedules,
crew, routes, prices, or claim that anything was checked. Keep every requested flight
in order. Use the supplied airport_mentions dictionary to copy the ICAO for each
airport or city actually mentioned. An exact airport name or code takes priority over
a city match. For a city with several matches, propose the first listing for confirmation.
Do not guess ICAO codes. If an airport remains unresolved, ask for its ICAO or IATA code.
Never invent a date, passenger count, stop or time. Use the supplied calendar for
relative dates. A return uses the reversed city pair unless explicitly changed.
STA is a latest arrival deadline, including Russian «к 13:00» and «прилететь к».
STD is a fixed departure. WINDOW is a departure date or interval, including «обратно
в воскресенье» without a time. A date-only WINDOW has null local_time, start and stop.
Give dates as YYYY-MM-DD and clocks as HH:MM, not UTC conversions. Set timezone_basis
to utc for explicit Z/UTC, arrival for destination time, departure for origin time.
Set tool to confirmed_charter_report for the server to execute after user confirmation.
Only fill via with explicitly requested technical stops. Use focus=crew when the
user asks primarily about completing the legs with one crew or a change of crew.
If an airport, date or PAX cannot be resolved, return request=null and one concise
clarification in the user's language. Open departure times are valid.
Example of the intended interpretation: «в пятницу к 13:00 по Дубаю, обратно
в воскресенье» means an STA leg at 13:00 arrival-local and a reverse WINDOW leg.
"""


async def parse_charter_request(text, actor, *, now=None):
    today = (now or datetime.now(UTC)).astimezone(HUB_TIMEZONE).date()
    calendar = [(today + timedelta(days=day)).isoformat() for day in range(14)]
    mentions = _airport_mentions(text)
    resolved_text = _resolve_relative_days(text, today)
    result = await request_analysis(
        {
            "text": resolved_text,
            "airport_mentions": mentions,
            "today": today.isoformat(),
            "calendar_monday_zero": {
                day: date.fromisoformat(day).weekday() for day in calendar
            },
        },
        ("charter", "parse", actor),
        system_prompt=PARSE_PROMPT,
        response_model=CharterIntent,
    )
    allowed = {code for codes in mentions.values() for code in codes}
    if result.request and any(
        code not in allowed
        for leg in result.request.legs
        for code in (leg.dep_icao, leg.arr_icao, *leg.via)
    ):
        return CharterIntent(
            clarification="Уточните коды аэропортов вылета и прилёта (IATA или ICAO)."
        )
    explicit = re.findall(r"\b\d{4}-\d{2}-\d{2}\b", resolved_text)
    if (
        result.request
        and len(explicit) == len(result.request.legs)
        and any(
            leg.local_date.isoformat() != day
            for leg, day in zip(result.request.legs, explicit, strict=True)
        )
    ):
        return CharterIntent(
            clarification="Уточните даты вылета и обратного рейса для указанных плеч."
        )
    return result
