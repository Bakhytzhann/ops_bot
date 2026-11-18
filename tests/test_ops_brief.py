"""OPS BRIEF: scheduled positions and access-controlled station observations."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot import bot, ops_brief
from bot.charter_check import build_fleet
from tests.test_bot_commands import update_for


def flight(nid, tail, depart, arrival, first="UAAA", last="UACC"):
    return {
        "flightNid": nid,
        "aircraft": tail,
        "aircraftType": "E35L",
        "flightNumber": f"ABC{nid}",
        "departureUTC": depart,
        "arrivalUTC": arrival,
        "departure": first,
        "arrival": last,
        "cancelled": False,
    }


def example(now):
    return build_fleet(
        {
            "flights": [
                flight(1, "UP-ABC", "2026-11-18T07:00:00Z", "2026-11-18T09:00:00Z"),
                flight(
                    2, "UP-ABC", "2026-11-19T10:00:00Z",
                    "2026-11-19T12:00:00Z", "UACC", "UAAA",
                ),
            ]
        },
        now,
    )


def test_last_station_not_live_location():
    fleet = example(datetime(2026, 11, 18, 12, tzinfo=UTC))
    item = fleet[0]
    assert ops_brief.station_for_aircraft(item) == "UACC"
    assert "плановое прибытие" in ops_brief.status_lines(item)[0]
    assert "не подтверждены" in ops_brief.status_lines(item)[1]
    assert "ABC2" in ops_brief.next_flight_lines(item)[0]
    while_active = example(datetime(2026, 11, 18, 8, tzinfo=UTC))[0]
    assert ops_brief.station_for_aircraft(while_active) is None
    assert "Фактическая позиция не подтверждена" in ops_brief.status_lines(while_active)[2]


def test_recent_notes_and_old_pinned_are_distinct():
    now = datetime(2026, 11, 18, 12, tzinfo=UTC)
    rows = [
        {
            "id": 1, "status": "active", "pinned": 0,
            "observed_at_utc": "2026-11-17T10:00:00Z",
            "verified_by": None, "text": "Проверьте контакт хэндлера",
        },
        {
            "id": 2, "status": "active", "pinned": 1,
            "observed_at_utc": "2026-08-01T10:00:00Z",
            "verified_by": 7, "text": "Закреплённое замечание",
        },
        {
            "id": 3, "status": "active", "pinned": 0,
            "observed_at_utc": "2026-08-01T10:00:00Z",
            "verified_by": None, "text": "Старое незакреплённое",
        },
        {
            "id": 4, "status": "archived", "pinned": 1,
            "observed_at_utc": "2026-11-17T10:00:00Z",
            "verified_by": 7, "text": "Архив",
        },
    ]
    result = ops_brief.current_notes(rows, now)
    assert [x["id"] for x in result] == [1, 2]
    assert result[1]["older"]
    assert "старше 30 дней" in ops_brief.note_label(result[1])
    assert "Неподтверждённое" in ops_brief.note_label(result[0])


@pytest.mark.asyncio
async def test_ops_brief_open_with_station_notes(monkeypatch):
    now = datetime(2026, 11, 18, 12, tzinfo=UTC)
    monkeypatch.setattr(
        ops_brief, "get_flight_schedule",
        AsyncMock(return_value={"flights": [
            flight(1, "UP-ABC", "2026-11-18T07:00:00Z", "2026-11-18T09:00:00Z")
        ], "complete": True}),
    )
    monkeypatch.setattr(ops_brief.notes, "role", lambda user: "Crew")
    monkeypatch.setattr(ops_brief.notes, "list", lambda *args: [
        {
            "id": 1, "status": "active", "pinned": 0,
            "observed_at_utc": "2026-11-17T10:00:00Z",
            "verified_by": 42, "text": "Работа хэндлера ограничена",
        }
    ])
    # Server clock is not patched: this test checks the menu path and content.
    monkeypatch.setattr(
        ops_brief, "load_ops_brief",
        AsyncMock(return_value={
            "token": "demo", "fleet": example(now), "complete": True,
            "fetched_at": now.isoformat(), "expires": 99999999999,
            "notes_access": "ready",
            "notes": {
                "UACC": [
                    {
                        "id": 1, "status": "active", "pinned": 0,
                        "observed_at_utc": "2026-11-17T10:00:00Z",
                        "verified_by": 42, "text": "Работа хэндлера ограничена",
                        "older": False,
                    }
                ]
            },
        }),
    )
    ctx = SimpleNamespace(user_data={})
    update = update_for("OPS BRIEF")
    await bot.handle_message(update, ctx)
    output = update.effective_message.reply_text.call_args.args[0]
    assert "OPS BRIEF" in output
    assert "UP-ABC" in output
    assert "Работа хэндлера ограничена" in output
    assert "не подтверждены" in output


@pytest.mark.asyncio
async def test_notes_not_leaked_to_unbound_user(monkeypatch):
    monkeypatch.setattr(
        ops_brief, "get_flight_schedule",
        AsyncMock(return_value={
            "flights": [flight(
                1, "UP-ABC", "2026-11-17T07:00:00Z",
                "2026-11-17T09:00:00Z",
            )],
            "complete": True,
        }),
    )
    monkeypatch.setattr(ops_brief.notes, "role", lambda user: None)
    read = AsyncMock()
    monkeypatch.setattr(ops_brief.notes, "list", read)
    result = await ops_brief.load_ops_brief(99)
    assert result["notes_access"] == "unbound"
    assert not result["notes"]
    read.assert_not_awaited()
    assert "нужна подтверждённая" in "\n".join(
        ops_brief.notes_info(result, "UACC")
    )


@pytest.mark.asyncio
async def test_stale_callback_is_rejected():
    context = SimpleNamespace(user_data={})
    update = update_for(None)
    update.callback_query = SimpleNamespace(
        data="opb:old:detail:0", answer=AsyncMock()
    )
    await bot.handle_button(update, context)
    assert update.callback_query.answer.call_args.kwargs["show_alert"] is True
