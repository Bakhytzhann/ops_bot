"""Preliminary charter duty for one rested multi-pilot crew, using Leon aircraft rules."""

import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import cache
from zoneinfo import ZoneInfo

import httpx

from integrations.leon.mcp_client import OPERATOR, leon_mcp
from services.charter_estimator import _airports
from services.snapshot_models import utc

TIME = r"[0-9]+:[0-5][0-9]"
TABLE_ROW = re.compile(rf"({TIME})\s*\[\s*((?:{TIME}\s*)+)\]")
BLOCK_ROW = re.compile(rf"({TIME})\s+({TIME})")
RULE_FIELDS = (
    "reporting_time_default reporting_time_domestic reporting_time_ferry "
    "duty_start_offset duty_after_last_sector_time duty_after_last_domestic "
    "post_flight_duty_in_fdp_weight max_post_flight_duty_in_fdp "
    "fdp_length_multi_acclimatised fdp_length_hard_limit disable_fdp_length_limit "
    "use_departure_time_for_fdp_limit use_block_time_for_fdp_length "
    "fdp_length_multi_special_rule fdp_length_wocl_reduce "
    "fdp_max_sectors_over_2h_wocl fdp_max_sectors_night "
    "fdp_max_sectors_night_within_acclimatised_zone_other equivalent_sector_count_acclimatised "
    "reduce_max_fdp_due_to_time_zone_difference max_block_time_multi "
    "max_block_time_single_sector use_flight_time_for_block_time_limits "
    "reporting_time_by_homebase reporting_time_by_sunrise reporting_time_distance_dependent "
    "custom_reporting_time_per_departure custom_reporting_time_per_destination "
    "custom_reporting_time_per_position overwrite_all_reporting_time_if_position_occupied "
    "long_haul_flight_duration reporting_time_cockpit_long_haul_flight "
    "duty_after_last_sector_by_homebase duty_after_last_sector_by_sunset "
    "custom_post_flight_duty_per_destination custom_post_flight_duty_per_position "
    "overwrite_all_post_flight_duty_time_if_position_occupied "
    "minimum_rest_at_home_base_time minimum_rest_outside_home_base_time "
    "minimum_rest_at_home_base_duty_multiplier minimum_rest_outside_home_base_duty_multiplier "
    "minimum_rest_by_fdp_at_home_base minimum_rest_by_fdp_outside_home "
    "minimum_rest_by_duty_at_home_base minimum_rest_by_duty_outside_home "
    "minimum_rest_when_no_local_night minimum_rest_by_local_night_window "
    "minimum_rest_outside_homebase_for_time_difference minimum_rest_at_homebase_for_time_difference "
    "increase_rest_due_to_displacement fixed_minimum_rest_after_non_commercial minimum_rest_max_duration "
    "minimum_rest_additional minimum_rest_between_19_and_midnight "
    "minimum_rest_before_by_block use_twice_total_block_time_for_min_rest"
)


class QuoteRulesUnavailable(ValueError):
    pass


@dataclass(frozen=True)
class _Sector:
    departure: str
    arrival: str
    start: datetime
    end: datetime
    ferry: bool
    airborne_minutes: int | float | None


def _minutes(value):
    if isinstance(value, str) and re.fullmatch(TIME, value):
        hours, minutes = map(int, value.split(":"))
        return hours * 60 + minutes
    if value in (None, "", "inf"):
        return None
    raise QuoteRulesUnavailable("Leon returned an unfamiliar time limit.")


def _required(rule, name):
    value = _minutes(rule.get(name))
    if value is None:
        raise QuoteRulesUnavailable(f"Leon did not return {name} for the aircraft.")
    return value


def _enabled(rule, field):
    return rule.get(field) not in (None, "", "0", "false", "inf", "00:00")


def _fraction(value):
    number = float(value or 0)
    if not math.isfinite(number) or number < 0:
        raise QuoteRulesUnavailable("Leon returned an unfamiliar duty multiplier.")
    return number


def _table_value(value, pattern, at, sectors):
    intervals = {}
    for line in (value or "").splitlines():
        if not line.strip():
            continue
        row = pattern.fullmatch(line.strip())
        if (
            row is None
            or (minute := _minutes(row[1])) >= 24 * 60
            or minute in intervals
        ):
            raise QuoteRulesUnavailable("Leon returned an unfamiliar FTL time table.")
        limits = list(map(_minutes, row[2].split()))
        intervals[minute] = limits[min(sectors, len(limits)) - 1]
    if not intervals:
        return None
    applicable = max(
        (start for start in intervals if start <= at), default=max(intervals)
    )
    return intervals[applicable]


def _sectors(rows):
    result = []
    for row in rows:
        start, end = utc(row.get("departure_utc")), utc(row.get("arrival_utc"))
        if not start or not end or end <= start:
            raise QuoteRulesUnavailable(
                "Calculate the routed departure and arrival of every sector first."
            )
        if result and (
            start < result[-1].end or row.get("departure") != result[-1].arrival
        ):
            raise QuoteRulesUnavailable(
                "Resolve the overlap or missing positioning between sectors first."
            )
        result.append(
            _Sector(
                row["departure"],
                row["arrival"],
                start,
                end,
                row.get("ferry") is True,
                row.get("airborne_minutes"),
            )
        )
    if not result:
        raise QuoteRulesUnavailable("There are no routed sectors to check.")
    return result


def _domestic(sector):
    airports = _airports()
    origin = airports.get(sector.departure, {}).get("country")
    return bool(origin and origin == airports.get(sector.arrival, {}).get("country"))


def _report_on(sector, rule):
    if any(
        _enabled(rule, field)
        for field in (
            "reporting_time_by_homebase",
            "reporting_time_by_sunrise",
            "reporting_time_distance_dependent",
            "custom_reporting_time_per_departure",
            "custom_reporting_time_per_destination",
            "custom_reporting_time_per_position",
            "overwrite_all_reporting_time_if_position_occupied",
        )
    ):
        raise QuoteRulesUnavailable(
            "The Leon reporting rule needs additional crew or route input."
        )
    overrides = []
    if (
        sector.ferry
        and (ferry := _minutes(rule.get("reporting_time_ferry"))) is not None
    ):
        overrides.append(ferry)
    if (
        _domestic(sector)
        and (domestic := _minutes(rule.get("reporting_time_domestic"))) is not None
    ):
        overrides.append(domestic)
    if (
        long_haul := _minutes(rule.get("long_haul_flight_duration"))
    ) and sector.end - sector.start >= timedelta(minutes=long_haul):
        overrides.append(_required(rule, "reporting_time_cockpit_long_haul_flight"))
    reporting = (
        max(overrides) if overrides else _required(rule, "reporting_time_default")
    )
    return sector.start - timedelta(minutes=reporting)


def _post_duty(sector, rule):
    if any(
        _enabled(rule, field)
        for field in (
            "duty_after_last_sector_by_homebase",
            "duty_after_last_sector_by_sunset",
            "custom_post_flight_duty_per_destination",
            "custom_post_flight_duty_per_position",
            "overwrite_all_post_flight_duty_time_if_position_occupied",
        )
    ):
        raise QuoteRulesUnavailable(
            "The Leon post-flight duty rule needs additional crew or airport input."
        )
    domestic = (
        _minutes(rule.get("duty_after_last_domestic")) if _domestic(sector) else None
    )
    return (
        domestic
        if domestic is not None
        else _required(rule, "duty_after_last_sector_time")
    )


def _flight_minutes(sectors, rule):
    if _enabled(rule, "use_flight_time_for_block_time_limits"):
        if any(sector.airborne_minutes is None for sector in sectors):
            raise QuoteRulesUnavailable(
                "Leon uses airborne time for this cap. Calculate every sector's airborne time first."
            )
        return sum(sector.airborne_minutes for sector in sectors)
    return sum((sector.end - sector.start).total_seconds() / 60 for sector in sectors)


def _period(sectors, rule, start, stop):
    first, last = sectors[start], sectors[stop - 1]
    report = _report_on(first, rule)
    duty_start = report - timedelta(minutes=_required(rule, "duty_start_offset"))
    post = _post_duty(last, rule)
    release = last.end + timedelta(minutes=post)
    post_max = _minutes(rule.get("max_post_flight_duty_in_fdp"))
    post_in_fdp = (
        post * _fraction(rule.get("post_flight_duty_in_fdp_weight"))
        if post_max is None or post <= post_max
        else 0
    )
    fdp_end = last.end + timedelta(minutes=post_in_fdp)
    zone_name = _airports().get(first.departure, {}).get("tz")
    if not zone_name:
        raise QuoteRulesUnavailable("The reporting airport has no published time zone.")
    at = first.start if _enabled(rule, "use_departure_time_for_fdp_limit") else report
    local = at.astimezone(ZoneInfo(zone_name))
    local_minute = local.hour * 60 + local.minute
    count = stop - start
    table_limit = _table_value(
        rule.get("fdp_length_multi_acclimatised"), TABLE_ROW, local_minute, count
    )
    if table_limit is None or _enabled(rule, "disable_fdp_length_limit"):
        raise QuoteRulesUnavailable(
            "Leon has no active FDP table for a rested multi-pilot crew on this aircraft."
        )
    if (
        rule.get("fdp_length_multi_special_rule")
        not in (None, "", "FtlSpecialRuleNone")
        or rule.get("fdp_length_wocl_reduce") not in (None, "", "FtlWoclReduceNone")
        or any(
            _enabled(rule, name)
            for name in (
                "fdp_max_sectors_over_2h_wocl",
                "fdp_max_sectors_night",
                "fdp_max_sectors_night_within_acclimatised_zone_other",
                "equivalent_sector_count_acclimatised",
                "reduce_max_fdp_due_to_time_zone_difference",
            )
        )
    ):
        raise QuoteRulesUnavailable(
            "Leon applies an additional FDP rule that needs operator confirmation for this quote."
        )
    hard = _minutes(rule.get("fdp_length_hard_limit"))
    fdp_limit = min(table_limit, hard) if hard is not None else table_limit
    flight_minutes = sum(
        (sector.end - sector.start).total_seconds() / 60
        for sector in sectors[start:stop]
    )
    fdp_required = math.ceil(
        flight_minutes
        if _enabled(rule, "use_block_time_for_fdp_length")
        else (fdp_end - report).total_seconds() / 60
    )
    duty_required = math.ceil((release - duty_start).total_seconds() / 60)
    block_limit = _table_value(
        rule.get("max_block_time_multi"), BLOCK_ROW, local_minute, 1
    )
    if count == 1 and (single := _minutes(rule.get("max_block_time_single_sector"))):
        block_limit = single
    block_excess = (
        block_limit is not None
        and _flight_minutes(sectors[start:stop], rule) > block_limit
    )
    return {
        "first_sector": start + 1,
        "last_sector": stop,
        "report_utc": duty_start.isoformat(),
        "fdp_start_utc": report.isoformat(),
        "release_utc": release.isoformat(),
        "fdp_end_utc": fdp_end.isoformat(),
        "required_duty_minutes": duty_required,
        "duty_limit_minutes": fdp_limit + duty_required - fdp_required,
        "duty_limit_basis": "fdp_plus_duty_only_minutes",
        "duty_margin_minutes": fdp_limit - fdp_required,
        "required_fdp_minutes": fdp_required,
        "fdp_limit_minutes": fdp_limit,
        "fdp_margin_minutes": fdp_limit - fdp_required,
        "block_limit_minutes": block_limit,
        "block_limit_exceeded": block_excess,
    }


def _rest(rule, period):
    configured = rule.get("minimum_rest_by_local_night_window", "")
    additional = rule.get("minimum_rest_additional", "")
    if (
        configured
        and not configured.endswith("FtlMinimumRestByLocalNightWindowNone")
        or additional
        and not additional.endswith("FtlMinimumRestAfterNone")
        or any(
            _enabled(rule, field)
            for field in (
                "minimum_rest_by_fdp_at_home_base",
                "minimum_rest_by_fdp_outside_home",
                "minimum_rest_by_duty_at_home_base",
                "minimum_rest_by_duty_outside_home",
                "minimum_rest_when_no_local_night",
                "minimum_rest_outside_homebase_for_time_difference",
                "minimum_rest_at_homebase_for_time_difference",
                "increase_rest_due_to_displacement",
                "minimum_rest_between_19_and_midnight",
                "minimum_rest_before_by_block",
                "use_twice_total_block_time_for_min_rest",
            )
        )
    ):
        return None
    floors = [
        _minutes(rule.get(f"minimum_rest_{where}_time"))
        for where in ("at_home_base", "outside_home_base")
    ]
    if any(floor is None for floor in floors):
        return None
    multipliers = [
        _fraction(rule.get(f"minimum_rest_{where}_duty_multiplier"))
        for where in ("at_home_base", "outside_home_base")
    ]
    return max(*floors, math.ceil(period["required_duty_minutes"] * max(multipliers)))


def _fits(period):
    return period["fdp_margin_minutes"] >= 0 and not period["block_limit_exceeded"]


def _after_rest(sectors, rules):
    @cache
    def partition(start):
        for stop in range(len(sectors), start, -1):
            period = _period(sectors, rules[start], start, stop)
            if not _fits(period):
                continue
            if stop == len(sectors):
                return [period]
            # Next duty uses its own aircraft revision and reporting location.
            gap = (
                _report_on(sectors[stop], rules[stop])
                - timedelta(minutes=_required(rules[stop], "duty_start_offset"))
                - utc(period["release_utc"])
            )
            rest = _rest(rules[start], period)
            if rest is not None and gap >= timedelta(minutes=rest):
                result = partition(stop)
                if result:
                    return [{**period, "required_rest_minutes": rest}, *result]
        return None

    return partition(0)


def _unavailable(issue):
    return {
        "status": "unavailable",
        "source": "Leon",
        "basis": "leon_rules",
        "periods": [],
        "issues": [issue],
    }


def _calculate(sectors, rules):
    planned = _period(sectors, rules[0], 0, len(sectors))
    if _fits(planned):
        status, periods, limiting, issues = "fits", [planned], None, []
    elif periods := _after_rest(sectors, rules):
        status, limiting, issues = "fits_after_rest", None, []
    else:
        status, periods = "limited", [planned]
        limiting = next(
            (
                stop
                for stop in range(1, len(sectors) + 1)
                if _period(sectors, rules[0], 0, stop)["required_fdp_minutes"]
                > planned["fdp_limit_minutes"]
                or (
                    planned["block_limit_exceeded"]
                    and _flight_minutes(sectors[:stop], rules[0])
                    > planned["block_limit_minutes"]
                )
            ),
            len(sectors),
        )
        issues = (
            ["Leon flight time limit is exceeded."]
            if planned["block_limit_exceeded"]
            else []
        )
    return {
        **periods[0],
        "status": status,
        "source": "Leon",
        "basis": "leon_rules",
        "assumption": "rested_acclimatised_multi_pilot",
        "periods": periods,
        "first_limited_segment": limiting,
        "required_rest_minutes": periods[0].get("required_rest_minutes"),
        "issues": issues,
    }


async def _load_aircraft_rules(aircraft_id, sectors):
    if (
        type(aircraft_id) is not int
        or aircraft_id < 1
        or not re.fullmatch(r"[a-zA-Z0-9-]+", OPERATOR or "")
    ):
        raise QuoteRulesUnavailable(
            "The aircraft needs a verified Leon fleet record for the FTL lookup."
        )
    # Every sector might begin its own duty, including across an AOC rule revision.
    variables = {
        "aircraft": aircraft_id,
        **{f"t{i}": sector.start.isoformat() for i, sector in enumerate(sectors)},
    }
    declarations = ",".join(f"$t{i}:DateTimeWithTimezone!" for i in range(len(sectors)))
    selections = " ".join(
        f"s{i}:settings(time:$t{i},aircraftNid:$aircraft){{{RULE_FIELDS}}}"
        for i in range(len(sectors))
    )
    query = f"query CharterFtl($aircraft:AircraftNid!,{declarations}){{ftl{{{selections}}}}}"
    token = await leon_mcp.get_access_token()
    async with httpx.AsyncClient(timeout=12, trust_env=False) as client:
        response = await client.post(
            f"https://{OPERATOR}.leon.aero/api/graphql/",
            json={"query": query, "variables": variables},
            headers={"Authorization": f"Bearer {token}"},
        )
    response.raise_for_status()
    data = response.json()
    ftl = (data.get("data") or {}).get("ftl") if isinstance(data, dict) else None
    rules = (
        [ftl.get(f"s{i}") for i in range(len(sectors))] if isinstance(ftl, dict) else []
    )
    if len(rules) != len(sectors) or not all(isinstance(rule, dict) for rule in rules):
        raise QuoteRulesUnavailable(
            "Leon did not return aircraft FTL settings. Check FTL access on the API key."
        )
    return rules


async def quote_ftl(aircraft, sectors, *, now=None):
    """Use date-effective Leon rules for a preliminary, unassigned crew quote."""
    try:
        sectors = _sectors(sectors)
        rules = await _load_aircraft_rules(aircraft.get("id"), sectors)
        return _calculate(sectors, rules)
    except QuoteRulesUnavailable as exc:
        return _unavailable(str(exc))
    except (httpx.HTTPError, ValueError, TypeError, RuntimeError, OSError):
        return _unavailable("Could not load or apply the aircraft's Leon FTL settings.")
