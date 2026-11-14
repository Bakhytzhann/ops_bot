"""Duty roster and existing Leon FTL output for exactly the assigned crew."""

import asyncio
import math
import re
from datetime import timedelta

from config.access import require_access
from integrations.leon.operations import records
from integrations.leon.tools import call_leon
from services.snapshot_models import SourceBlock, utc, zulu

_slots = asyncio.Semaphore(3)


def ftl_days(data):
    records(data)
    if not isinstance(data, dict) or not isinstance(data.get("ftl"), dict):
        raise TypeError("FTL envelope missing")
    envelope = data["ftl"].get("ftlCalculationsForCrewMember")
    if not isinstance(envelope, dict) or not isinstance(
        envelope.get("calculations"), list
    ):
        raise TypeError("FTL calculations missing")
    return envelope["calculations"]


def seconds_duration(value):
    """Leon FTL durations are seconds, independently checked against reporting times."""
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
        or value >= 2**31 - 1
    ):
        return "нет данных"
    seconds = int(value)
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}" + (f":{seconds:02d}" if seconds else "")


def flight_calculation(data, flight_nid):
    matches = [
        c
        for day in ftl_days(data)
        for c in day.get("ftlCalculations", [])
        if any(s.get("nid") == flight_nid for s in c.get("fdpSectors", []))
    ]
    return matches[0] if len(matches) == 1 else None


def offence_labels(value, path=""):
    """Do not infer offences or apply duration units to counts such as days off."""
    if isinstance(value, dict):
        if value.get("offence") is True:
            yield str(value.get("label") or path)
        for key, child in value.items():
            yield from offence_labels(child, key)
    elif isinstance(value, list):
        for child in value:
            yield from offence_labels(child, path)


def reporting_interval(calculation):
    if not calculation:
        return None
    windows = []
    for sector in calculation.get("fdpSectors", []):
        schedule = sector.get("schedule") or {}
        day = utc(schedule.get("date"))
        if not day or schedule.get("cnl"):
            return None

        def clock(value):
            if not isinstance(value, str) or not re.fullmatch(
                r"(?:[01]\d|2[0-3]):[0-5]\d", value
            ):
                return None
            hour, minute = map(int, value.split(":"))
            return timedelta(hours=hour, minutes=minute)

        start, end = clock(schedule.get("startTime")), clock(schedule.get("endTime"))
        on, off = (
            clock((sector.get("reportOn") or {}).get("time")),
            clock((sector.get("reportOff") or {}).get("time")),
        )
        if any(value is None for value in (start, end, on, off)):
            return None
        day = day.replace(hour=0, minute=0, second=0, microsecond=0)
        start, end = day + start, day + end
        if end < start:
            end += timedelta(days=1)
        on = start.replace(hour=0, minute=0, second=0, microsecond=0) + on
        if on > start:
            on -= timedelta(days=1)
        off = end.replace(hour=0, minute=0, second=0, microsecond=0) + off
        if off < end:
            off += timedelta(days=1)
        windows.append((on, off))
    if not windows:
        return None
    start, end = min(w[0] for w in windows), max(w[1] for w in windows)
    length = calculation.get("dutyLength")
    if (
        isinstance(length, bool)
        or not isinstance(length, (int, float))
        or not math.isfinite(length)
        or length <= 0
    ):
        return None
    # Never turn a calendar ON day into reporting hours, or accept a unit mismatch.
    if abs((end - start).total_seconds() - length) > 60:
        return None
    return start, end


def flight_ftl(data, flight_nid):
    result = flight_calculation(data, flight_nid)
    if result is None:
        return ["FTL / REST: нет однозначного расчёта Leon для этого рейса."]
    lines = ["FTL / REST"]
    violations = []
    shown = set()
    for key, title in (
        ("fdpLength", "FDP"),
        ("dutyLimitedLength", "Duty duration"),
        ("restBeforeDuty", "Rest Before"),
        ("restAfterDuty", "Rest After"),
    ):
        metric = result.get(key)
        if not isinstance(metric, dict):
            lines.append(title + ": нет данных")
            continue
        actual, limit = (
            seconds_duration(metric.get("actual")),
            seconds_duration(metric.get("limit")),
        )
        lines.append(
            f"{title}: {actual} / {metric.get('limitType') or 'лимит'} {limit}"
        )
        if metric.get("offence") is True:
            violations.append(f"{title}: {actual} / {limit}")
            shown.add(str(metric.get("label") or key))
    for label in offence_labels(result):
        if label not in shown:
            violations.append(label)
            shown.add(label)
    if utc(result.get("nextEOBT")):
        lines.append("nextEOBT: " + zulu(result["nextEOBT"]))
    if violations:
        lines.extend(["FTL violations", *violations])
    return lines


def assigned_members(crew, flight_nid):
    result = []
    rows = crew if isinstance(crew, list) else [crew]
    for row in rows:
        if not isinstance(row, dict) or row.get("flightNid") != flight_nid:
            continue
        for member in row.get("crewMemberList") or []:
            result.append(member)
    return result


def duty_rows(roster, ftl, nid):
    """Leon per_type is not a duty/off classification; use its definition or FTL."""
    result = {}
    if not isinstance(roster, Exception):
        try:
            rows = records(roster)
        except (RuntimeError, TypeError):
            rows = []
        for row in rows:
            definition = row.get("definition") or {}
            if (row.get("login") or {}).get("loginNid") != nid:
                continue
            is_duty = str(definition.get("cdrType") or "").casefold() == "duty"
            is_duty |= (
                definition.get("cdrType") == "per_type"
                and str(definition.get("legend") or "").casefold() == "on duty"
            )
            if is_duty:
                start, end = utc(row.get("startTimeIso")), utc(row.get("endTimeIso"))
                if start and end and end > start:
                    result[(start, end)] = row
    try:
        days = ftl_days(ftl)
    except (RuntimeError, TypeError):
        days = []
    ftl_rows = {}
    for day in days:
        for row in day.get("rosterDuties", []):
            if str(row.get("type") or "").upper() == "DUTY":
                start, end = (
                    utc(row.get("startTimeIso")),
                    utc(row.get("endTimeIso")),
                )
                if start and end and end > start:
                    ftl_rows[(start, end)] = row
    return list(ftl_rows.values()) if ftl_rows else list(result.values())


async def load_duty(flight, crew):
    require_access()
    members = assigned_members(crew, flight["flightNid"])
    if not members:
        return SourceBlock("Leon", "Duty / Rest: назначенный экипаж не предоставлен.")
    departure, arrival = utc(flight.get("departureUTC")), utc(flight.get("arrivalUTC"))
    if not departure or not arrival:
        return SourceBlock("Leon", "Duty / Rest: времена рейса не распознаны.")
    # Look behind midnight and ahead for the next assigned shift. Not a rest calculation.
    interval = {
        "start": (departure - timedelta(days=2)).strftime("%Y-%m-%dT00:00:00Z"),
        "end": (arrival + timedelta(days=7)).strftime("%Y-%m-%dT23:59:59Z"),
    }

    async def member_duty(member):
        login = member.get("login") or {}
        nid = login.get("loginNid") if isinstance(login, dict) else None
        contact = member.get("contact") or {}
        name = (
            contact.get("knownAs")
            or " ".join(str(contact.get(k) or "") for k in ("name", "surname")).strip()
            or "Сотрудник"
        )
        position = member.get("position") or {}
        label = f"{position.get('name') or 'Crew'}: {name}"
        if (type(nid) is not int or nid <= 0) and type(
            contact.get("contactNid")
        ) is int:
            try:
                profiles = records(
                    await call_leon(
                        "get-contact-profiles-by-nid-list",
                        {"contactsNids": [contact["contactNid"]]},
                    )
                )
                matches = [
                    p for p in profiles if p.get("contactNid") == contact["contactNid"]
                ]
                profile = matches[0].get("login") if len(matches) == 1 else None
                nid = profile.get("loginNid") if isinstance(profile, dict) else None
            except (RuntimeError, TypeError, OSError):
                nid = None
        if type(nid) is not int or nid <= 0:
            return label + "\nDuty / Rest: нет подтверждённого loginNid.", False
        async with _slots:
            roster, ftl = await asyncio.gather(
                call_leon(
                    "get-duty-list",
                    {
                        "loginNidList": [nid],
                        "timeInterval": interval,
                        "dutyDefinitionNidList": None,
                    },
                ),
                call_leon(
                    "get-crew-ftl-sheet", {"crewMember": nid, "timeInterval": interval}
                ),
                return_exceptions=True,
            )
        lines = [label]
        next_scheduled = "Next scheduled duty: нет данных"
        valid_roster = False
        try:
            calculation = (
                flight_calculation(ftl, flight["flightNid"])
                if not isinstance(ftl, Exception)
                else None
            )
        except (RuntimeError, TypeError):
            calculation = None
        reporting = reporting_interval(calculation)
        try:
            duties = duty_rows(roster, ftl, nid)
            matches = [
                r
                for r in duties
                if utc(r.get("startTimeIso"))
                and utc(r.get("endTimeIso"))
                and utc(r["startTimeIso"]) <= departure
                and utc(r["endTimeIso"]) >= arrival
            ]
            window = (
                reporting
                if reporting and reporting[0] <= departure and reporting[1] >= arrival
                else None
            )
            if window:
                start, end = window
                prefix = "Reporting duty interval"
            elif len(matches) == 1:
                start, end = (
                    utc(matches[0]["startTimeIso"]),
                    utc(matches[0]["endTimeIso"]),
                )
                prefix = "Duty по графику"
                lines.append("Reporting duty interval: нет данных")
            else:
                start = end = None
            if start and end:
                lines.append(
                    f"{prefix}: {zulu(start)} → {zulu(end)} · {seconds_duration((end - start).total_seconds())}"
                )
                following = sorted(
                    utc(r["startTimeIso"])
                    for r in duties
                    if utc(r.get("startTimeIso")) and utc(r["startTimeIso"]) >= end
                )
                if following:
                    next_scheduled = "Next scheduled duty: " + zulu(following[0])
                valid_roster = True
            else:
                lines.append("Reporting duty interval: нет данных")
        except (RuntimeError, TypeError):
            lines.append("DUTY: нет данных")
        if calculation is not None:
            metrics = flight_ftl(ftl, flight["flightNid"])
            next_eobt = [line for line in metrics if line.startswith("nextEOBT:")]
            lines.extend(line for line in metrics if not line.startswith("nextEOBT:"))
            lines.extend([next_scheduled, *next_eobt])
        else:
            lines.extend(
                [
                    "REST: нет данных",
                    next_scheduled,
                    "Следующий допустимый старт после отдыха: не проверено.",
                ]
            )
        return "\n".join(lines), valid_roster

    results = await asyncio.gather(
        *(member_duty(m) for m in members), return_exceptions=True
    )
    texts = [
        r[0]
        if not isinstance(r, Exception)
        else "Duty / Rest сотрудника: источник недоступен."
        for r in results
    ]
    summary = []
    for text in texts:
        lines = text.splitlines()
        summary.append(
            lines[0]
            + " · "
            + next(
                (
                    line
                    for line in lines
                    if line.startswith(("Duty по графику:", "Reporting duty interval:"))
                    and "нет данных" not in line
                ),
                "Duty: не проверено",
            )
        )
        summary.extend(
            line for line in lines if line.startswith(("Rest Before:", "Rest After:"))
        )
    return SourceBlock(
        "Leon",
        "DUTY / REST\n\n" + "\n\n".join(texts),
        "partial",
        details={
            "members": len(members),
            "roster_ready": sum(
                bool(r[1]) for r in results if not isinstance(r, Exception)
            ),
            "ftl_matched": sum("FTL / REST" in text for text in texts),
            "reporting_ready": sum(
                any(
                    line.startswith("Reporting duty interval:")
                    and "нет данных" not in line
                    for line in text.splitlines()
                )
                for text in texts
            ),
            "summary": "\n".join(summary),
            "duration_unit": "seconds",
        },
    )
