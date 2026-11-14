"""Compatibility entrypoint for the shared operational snapshot service."""

from services.snapshot import load_flight_snapshot, render_snapshot


async def load_flight_card(selected):
    return render_snapshot(await load_flight_snapshot(selected))
