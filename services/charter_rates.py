import re
import sqlite3
from collections import defaultdict
from contextlib import closing
from datetime import UTC, datetime
from functools import cache
from math import isfinite
from typing import Annotated, Literal

import airportsdata
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from config.settings import settings
from services.snapshot_models import utc

Currency = Literal["USD", "EUR"]
Amount = Annotated[float, Field(ge=0, strict=True)]
OptionalText = Annotated[str, Field(max_length=500)] | None
UTC_RANGE = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d-(?:(?:[01]\d|2[0-3]):[0-5]\d|24:00)")

SCHEMA = """
CREATE TABLE IF NOT EXISTS aircraft_rate_cards (
    id INTEGER PRIMARY KEY,
    registration TEXT,
    aircraft_type TEXT,
    currency TEXT NOT NULL CHECK (currency IN ('USD', 'EUR')),
    pax_hour_rate REAL NOT NULL CHECK (pax_hour_rate >= 0),
    ferry_hour_rate REAL NOT NULL CHECK (ferry_hour_rate >= 0),
    overnight_fee REAL NOT NULL CHECK (overnight_fee >= 0),
    daily_min_hours REAL CHECK (daily_min_hours BETWEEN 0 AND 24),
    fuel_included INTEGER NOT NULL CHECK (fuel_included IN (0, 1)),
    fuel_price_per_kg REAL CHECK (fuel_price_per_kg >= 0),
    margin_pct REAL NOT NULL CHECK (margin_pct >= 0),
    version INTEGER NOT NULL CHECK (version > 0),
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    archived_at TEXT,
    updated_by TEXT NOT NULL,
    comment TEXT,
    CHECK ((registration IS NULL) <> (aircraft_type IS NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS charter_rate_active_registration
    ON aircraft_rate_cards(registration)
    WHERE valid_to IS NULL AND registration IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS charter_rate_active_type
    ON aircraft_rate_cards(aircraft_type)
    WHERE valid_to IS NULL AND aircraft_type IS NOT NULL;

CREATE TABLE IF NOT EXISTS airport_fee_profiles (
    id INTEGER PRIMARY KEY,
    scope_type TEXT NOT NULL CHECK (scope_type IN ('category', 'icao')),
    scope_key TEXT NOT NULL,
    currency TEXT CHECK (currency IN ('USD', 'EUR')),
    handling_landing_est REAL CHECK (handling_landing_est >= 0),
    pax_fee_per_head REAL CHECK (pax_fee_per_head >= 0),
    overflight_avg_per_hr REAL CHECK (overflight_avg_per_hr >= 0),
    permit_lead_days INTEGER CHECK (permit_lead_days >= 0),
    curfew_utc TEXT,
    operating_hours_utc TEXT,
    techstop_turn_minutes INTEGER CHECK (techstop_turn_minutes > 0),
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    updated_by TEXT NOT NULL,
    comment TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS charter_airport_profile_active
    ON airport_fee_profiles(scope_type, scope_key) WHERE valid_to IS NULL;
"""


class CharterRateInput(BaseModel):
    model_config = ConfigDict(
        extra="forbid", str_strip_whitespace=True, allow_inf_nan=False
    )

    registration: str | None = Field(default=None, pattern=r"^[A-Z0-9][A-Z0-9-]{0,15}$")
    aircraft_type: str | None = Field(
        default=None, pattern=r"^[A-Z0-9][A-Z0-9 -]{0,31}$"
    )
    currency: Currency
    pax_hour_rate: Amount
    ferry_hour_rate: Amount
    overnight_fee: Amount
    daily_min_hours: float | None = Field(default=None, ge=0, le=24, strict=True)
    fuel_included: bool = Field(strict=True)
    fuel_price_per_kg: Amount | None = None
    margin_pct: Amount
    comment: OptionalText = None

    @field_validator("registration", "aircraft_type", mode="before")
    @classmethod
    def normalize_scope(cls, value):
        return (value.strip().upper() or None) if isinstance(value, str) else value

    @model_validator(mode="after")
    def one_scope(self):
        if (self.registration is None) == (self.aircraft_type is None):
            raise ValueError("Choose a registration or aircraft type for the rate.")
        return self


class AirportFeeInput(BaseModel):
    model_config = ConfigDict(
        extra="forbid", str_strip_whitespace=True, allow_inf_nan=False
    )

    scope_type: Literal["category", "icao"]
    scope_key: str = Field(max_length=16)
    currency: Currency | None = None
    handling_landing_est: Amount | None = None
    pax_fee_per_head: Amount | None = None
    overflight_avg_per_hr: Amount | None = None
    permit_lead_days: int | None = Field(default=None, ge=0, strict=True)
    curfew_utc: str | None = None
    operating_hours_utc: str | None = None
    techstop_turn_minutes: int | None = Field(default=None, gt=0, strict=True)
    comment: OptionalText = None

    @field_validator("curfew_utc", "operating_hours_utc")
    @classmethod
    def valid_utc_range(cls, value):
        if not value:
            return None
        if not UTC_RANGE.fullmatch(value) or value[:5] == value[6:]:
            raise ValueError("Use a UTC range HH:MM-HH:MM, for example 06:00-23:00.")
        return value

    @model_validator(mode="after")
    def valid_scope(self):
        if self.scope_type == "icao":
            self.scope_key = self.scope_key.upper()
            if not re.fullmatch(r"[A-Z]{4}", self.scope_key):
                raise ValueError("Use a four-letter ICAO airport code.")
        else:
            self.scope_key = self.scope_key.lower()
            if self.scope_key not in {"domestic", "regional", "international"}:
                raise ValueError(
                    "Use domestic, regional or international as the fee category."
                )
        if not self.currency and any(
            (
                self.handling_landing_est,
                self.pax_fee_per_head,
                self.overflight_avg_per_hr,
            )
        ):
            raise ValueError("Set the currency for airport fees.")
        return self


def _rate_row(row):
    rate = dict(row)
    rate["fuel_included"] = bool(rate["fuel_included"])
    return rate


class CharterRates:
    def __init__(self, path=None):
        self.path = path

    def _connect(self):
        path = self.path or settings.database_path
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, timeout=10)
        try:
            conn.row_factory = sqlite3.Row
            conn.executescript(SCHEMA)
        except sqlite3.Error:
            conn.close()
            raise
        return conn

    def snapshot(self):
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN")
            rates = conn.execute(
                "SELECT * FROM aircraft_rate_cards ORDER BY registration, aircraft_type, version DESC"
            ).fetchall()
            profiles = conn.execute(
                "SELECT * FROM airport_fee_profiles WHERE valid_to IS NULL ORDER BY scope_type, scope_key"
            ).fetchall()
        active, archive = [], []
        for row in rates:
            (active if row["valid_to"] is None else archive).append(_rate_row(row))
        return {
            "rates": active,
            "airport_profiles": [dict(row) for row in profiles],
            "archive": archive,
        }

    def save_rate(self, fields, actor):
        rate = CharterRateInput.model_validate(fields).model_dump()
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            previous = conn.execute(
                """
                SELECT MAX(version) AS version FROM aircraft_rate_cards
                WHERE registration IS :registration AND aircraft_type IS :aircraft_type
                """,
                rate,
            ).fetchone()
            rate.update(
                version=(previous["version"] or 0) + 1,
                valid_from=datetime.now(UTC).isoformat(),
                updated_by=str(actor),
            )
            conn.execute(
                """
                UPDATE aircraft_rate_cards SET valid_to = :valid_from, archived_at = :valid_from
                WHERE registration IS :registration AND aircraft_type IS :aircraft_type
                AND valid_to IS NULL
                """,
                rate,
            )
            return _rate_row(
                conn.execute(
                    """
                    INSERT INTO aircraft_rate_cards (
                        registration, aircraft_type, currency, pax_hour_rate, ferry_hour_rate,
                        overnight_fee, daily_min_hours, fuel_included, fuel_price_per_kg,
                        margin_pct, version, valid_from, updated_by, comment
                    ) VALUES (
                        :registration, :aircraft_type, :currency, :pax_hour_rate, :ferry_hour_rate,
                        :overnight_fee, :daily_min_hours, :fuel_included, :fuel_price_per_kg,
                        :margin_pct, :version, :valid_from, :updated_by, :comment
                    ) RETURNING *
                    """,
                    rate,
                ).fetchone()
            )

    def save_airport_profile(self, fields, actor):
        profile = AirportFeeInput.model_validate(fields).model_dump()
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            profile.update(
                valid_from=datetime.now(UTC).isoformat(), updated_by=str(actor)
            )
            conn.execute(
                """
                UPDATE airport_fee_profiles SET valid_to = :valid_from
                WHERE scope_type = :scope_type AND scope_key = :scope_key AND valid_to IS NULL
                """,
                profile,
            )
            return dict(
                conn.execute(
                    """
                    INSERT INTO airport_fee_profiles (
                        scope_type, scope_key, currency, handling_landing_est, pax_fee_per_head,
                        overflight_avg_per_hr, permit_lead_days, curfew_utc, operating_hours_utc,
                        techstop_turn_minutes, valid_from, updated_by, comment
                    ) VALUES (
                        :scope_type, :scope_key, :currency, :handling_landing_est, :pax_fee_per_head,
                        :overflight_avg_per_hr, :permit_lead_days, :curfew_utc, :operating_hours_utc,
                        :techstop_turn_minutes, :valid_from, :updated_by, :comment
                    ) RETURNING *
                    """,
                    profile,
                ).fetchone()
            )


charter_rates = CharterRates()


@cache
def _airport_countries():
    return {
        code: airport["country"] for code, airport in airportsdata.load("ICAO").items()
    }


def fee_category(departure, arrival):
    countries = _airport_countries()
    origin, destination = countries.get(departure), countries.get(arrival)
    if not origin or not destination:
        return None
    return "domestic" if origin == destination else "international"


def airport_profile(snapshot, icao, category=None):
    profiles = snapshot["airport_profiles"]
    for scope_type, scope_key in (("icao", icao), ("category", category)):
        if profile := next(
            (
                row
                for row in profiles
                if row["scope_type"] == scope_type and row["scope_key"] == scope_key
            ),
            None,
        ):
            return profile
    return None


def _lookup_rate(snapshot, registration, aircraft_type):
    rates = snapshot["rates"]
    if registration and (
        match := next(
            (rate for rate in rates if rate["registration"] == registration.upper()),
            None,
        )
    ):
        return match
    if aircraft_type:
        return next(
            (rate for rate in rates if rate["aircraft_type"] == aircraft_type.upper()),
            None,
        )
    return None


def _hours(legs):
    durations = [leg.get("block_minutes") for leg in legs]
    if not all(_measured(duration) for duration in durations):
        return None
    return sum(durations) / 60


def _measured(value):
    return type(value) in (int, float) and isfinite(value) and value >= 0


def _waiting_cost(rate, stay_days, movements, missing):
    minimum = rate["daily_min_hours"]
    if not minimum:
        return stay_days * rate["overnight_fee"]
    by_day = defaultdict(float)
    for leg in movements:
        departure_utc = utc(leg.get("departure_utc"))
        hours = _hours([leg])
        if departure_utc is None or hours is None:
            missing.add("Timed block hours on every segment for the daily minimum")
            return None
        by_day[departure_utc.date()] += hours
    days = (max(by_day) - min(by_day)).days + 1
    shortfall = (days - len(by_day)) * minimum + sum(
        max(0, minimum - hours) for hours in by_day.values()
    )
    return shortfall * rate["pax_hour_rate"]


def _airport_fees(snapshot, pax_legs, ferries, passengers, currency, missing):
    total = 0.0
    fee_missing = set()

    def add_fee(profile, airport, key, label, quantity):
        value = profile.get(key) if profile else None
        if value is None:
            fee_missing.add(f"{airport} {label} estimate")
        elif value and profile["currency"] != currency:
            fee_missing.add(f"{airport} {label} currency ({currency} required)")
        elif value and quantity is None:
            fee_missing.add(f"Flight duration for {airport} {label}")
        elif value:
            return value * quantity
        return 0.0

    for leg, passenger_count in [(leg, passengers) for leg in pax_legs] + [
        (leg, 0) for leg in ferries
    ]:
        departure, arrival = leg["departure"], leg["arrival"]
        category = leg.get("fee_category") or fee_category(departure, arrival)
        destination = airport_profile(snapshot, arrival, category)
        total += add_fee(
            destination, arrival, "handling_landing_est", "handling and landing", 1
        )
        if passenger_count:
            origin = airport_profile(snapshot, departure, category)
            total += add_fee(
                origin, departure, "pax_fee_per_head", "passenger fee", passenger_count
            )
        # The configured per-hour overflight estimate is for the route's arrival profile.
        minutes = leg.get("airborne_minutes")
        hours = minutes / 60 if _measured(minutes) else _hours([leg])
        total += add_fee(
            destination, arrival, "overflight_avg_per_hr", "overflight", hours
        )
    missing.update(fee_missing)
    return total if not fee_missing else None


def _fuel_cost(rate, movements, price_per_kg, fuel_currency, missing):
    if rate["fuel_included"]:
        return 0.0
    if price_per_kg is None:
        price_per_kg = rate.get("fuel_price_per_kg")
        fuel_currency = rate["currency"] if price_per_kg is not None else None
    if not _measured(price_per_kg) or fuel_currency != rate["currency"]:
        missing.add(f"Fuel price per kg in {rate['currency']} for the dry rate")
        return None
    fuel = [leg.get("fuel_kg") for leg in movements]
    if not all(_measured(value) for value in fuel):
        missing.add("Route fuel burn in kg on every segment for the dry rate")
        return None
    return sum(fuel) * price_per_kg


def calculate_economics(
    snapshot,
    *,
    registration,
    aircraft_type,
    legs,
    ferry,
    stay_days,
    passengers,
    ferry_required=False,
    fuel_price_per_kg=None,
    fuel_currency=None,
):
    rate = _lookup_rate(snapshot, registration, aircraft_type)
    result = dict.fromkeys(
        (
            "currency",
            "pax_cost",
            "ferry_cost",
            "wait_cost",
            "fees_est",
            "fuel_addon",
            "floor",
            "target",
            "rate_version",
        )
    )
    if rate is None:
        return {
            **result,
            "missing": [f"Active rate for {registration or aircraft_type}"],
        }
    result.update(currency=rate["currency"], rate_version=rate["version"])
    if not legs:
        return {**result, "missing": ["Passenger routing and block hours"]}

    missing = set()
    ferries = [ferry] if isinstance(ferry, dict) else list(ferry or [])
    pax_hours = _hours(legs)
    ferry_hours = _hours(ferries) if ferries or ferry_required is False else None
    if pax_hours is None:
        missing.add("Passenger block hours on every segment")
    if ferry_hours is None:
        missing.add("Positioning route and block hours")
    movements = [*ferries, *legs]
    components = {
        "pax_cost": pax_hours * rate["pax_hour_rate"]
        if pax_hours is not None
        else None,
        "ferry_cost": ferry_hours * rate["ferry_hour_rate"]
        if ferry_hours is not None
        else None,
        "wait_cost": _waiting_cost(rate, stay_days, movements, missing),
        "fees_est": _airport_fees(
            snapshot, legs, ferries, passengers, rate["currency"], missing
        ),
        "fuel_addon": _fuel_cost(
            rate, movements, fuel_price_per_kg, fuel_currency, missing
        ),
    }
    result.update(
        {
            key: round(amount, 2) if amount is not None else None
            for key, amount in components.items()
        }
    )
    if not missing:
        floor = sum(result[key] for key in components)
        result.update(
            floor=round(floor, 2),
            target=round(floor * (1 + rate["margin_pct"] / 100), 2),
        )
    return {**result, "missing": sorted(missing)}
