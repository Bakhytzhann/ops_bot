"""Read-only charter fleet monitor must never fabricate live position or clearance."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot import charter_check
from bot import bot
from tests.test_bot_commands import update_for


def flight(nid, reg, departure, arrival, dep, arr):
    return {
        "flightNid": nid, "aircraft": reg, "aircraftType": "GLF6",
        "flightNumber": f"ABC{nid}", "departureUTC": departure,
        "arrivalUTC": arrival, "departure": dep, "arrival": arr,
    }


def test_fleet_distinguishes_scheduled_from_actual_position():
    now = datetime(2026, 11, 18, 12, tzinfo=UTC)
    flights = [
        flight(1, "UP-AAA", "2026-11-18T09:00:00Z", "2026-11-18T10:00:00Z", "UAAA", "UACC"),
        flight(2, "UP-AAA", "2026-11-18T18:00:00Z", "2026-11-18T20:00:00Z", "UACC", "UGTB"),
        flight(3, "UP-BBB", "2026-11-18T11:00:00Z", "2026-11-18T15:00:00Z", "UAAA", "OMDB"),
        {**flight(4, "UP-CCC", "2026-11-18T10:00:00Z", "2026-11-18T11:00:00Z", "UAAA", "UACC"), "cancelled": True},
    ]
    fleet = charter_check.build_fleet({"flights": flights}, now)
    assert [f["registration"] for f in fleet] == ["UP-AAA", "UP-BBB"]
    assert fleet[0]["next"]["flightNid"] == 2
    assert "не подтверждена" in charter_check.position_label(fleet[0])
    assert "факт не проверен" in charter_check.position_label(fleet[1])


def test_fleet_page_discloses_incomplete_schedule():
    state = {
        "token": "test", "fleet": [], "complete": False,
        "fetched_at": "2026-11-18T12:00:00+00:00",
    }
    text, _ = charter_check.fleet_page("test", state)
    assert "неполное" in text
    assert "не найдены" in text


@pytest.mark.asyncio
async def test_charter_button_reads_leon(monkeypatch):
    monkeypatch.setattr(
        charter_check, "get_flight_schedule",
        AsyncMock(return_value={"flights": [], "complete": True}),
    )
    ctx = SimpleNamespace(user_data={})
    update = update_for("CHARTER CHECK")
    await bot.handle_message(update, ctx)
    assert "Fleet Availability" in update.effective_message.reply_text.call_args.args[0]
    assert ctx.user_data["charter_fleet"]["complete"] is True


@pytest.mark.asyncio
async def test_charter_details_unknowns_without_crew():
    item = {
        "registration": "UP-AAA", "type": "GLF6",
        "last": None, "active": None, "next": None, "flights": [],
    }
    result = await charter_check.detail_text(item, False)
    assert "MEL / технические ограничения: реестр не подключён" in result
    assert "Остаток доступного FDP для нового чартера: не рассчитан" in result
    assert "Расписание неполное" in result


@pytest.mark.asyncio
async def test_charter_old_callback_rejected():
    ctx = SimpleNamespace(user_data={})
    update = update_for(None)
    update.callback_query = SimpleNamespace(
        data="char:old:detail:0", answer=AsyncMock()
    )
    await bot.handle_button(update, ctx)
    assert update.callback_query.answer.call_args.kwargs["show_alert"] is True
