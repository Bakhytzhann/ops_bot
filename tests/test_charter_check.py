from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot import bot, charter_check
from tests.test_bot_commands import update_for


def flight(nid, reg, departure, arrival, dep, arr):
    return {
        "flightNid": nid,
        "aircraft": reg,
        "aircraftType": "GLF6",
        "flightNumber": f"ABC{nid}",
        "departureUTC": departure,
        "arrivalUTC": arrival,
        "departure": dep,
        "arrival": arr,
    }


def test_fleet_distinguishes_scheduled_from_actual_position():
    now = datetime(2026, 11, 18, 12, tzinfo=UTC)
    flights = [
        flight(
            1, "UP-AAA", "2026-11-18T09:00:00Z", "2026-11-18T10:00:00Z", "UAAA", "UACC"
        ),
        flight(
            2, "UP-AAA", "2026-11-18T18:00:00Z", "2026-11-18T20:00:00Z", "UACC", "UGTB"
        ),
        flight(
            3, "UP-BBB", "2026-11-18T11:00:00Z", "2026-11-18T15:00:00Z", "UAAA", "OMDB"
        ),
        {
            **flight(
                4,
                "UP-CCC",
                "2026-11-18T10:00:00Z",
                "2026-11-18T11:00:00Z",
                "UAAA",
                "UACC",
            ),
            "cancelled": True,
        },
    ]
    fleet = charter_check.build_fleet({"flights": flights}, now)
    assert [f["registration"] for f in fleet] == ["UP-AAA", "UP-BBB"]
    assert fleet[0]["next"]["flightNid"] == 2
    assert "не подтверждена" in charter_check.position_label(fleet[0])
    assert "факт не проверен" in charter_check.position_label(fleet[1])


def test_fleet_page_discloses_incomplete_schedule():
    state = {
        "token": "test",
        "fleet": [],
        "complete": False,
        "fetched_at": "2026-11-18T12:00:00+00:00",
    }
    text, _ = charter_check.fleet_page("test", state)
    assert "неполное" in text
    assert "не найдены" in text


@pytest.mark.asyncio
async def test_charter_button_reads_leon(monkeypatch):
    schedule = AsyncMock(return_value={"flights": [], "complete": True})
    monkeypatch.setattr(
        charter_check,
        "get_flight_schedule",
        schedule,
    )
    ctx = SimpleNamespace(user_data={})
    update = update_for("CHARTER CHECK")
    await bot.handle_message(update, ctx)
    assert "Выберите раздел" in update.effective_message.reply_text.call_args.args[0]
    schedule.assert_not_awaited()

    markup = update.effective_message.reply_text.call_args.kwargs["reply_markup"]
    callback = update_for(None)
    callback.callback_query = SimpleNamespace(
        data=markup.inline_keyboard[0][0].callback_data,
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )
    await bot.handle_button(callback, ctx)
    assert (
        "Fleet Availability"
        in callback.callback_query.edit_message_text.call_args.args[0]
    )
    assert ctx.user_data["charter_check"]["fleet_data"]["complete"] is True
    schedule.assert_awaited_once()


@pytest.mark.asyncio
async def test_unprivileged_sales_callback_never_reads_requests(monkeypatch):
    monkeypatch.setattr(charter_check, "can_view_charter_sales", lambda _: False)
    fetch_requests = AsyncMock()
    monkeypatch.setattr(charter_check, "list_charter_requests", fetch_requests)
    ctx = SimpleNamespace(user_data={})
    await charter_check.show_charter(update_for("CHARTER CHECK"), ctx)
    token = ctx.user_data["charter_check"]["token"]
    callback = update_for(None)
    callback.callback_query = SimpleNamespace(
        data=f"char:{token}:sales:0", answer=AsyncMock()
    )
    await bot.handle_button(callback, ctx)
    assert callback.callback_query.answer.call_args.kwargs["show_alert"] is True
    fetch_requests.assert_not_awaited()


@pytest.mark.asyncio
async def test_journey_route_analysis_uses_selected_type_and_aggregate(monkeypatch):
    end = datetime.now(UTC).date()
    stats = charter_check.journey_history.summary(end, end)
    stats.update(
        actual_count=1,
        routes=[{"departure": "UAAA", "arrival": "UACC", "count": 1}],
        raw_journey_log="Must never reach the model",
    )
    calls = []

    def summary(start, end, **filters):
        calls.append(filters)
        return stats

    review = AsyncMock(return_value="ИИ · Разбор маршрута")
    monkeypatch.setattr(charter_check.journey_history, "summary", summary)
    monkeypatch.setattr(charter_check, "review_charter", review)
    ctx = SimpleNamespace(user_data={})
    await charter_check.show_charter(update_for("CHARTER CHECK"), ctx)
    token = ctx.user_data["charter_check"]["token"]
    callback = update_for(None)
    callback.callback_query = SimpleNamespace(
        data=f"char:{token}:routeai:30-UAAA-UACC-GLF6",
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )
    await bot.handle_button(callback, ctx)
    assert calls == [{"departure": "UAAA", "arrival": "UACC", "aircraft_type": "GLF6"}]
    evidence = review.await_args.args[0]["LEON_JOURNEY_LOG"]
    assert evidence["filter"] == calls[0]
    assert evidence["actual_count"] == 1
    assert "raw_journey_log" not in evidence
    assert (
        callback.callback_query.edit_message_text.call_args.args[0]
        == "ИИ · Разбор маршрута"
    )


@pytest.mark.asyncio
async def test_charter_details_unknowns_without_crew():
    item = {
        "registration": "UP-AAA",
        "type": "GLF6",
        "last": None,
        "active": None,
        "next": None,
        "flights": [],
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
