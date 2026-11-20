"""OPS BRIEF: scheduled location only and access-controlled station notes."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot import bot, ops_brief
from bot.charter_check import build_fleet
from tests.test_bot_commands import update_for


def flight(nid, dep, arr, origin="UAAA", destination="UACC"):
    return {
        "flightNid": nid,
        "aircraft": "UP-ABC",
        "aircraftType": "E35L",
        "flightNumber": f"ABC{nid}",
        "departureUTC": dep,
        "arrivalUTC": arr,
        "departure": origin,
        "arrival": destination,
        "cancelled": False,
    }


def test_brief_has_scheduled_airport_and_next_flight():
    schedule = {
        "flights": [
            flight(1, "2026-11-18T07:00:00Z", "2026-11-18T09:00:00Z"),
            flight(
                2,
                "2026-11-19T10:00:00Z",
                "2026-11-19T12:00:00Z",
                "UACC",
                "UAAA",
            ),
        ]
    }
    item = build_fleet(schedule, datetime(2026, 11, 18, 12, tzinfo=UTC))[0]
    assert ops_brief.station_for_aircraft(item) == "UACC"
    assert "плановое прибытие" in ops_brief.status_lines(item)[0]
    assert "не подтверждены" in ops_brief.status_lines(item)[1]
    assert "ABC2" in ops_brief.next_flight_lines(item)[0]
    active = build_fleet(schedule, datetime(2026, 11, 18, 8, tzinfo=UTC))[0]
    assert ops_brief.station_for_aircraft(active) is None


def test_notes_are_time_bound_and_verified_separately():
    now = datetime(2026, 11, 18, 12, tzinfo=UTC)

    def row(id, observed, pinned=0, status="active", verified_by=None):
        return {
            "id": id,
            "status": status,
            "observed_at_utc": observed,
            "pinned": pinned,
            "verified_by": verified_by,
            "text": "Замечание",
        }

    rows = [
        row(1, "2026-11-17T10:00:00Z"),
        row(2, "2026-08-01T10:00:00Z", pinned=1, verified_by=42),
        row(3, "2026-08-01T10:00:00Z"),
        row(4, "2026-11-17T10:00:00Z", status="archived"),
    ]
    selected = ops_brief.current_notes(rows, now)
    assert [item["id"] for item in selected] == [1, 2]
    assert "Неподтверждённое" in ops_brief.note_label(selected[0])
    assert "старше 30 дней" in ops_brief.note_label(selected[1])


@pytest.mark.asyncio
async def test_ops_brief_button_shows_fleet(monkeypatch):
    now = datetime(2026, 11, 18, 12, tzinfo=UTC)
    sample = build_fleet(
        {"flights": [flight(1, "2026-11-18T07:00:00Z", "2026-11-18T09:00:00Z")]},
        now,
    )
    sample_note = {
        "id": 1,
        "status": "active",
        "pinned": 0,
        "verified_by": 42,
        "observed_at_utc": "2026-11-17T10:00:00Z",
        "text": "Работа хэндлера ограничена",
        "older": False,
    }
    monkeypatch.setattr(
        ops_brief,
        "load_ops_brief",
        AsyncMock(
            return_value={
                "token": "demo",
                "fleet": sample,
                "complete": True,
                "fetched_at": now.isoformat(),
                "expires": 99999999999,
                "notes_access": "ready",
                "notes": {"UACC": [sample_note]},
            }
        ),
    )
    ctx = SimpleNamespace(user_data={})
    update = update_for("OPS BRIEF")
    await bot.handle_message(update, ctx)
    text = update.effective_message.reply_text.call_args.args[0]
    assert "OPS BRIEF" in text
    assert "UP-ABC" in text
    assert "Работа хэндлера ограничена" in text


@pytest.mark.asyncio
async def test_notes_not_loaded_for_unbound_staff(monkeypatch):
    monkeypatch.setattr(
        ops_brief,
        "get_flight_schedule",
        AsyncMock(
            return_value={
                "flights": [flight(1, "2026-11-17T07:00:00Z", "2026-11-17T09:00:00Z")],
                "complete": True,
            }
        ),
    )
    monkeypatch.setattr(ops_brief.notes, "role", lambda user: None)
    read = AsyncMock()
    monkeypatch.setattr(ops_brief.notes, "list", read)
    state = await ops_brief.load_ops_brief(99)
    assert state["notes_access"] == "unbound"
    read.assert_not_awaited()
    assert "нужна подтверждённая" in "\n".join(ops_brief.notes_info(state, "UACC"))


@pytest.mark.asyncio
async def test_stale_ops_brief_callback_rejected():
    ctx = SimpleNamespace(user_data={})
    update = update_for(None)
    update.callback_query = SimpleNamespace(data="opb:old:detail:0", answer=AsyncMock())
    await bot.handle_button(update, ctx)
    assert update.callback_query.answer.call_args.kwargs["show_alert"] is True
