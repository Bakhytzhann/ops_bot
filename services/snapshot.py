"""Source-backed flight snapshot; operational APIs never pass through the model."""

import asyncio

from config.settings import settings
from integrations.leon.tools import get_flight_crew, get_flight_schedule
from services.crew_format import format_flight_crew
from services.flight_duty import assigned_members, load_duty
from services.flight_foreflight import load_foreflight
from services.flight_summary import render_card
from services.flight_weather import alternate_reports, station_roles
from services.operational_routes import selected_flight_weather
from services.snapshot_models import FlightSnapshot, SourceBlock, utc, zulu
from services.source_cache import SourceCache
from services.weather_reports import format_reports

cache = SourceCache()


async def load_flight_snapshot(selected, *, refresh=False, on_update=None):
    day = str(selected.get("departureUTC") or "")[:10]

    async def cached(key, ttl, loader):
        return await cache.get(key, ttl, loader, refresh=refresh)

    schedule = await cached(
        ("schedule", day),
        settings.snapshot_schedule_ttl,
        lambda: get_flight_schedule(day),
    )
    matches = [
        f
        for f in schedule["flights"]
        if f.get("flightNid") == selected.get("flightNid")
    ]
    if len(matches) != 1:
        raise ValueError(
            "Рейс изменён или не найден в актуальном расписании. Обновите SKED."
        )
    flight = matches[0]
    version = tuple(
        str(flight.get(k))
        for k in (
            "flightNid",
            "departureUTC",
            "arrivalUTC",
            "departure",
            "arrival",
            "aircraft",
            "aircraftType",
            "departureLocal",
            "arrivalLocal",
            "departureIATA",
            "arrivalIATA",
        )
    )
    snapshot = FlightSnapshot(flight)

    async def emit():
        if on_update:
            await on_update(snapshot)

    async def crew_and_duty():
        crew = await cached(
            ("crew", *version),
            settings.snapshot_crew_ttl,
            lambda: get_flight_crew(flight["flightNid"]),
        )
        snapshot.blocks["crew"] = SourceBlock(
            "Leon",
            format_flight_crew(
                flight.get("flightNumber") or "—",
                day,
                crew["crew"],
                compact=True,
                flight_nid=flight["flightNid"],
            ),
            "ready",
            details={
                "codes": list(
                    dict.fromkeys(
                        member["login"]["code"].strip()
                        for member in assigned_members(
                            crew["crew"], flight["flightNid"]
                        )
                        if isinstance(member.get("login"), dict)
                        and isinstance(member["login"].get("code"), str)
                        and member["login"]["code"].strip()
                    )
                )
            },
        )
        if utc(crew.get("fetched_at")):
            snapshot.blocks["crew"].fetched_at = utc(crew["fetched_at"])
        await emit()
        snapshot.blocks["duty"] = await cached(
            ("duty", *version, repr(crew["crew"])),
            settings.snapshot_ftl_ttl,
            lambda: load_duty(flight, crew["crew"]),
        )

    async def weather():
        result = await cached(
            ("weather", *version),
            settings.snapshot_weather_ttl,
            lambda: selected_flight_weather(flight, day, include_taf=True),
        )
        snapshot.blocks["weather"] = SourceBlock(
            "Leon / AWC",
            format_reports(result["reports"], result.get("errors", []))
            if "reports" in result
            else result["answer"],
            "partial" if result.get("partial") else "ready",
            details={"include_taf": True, "reports": list(result.get("reports", []))},
        )
        if utc(result.get("fetched_at")):
            snapshot.blocks["weather"].fetched_at = utc(result["fetched_at"])

    async def foreflight():
        snapshot.blocks.update(
            await cached(
                ("foreflight", *version),
                settings.snapshot_briefing_ttl,
                lambda: load_foreflight(flight, snapshot.blocks),
            )
        )

    async def section(name, loader):
        try:
            await asyncio.wait_for(loader(), timeout=settings.snapshot_timeout)
        except Exception as exc:  # noqa: BLE001 -- source boundary; no payloads/URLs
            label = {"crew": "Экипаж", "weather": "Погода", "foreflight": "ForeFlight"}[
                name
            ]
            snapshot.blocks.setdefault(
                name,
                SourceBlock(
                    name,
                    f"{label}: ошибка получения ({type(exc).__name__}).",
                    details={
                        "error_type": type(exc).__name__,
                        "http_status": getattr(exc, "status_code", None),
                        "match_error": str(exc)
                        if str(exc)
                        in {
                            "ForeFlight route does not match selected Leon leg",
                            "ForeFlight departure does not match selected Leon leg",
                        }
                        else None,
                    },
                ),
            )
        finally:
            await emit()

    await asyncio.gather(
        section("crew", crew_and_duty),
        section("weather", weather),
        section("foreflight", foreflight),
    )
    additional = [
        code
        for code in station_roles(snapshot)
        if code not in {flight.get("departure"), flight.get("arrival")}
    ]
    if additional:
        try:
            extra = await asyncio.wait_for(
                cached(
                    ("alternate_weather", *additional),
                    settings.snapshot_weather_ttl,
                    lambda: alternate_reports(additional),
                ),
                timeout=settings.snapshot_timeout,
            )
            block = snapshot.blocks.setdefault(
                "weather",
                SourceBlock("AWC", details={"include_taf": True, "reports": []}),
            )
            block.details["reports"] = [
                *block.details.get("reports", []),
                *extra["reports"],
            ]
            block.text = format_reports(block.details["reports"], extra["errors"])
            if extra["errors"] or block.status != "ready":
                block.status = "partial"
            await emit()
        except (TimeoutError, OSError):
            pass
    snapshot.blocks.setdefault(
        "duty", SourceBlock("Leon", "Duty / Rest: нет данных экипажа.")
    )
    snapshot.blocks.setdefault(
        "flighttime", SourceBlock("ForeFlight", "Полётное время: нет данных.")
    )
    snapshot.blocks.setdefault(
        "notams",
        SourceBlock(
            "ForeFlight briefing", "NOTAM: нет данных; количество не определено."
        ),
    )
    if not schedule.get("complete", True):
        snapshot.blocks["schedule"] = SourceBlock(
            "Leon", "Расписание неполное.", "partial"
        )
    return snapshot


def render_snapshot(snapshot, preferences=None, *, report=False):
    if not report:
        return render_card(snapshot, preferences)
    lines = ["Bot Report", render_card(snapshot, preferences)]
    for title, key in (
        ("DUTY / REST", "duty"),
        ("WX / TAF", "weather"),
        ("ROUTE / ALTN", "route"),
        ("NOTAM", "notams"),
    ):
        block = snapshot.blocks.get(key)
        lines.append(title + "\n" + (block.text if block else "нет данных"))
    for key, block in snapshot.blocks.items():
        lines.append(
            f"{key}: {block.source} · {block.status} · {zulu(block.fetched_at)}"
        )
    return "\n\n".join(lines)
