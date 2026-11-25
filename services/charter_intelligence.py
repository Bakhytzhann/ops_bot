import asyncio
import math
import re
import sqlite3
from datetime import UTC, datetime, timedelta

from httpx import HTTPError

from config.settings import settings
from integrations.foreflight.api_client import ForeFlightClient
from integrations.leon.tools import call_leon, get_flight_schedule
from services.foreflight_operations import find_flight, normalized
from services.journey_history import journey_history
from services.snapshot_models import utc, zulu

SOURCE_ERRORS = (
    HTTPError,
    TimeoutError,
    ValueError,
    TypeError,
    RuntimeError,
    OSError,
)
CHECKLIST_TOOLS = {
    "OPS": "get-flight-list-ops-checklist-items",
    "SALES": "get-flight-list-sales-checklist-items",
    "FLIGHT CARE": "get-flight-list-flight-care-checklist-items",
}


def _object(value):
    return value if isinstance(value, dict) else {}


def _objects(value):
    return (
        [item for item in value if isinstance(item, dict)]
        if isinstance(value, list)
        else []
    )


def _records(data):
    if isinstance(data, list):
        return _objects(data)
    if isinstance(data, dict) and not data.get("error") and not data.get("errors"):
        for key in (
            "quoteRequests",
            "quoteRequestList",
            "quoteRequestsList",
            "result",
            "data",
        ):
            if isinstance(data.get(key), (dict, list)):
                return _records(data[key])
    raise ValueError("Unexpected Leon Sales response")


def _id(value):
    if type(value) is int and 0 < value < 10**18:
        return value
    if isinstance(value, str) and re.fullmatch(r"[1-9][0-9]{0,17}", value):
        return int(value)
    return None


def _code(value, pattern):
    if not isinstance(value, str):
        return None
    value = value.strip().upper()
    return value if re.fullmatch(pattern, value) else None


def _airport(value):
    if isinstance(value, dict):
        code = _object(value.get("code"))
        value = code.get("icao") or value.get("icao") or code.get("mostDescriptive")
    return _code(value, r"[A-Z0-9]{3,4}")


def _number(value):
    if isinstance(value, str) and re.fullmatch(
        r"-?[0-9]{1,12}(?:\.[0-9]{1,6})?", value
    ):
        value = float(value)
    if (
        type(value) in {int, float}
        and -(10**12) < value < 10**12
        and math.isfinite(value)
    ):
        return value
    return None


def _status(value):
    if isinstance(value, dict):
        value = value.get("name")
    return _code(value, r"[\w -]{1,30}")


def _iso(value):
    instant = utc(value)
    return instant.isoformat() if instant else None


def _leg(row):
    passenger_list = _object(row.get("passengerList"))
    ferry = passenger_list.get("isFerry", passenger_list.get("ferry"))
    return {
        "departure": _airport(row.get("adep")),
        "arrival": _airport(row.get("ades")),
        "departure_utc": _iso(row.get("std") or row.get("startDateTime")),
        "arrival_utc": _iso(row.get("sta") or row.get("endDateTime")),
        "pax": _number(row.get("paxNo", passenger_list.get("paxNumber"))),
        "ferry": ferry if type(ferry) is bool else None,
    }


def _variant(row):
    aircraft = _object(row.get("acft"))
    pricing = _object(row.get("pricing"))
    legs = _objects(row.get("legs"))
    type_name = aircraft.get("acftTypeName")
    return {
        "id": _id(row.get("nid")),
        "aircraft": _code(aircraft.get("registration"), r"[A-Z0-9-]{1,12}"),
        "type": _code(
            _object(aircraft.get("acftType")).get("icao") or type_name, r"[A-Z0-9]{2,4}"
        ),
        "subcharter": row.get("isSubcharter") is True,
        "status": _status(row.get("status")),
        "quoted_at": _iso(row.get("quotedDateTime")),
        "outdated": row.get("isOutdated") is True,
        "quote_price": _number(row.get("price")),
        "quote_currency": _code(row.get("currency"), r"[A-Z]{3}"),
        "pricing_currency": _code(pricing.get("currency"), r"[A-Z]{3}"),
        "calculated_cost": _number(pricing.get("cost")),
        "reported_margin_pct": _number(pricing.get("pricingMargin")),
        "vat_amount": _number(pricing.get("VATAmount")),
        "legs": [_leg(leg) for leg in legs[:8]],
        "leg_count": len(legs),
    }


def _request(row):
    rfq_id = _id(row.get("nid"))
    variants = _objects(row.get("realizations"))
    legs = _objects(row.get("legs"))
    return {
        "id": rfq_id,
        "reference": _code(row.get("id"), r"[A-Z0-9_-]{1,24}") or f"RFQ {rfq_id}",
        "status": _status(row.get("status")),
        "created_at": _iso(row.get("createdDate")),
        "trip_id": _id(_object(row.get("trip")).get("tripNid")),
        "legs": [_leg(leg) for leg in legs[:8]],
        "leg_count": len(legs),
        "variants": [_variant(variant) for variant in variants[:4]],
        "variant_count": len(variants),
    }


def _route(leg):
    return f"{leg['departure'] or '—'} → {leg['arrival'] or '—'}"


async def list_charter_requests():
    since = datetime.now(UTC) - timedelta(days=30)
    data = await call_leon(
        "get-quote-requests-list",
        {
            "limit": 12,
            "from": since.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "includeArchived": False,
            "isBooked": None,
            "wildcard": None,
        },
    )
    requests = []
    for row in _records(data)[:12]:
        rfq_id = _id(row.get("nid"))
        if rfq_id is not None:
            reference = _code(row.get("id"), r"[A-Z0-9_-]{1,24}") or f"RFQ {rfq_id}"
            legs = _objects(row.get("legs"))
            route = f" · {_route(_leg(legs[0]))}" if legs else ""
            requests.append({"id": rfq_id, "label": f"{reference}{route}"})
    return requests


def _history_comparisons(rfq):
    end = datetime.now(UTC).date() - timedelta(days=1)
    start = end - timedelta(days=89)
    routes = dict.fromkeys(
        (leg["departure"], leg["arrival"])
        for leg in rfq["legs"]
        if leg["departure"] and leg["arrival"]
    )
    aircraft_types = list(
        dict.fromkeys(variant["type"] for variant in rfq["variants"] if variant["type"])
    )
    return [
        {
            "route": f"{departure} → {arrival}",
            "aircraft_type": aircraft_type,
            "summary": journey_history.summary(
                start,
                end,
                departure=departure,
                arrival=arrival,
                aircraft_type=aircraft_type,
            ),
        }
        for departure, arrival in list(routes)[:2]
        for aircraft_type in aircraft_types[:2] or [None]
    ]


async def _booked_leg(rfq):
    if not rfq["trip_id"] or not rfq["legs"]:
        return None
    leg = rfq["legs"][0]
    departure = utc(leg["departure_utc"])
    if not departure or not leg["departure"] or not leg["arrival"]:
        return None
    day = departure.date().isoformat()
    schedule = await get_flight_schedule(day)
    if schedule.get("complete") is not True:
        return None
    matches = [
        flight
        for flight in schedule["flights"]
        if _id(flight.get("tripNid")) == rfq["trip_id"]
        and flight.get("cancelled") is not True
        and flight.get("departure") == leg["departure"]
        and flight.get("arrival") == leg["arrival"]
        and utc(flight.get("departureUTC")) == departure
        and flight.get("aircraft")
    ]
    return matches[0] if len(matches) == 1 else None


async def _foreflight_plan(flight):
    if not settings.foreflight_api_key or not settings.foreflight_vendor_id:
        return None
    departure = utc(flight["departureUTC"])
    day = departure.date().isoformat()
    async with ForeFlightClient() as client:
        row, _ = await find_flight(client, flight.get("flightNumber"), day, flight)
        if (
            row.get("departure") != flight["departure"]
            or row.get("destination") != flight["arrival"]
            or utc(row.get("departureTime")) != departure
            or normalized(row.get("aircraftRegistration"))
            != normalized(flight["aircraft"])
        ):
            return None
        plan = _object(await client.get_performance(row["flightId"]))
    performance = _object(plan.get("performance")) or plan
    fuel = _object(performance.get("fuel"))
    unit = _code(fuel.get("unit"), r"KG|KILOGRAMS?|LB|LBS|POUNDS?")
    minutes = _number(_object(performance.get("times")).get("timeToDestinationMinutes"))
    burn = _number(fuel.get("fuelToDestination")) if unit else None
    minutes = minutes if minutes and minutes > 0 else None
    burn = burn if burn and burn > 0 else None
    if minutes is None and burn is None:
        return None
    return {
        "route": _route(flight),
        "departure_utc": departure.isoformat(),
        "aircraft": _code(flight["aircraft"], r"[A-Z0-9-]{1,12}"),
        "ete_minutes": minutes,
        "fuel_to_destination": burn,
        "fuel_unit": unit,
        "has_warnings": bool(
            plan.get("errors")
            or plan.get("warnings")
            or performance.get("errors")
            or performance.get("warnings")
        ),
    }


async def load_flight_checklist(tool, flight_nid, *, limit=4):
    data = await call_leon(tool, {"flightNidList": [flight_nid]})
    if isinstance(data, dict):
        data = data.get("result")
    if not isinstance(data, list) or any(
        not isinstance(row, dict) or row.get("error") for row in data
    ):
        raise ValueError("Unexpected Leon checklist response")
    items = []
    for row in data:
        if _id(row.get("flightNid")) != flight_nid:
            continue
        label = _code(row.get("label"), r"[\w /().-]{1,60}")
        if label:
            items.append(
                {
                    "item": label,
                    "statuses": [
                        caption
                        for status in _objects(row.get("status"))[:3]
                        if (caption := _status(status.get("caption")))
                    ],
                }
            )
    return {"count": len(items), "items": items[:limit]}


async def _optional_source(coroutine):
    try:
        return await asyncio.wait_for(coroutine, timeout=12)
    except SOURCE_ERRORS:
        return None


async def _booked_evidence(rfq):
    flight = await _optional_source(_booked_leg(rfq))
    if not flight:
        return None, {}
    flight_nid = _id(flight.get("flightNid"))
    keys = tuple(CHECKLIST_TOOLS) if flight_nid is not None else ()
    plan, *checklists = await asyncio.gather(
        _optional_source(_foreflight_plan(flight)),
        *(
            _optional_source(load_flight_checklist(CHECKLIST_TOOLS[group], flight_nid))
            for group in keys
        ),
    )
    return plan, dict(zip(keys, checklists, strict=True))


async def load_charter_request(rfq_id):
    if type(rfq_id) is not int or _id(rfq_id) is None:
        raise ValueError("Invalid Leon RFQ ID")
    rows = _records(
        await call_leon(
            "get-quote-requests-by-nid-list", {"quoteRequestNids": [rfq_id]}
        )
    )
    matching = [row for row in rows if _id(row.get("nid")) == rfq_id]
    if len(matching) != 1:
        raise ValueError("Leon RFQ not found unambiguously")
    rfq = _request(matching[0])
    (plan, checklists), history = await asyncio.gather(
        _booked_evidence(rfq), _optional_history(rfq)
    )
    return {
        "rfq": rfq,
        "history": history,
        "foreflight": plan,
        "checklists": checklists,
    }


async def _optional_history(rfq):
    try:
        return await asyncio.to_thread(_history_comparisons, rfq)
    except sqlite3.Error:
        return None


def _amount(value, currency=None):
    return (
        "—" if value is None else f"{value:,.2f}" + (f" {currency}" if currency else "")
    )


def render_charter_request(data):
    rfq = data["rfq"]
    lines = [
        f"CHARTER CHECK · {rfq['reference']}",
        f"Статус Leon: {rfq['status'] or '—'} · плеч: {rfq['leg_count']}",
    ]
    for leg in rfq["legs"][:3]:
        date = zulu(leg["departure_utc"])
        pax = f" · {leg['pax']:g} PAX" if leg["pax"] is not None else ""
        lines.append(f"{_route(leg)} · {date}{pax}")
    lines.append(f"\nВарианты Leon: {len(rfq['variants'])} из {rfq['variant_count']}")
    for variant in rfq["variants"]:
        registration = variant["aircraft"] or (
            "Субчартер" if variant["subcharter"] else "Борт не указан"
        )
        price = _amount(variant["quote_price"], variant["quote_currency"])
        cost = _amount(variant["calculated_cost"], variant["pricing_currency"])
        reported_margin = variant["reported_margin_pct"]
        margin = "—" if reported_margin is None else f"{reported_margin:g}%"
        outdated = " · оффер устарел" if variant["outdated"] else ""
        lines.append(
            f"{registration} · оффер {price} · расчёт затрат {cost} · маржа Leon {margin}{outdated}"
        )
        sectors = [
            _route(leg) + (" (перегон)" if leg["ferry"] else "")
            for leg in variant["legs"][:3]
        ]
        if sectors:
            suffix = " · …" if variant["leg_count"] > len(sectors) else ""
            lines.append(" / ".join(sectors) + suffix)
    if data["history"]:
        lines.append("\nJourney Log · локальная выборка за 90 дней")
        for row in data["history"]:
            summary = row["summary"]
            block = summary["block_minutes"]
            label = f"{row['route']} · {row['aircraft_type'] or 'все типы'}"
            if summary["last_synced_at"] is None:
                lines.append(f"{label} · история ещё не загружена")
                continue
            basis = (
                f" · медиана block {block['median']:g} мин (n={block['count']})"
                if block["count"]
                else ""
            )
            lines.append(
                f"{label} · фактических рейсов: {summary['actual_count']}{basis}"
            )
    elif data["history"] is None:
        lines.append("\nJourney Log: не удалось прочитать локальную статистику.")
    if plan := data["foreflight"]:
        lines.append(
            f"\nForeFlight · план первого плеча: {_amount(plan['ete_minutes'])} мин · "
            f"топливо до цели {_amount(plan['fuel_to_destination'], plan['fuel_unit'])}"
        )
        if plan["has_warnings"]:
            lines.append("ForeFlight вернул предупреждения или ошибки расчёта.")
    elif rfq["trip_id"]:
        lines.append("\nForeFlight: план первого плеча не подтверждён сопоставлением.")
    if data["checklists"]:
        lines.append("\nЧек-листы Leon · первое плечо")
        for group, checklist in data["checklists"].items():
            if checklist is None:
                lines.append(f"{group}: источник недоступен")
                continue
            lines.append(f"{group}: позиций {checklist['count']}")
            for item in checklist["items"][:2]:
                status = ", ".join(item["statuses"]) or "статус не указан"
                lines.append(f"· {item['item']} — {status}")
    lines.append(
        "\nСуммы оффера и калькуляции — данные Leon. Подтвердите выполнимость у Ops и Crew Control."
    )
    return "\n".join(lines)


def charter_request_sources(data):
    rfq = data["rfq"]
    sources = {
        "LEON_SALES": {
            **rfq,
            "legs": rfq["legs"][:4],
            "variants": [
                {**variant, "legs": variant["legs"][:4]} for variant in rfq["variants"]
            ],
        }
    }
    if data["history"]:
        keys = (
            "start",
            "end",
            "actual_count",
            "complete",
            "covered_days",
            "last_synced_at",
            "block_minutes",
            "block_delta_minutes",
            "airborne_minutes",
            "fuel",
        )
        sources["LEON_JOURNEY_LOG"] = [
            {
                "route": row["route"],
                "aircraft_type": row["aircraft_type"],
                "aircraft_types": row["summary"]["aircraft_types"][:6],
                **{key: row["summary"][key] for key in keys},
            }
            for row in data["history"]
        ]
    if data["foreflight"]:
        sources["FOREFLIGHT_PLAN"] = data["foreflight"]
    if data["checklists"]:
        sources["LEON_CHECKLISTS"] = data["checklists"]
    return sources
