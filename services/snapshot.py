import asyncio

from config.settings import settings
from integrations.leon.tools import get_flight_crew, get_flight_schedule
from services.crew_format import format_flight_crew
from services.flight_duty import assigned_members, load_duty
from services.flight_foreflight_fast import load_fast_foreflight
from services.flight_handlers import load_flight_handlers
from services.flight_summary import render_card
from services.operational_routes import selected_flight_weather
from services.snapshot_models import FlightSnapshot, SourceBlock, utc, zulu
from services.source_cache import SourceCache
from services.weather_reports import format_reports

cache = SourceCache()


async def load_flight_snapshot(
    selected, *, refresh=False, on_update=None, from_sked=False
):
    day = str(selected.get("departureUTC") or "")[:10]

    async def cached(key, ttl, loader):
        return await cache.get(key, ttl, loader, refresh=refresh)

    if from_sked and not refresh:
        flight = selected
        schedule = {"complete": True}
    else:
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
                "hotels": [
                    {
                        "position": member["position"].get("name", "Crew")
                        if isinstance(member.get("position"), dict)
                        else "Crew",
                        "name": (member.get("hotel") or {}).get("name"),
                    }
                    for member in assigned_members(crew["crew"], flight["flightNid"])
                    if isinstance(member.get("hotel"), dict)
                    and isinstance(member["hotel"].get("name"), str)
                    and member["hotel"]["name"].strip()
                ],
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
                ),
            },
        )
        if fetched_at := utc(crew.get("fetched_at")):
            snapshot.blocks["crew"].fetched_at = fetched_at
        await emit()
        snapshot.blocks["duty"] = await cached(
            ("duty", *version, repr(crew["crew"])),
            settings.snapshot_ftl_ttl,
            lambda: load_duty(flight, crew["crew"]),
        )

    async def weather():
        result = await cached(
            ("weather_current", *version),
            settings.snapshot_weather_ttl,
            lambda: selected_flight_weather(flight, day, include_taf=False),
        )
        snapshot.blocks["weather"] = SourceBlock(
            "Leon / AWC",
            format_reports(result["reports"], result.get("errors", []))
            if "reports" in result
            else result["answer"],
            "partial" if result.get("partial") else "ready",
            details={"include_taf": False, "reports": list(result.get("reports", []))},
        )
        if fetched_at := utc(result.get("fetched_at")):
            snapshot.blocks["weather"].fetched_at = fetched_at

    async def handlers():
        snapshot.blocks["handlers"] = await cached(
            ("handlers", *version),
            settings.snapshot_crew_ttl,
            lambda: load_flight_handlers(flight),
        )

    async def foreflight():
        snapshot.blocks.update(
            await cached(
                ("foreflight_api", *version),
                settings.snapshot_briefing_ttl,
                lambda: load_fast_foreflight(flight),
            )
        )

    async def section(name, loader):
        try:
            await asyncio.wait_for(loader(), timeout=settings.snapshot_timeout)
        except Exception as exc:
            label = {
                "crew": "Экипаж",
                "weather": "Погода",
                "foreflight": "ForeFlight",
                "handlers": "Handling",
            }[name]
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
        section("handlers", handlers),
    )
    snapshot.blocks.setdefault(
        "duty", SourceBlock("Leon", "Duty / Rest: нет данных экипажа.")
    )
    snapshot.blocks.setdefault(
        "flighttime", SourceBlock("ForeFlight", "Полётное время: нет данных.")
    )
    snapshot.blocks.setdefault(
        "notams",
        SourceBlock("ForeFlight briefing", "NOTAM: открыть «Подготовка» для загрузки."),
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
