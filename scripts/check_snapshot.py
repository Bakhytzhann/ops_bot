"""Read-only snapshot smoke check: counts/statuses only, no operational records."""

import argparse
import asyncio
import logging
import math
from statistics import median
from time import perf_counter

from bot.notam_browser import render_notams
from config.access import current_user_id
from config.settings import settings
from integrations.leon.tools import get_flight_schedule
from services.flight_weather import full_weather, station_roles
from services.notam_filters import notam_sets
from services.snapshot import load_flight_snapshot, render_snapshot


def check_views(snapshot):
    views = 0
    for endpoint, field in (("D", "departure"), ("A", "arrival")):
        groups, _ = notam_sets(snapshot, snapshot.flight.get(field))
        for selected, records in groups.items():
            for page in range(max(1, math.ceil(len(records) / 5))):
                text, markup = render_notams(
                    snapshot, "check", 0, f"nl{endpoint}_{selected}", page
                )
                if len(text) > 3800:
                    raise ValueError("NOTAM list exceeds Telegram limit")
                views += 1
                for row in markup.inline_keyboard:
                    for button in row:
                        if len(button.callback_data.encode()) > 64:
                            raise ValueError("Callback exceeds Telegram limit")
                        if ":nv" not in button.callback_data:
                            continue
                        action = button.callback_data.split(":")[3]
                        detail, _ = render_notams(snapshot, "check", 0, action)
                        if len(detail) > 3800:
                            raise ValueError("NOTAM text exceeds Telegram limit")
                        views += 1
    full = full_weather(snapshot)
    block = snapshot.blocks.get("weather")
    roles = station_roles(snapshot)
    reports = block.details.get("reports", []) if block else []
    for report in reports:
        raw = str(report.get("raw") or "").strip()
        if (
            raw
            and raw not in {"NIL", "N/A"}
            and report.get("station") in roles
            and " ".join(raw.split()) not in " ".join(full.split())
        ):
            raise ValueError("Raw weather missing from detail view")
    crew = snapshot.blocks.get("crew")
    print(
        "View validation:",
        {
            "notam_views": views,
            "wx_stations": len(roles),
            "crew_codes": len(crew.details.get("codes", [])) if crew else 0,
        },
    )


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("date", help="UTC date YYYY-MM-DD")
    parser.add_argument("--repeat", type=int, choices=range(1, 11), default=3)
    args = parser.parse_args()
    if not settings.allowed_user_ids:
        print("Manual allowlist is empty; no data requested")
        return 1
    token = current_user_id.set(next(iter(settings.allowed_user_ids)))
    try:
        schedule = await get_flight_schedule(args.date)
        print(
            f"Schedule: count={len(schedule['flights'])}, complete={schedule['complete']}"
        )
        if not schedule["flights"]:
            return 1
        timings = []
        for _ in range(args.repeat):
            start = perf_counter()
            result = await load_flight_snapshot(schedule["flights"][0])
            timings.append(perf_counter() - start)
        print(f"Snapshot first seconds: {timings[0]:.2f}")
        print(
            f"Sample n={len(timings)}, p50={median(timings):.2f}s, p95={sorted(timings)[math.ceil(len(timings) * 0.95) - 1]:.2f}s (includes cold load)"
        )
        print(f"Card characters: {len(render_snapshot(result))}")
        check_views(result)
        for name, block in result.blocks.items():
            print(f"{name}: {block.status}")
            if block.details.get("error_type"):
                print(
                    "Error metadata:",
                    {
                        k: block.details.get(k)
                        for k in ("error_type", "http_status", "match_error")
                    },
                )
            if name == "notams":
                print(
                    "Extracted airport NOTAM counts:",
                    [len(rows) for rows in block.details.get("notams", {}).values()],
                )
            if name == "duty":
                print(
                    "Crew duty coverage:",
                    {
                        key: block.details.get(key)
                        for key in (
                            "members",
                            "roster_ready",
                            "ftl_matched",
                            "reporting_ready",
                            "duration_unit",
                        )
                    },
                )
        return (
            0
            if result.blocks["flighttime"].details.get("has_ete")
            and result.blocks["notams"].details.get("recognized")
            else 1
        )
    except Exception as exc:  # noqa: BLE001 -- never print credential-bearing API errors
        print(f"Snapshot failed: {type(exc).__name__}")
        return 1
    finally:
        current_user_id.reset(token)


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    raise SystemExit(asyncio.run(main()))
