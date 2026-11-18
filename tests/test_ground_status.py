"""Ground Status registration and deterministic validation."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot import bot, ground_status
from bot.ground_status import GroundStore, validate_events
from tests.test_bot_commands import update_for


def test_extract_validates_reported_facts_and_missing_times():
    original = "Колодки 1235, стоянка 24. Заправку закончили, салон готов"
    events = validate_events(
        {
            "events": [
                {
                    "type": "CHOCKS_ON",
                    "time": "12:35",
                    "value": None,
                    "evidence": "Колодки 1235",
                },
                {
                    "type": "STAND",
                    "time": None,
                    "value": "24",
                    "evidence": "стоянка 24",
                },
                {
                    "type": "FUEL_COMPLETED",
                    "time": None,
                    "value": None,
                    "evidence": "Заправку закончили",
                },
                {
                    "type": "CABIN_READY",
                    "time": None,
                    "value": None,
                    "evidence": "салон готов",
                },
                {
                    "type": "REPORTED_READY",
                    "time": None,
                    "value": None,
                    "evidence": "все готовы",
                },
                {
                    "type": "FUEL_STARTED",
                    "time": "18:09",
                    "value": None,
                    "evidence": "Заправку закончили",
                },
            ]
        },
        original,
    )
    assert [event["type"] for event in events] == [
        "CHOCKS_ON",
        "STAND",
        "FUEL_COMPLETED",
        "CABIN_READY",
    ]
    assert events[2]["time"] is None


def test_ground_store_persists_events_and_ignores_duplicate_updates(tmp_path):
    store = GroundStore(tmp_path / "ground.db")
    event = {
        "type": "CABIN_READY",
        "time": None,
        "value": None,
        "evidence": "салон готов",
    }
    assert store.record(42, "ABC123", 101, "салон готов", [event])
    assert not store.record(42, "ABC123", 101, "салон готов", [event])
    entries = store.events(42, "ABC123")
    assert len(entries) == 1 and entries[0]["event_type"] == "CABIN_READY"
    assert store.events(43, "ABC123") == []


@pytest.mark.asyncio
async def test_ground_selection_and_capture(monkeypatch, tmp_path):
    store = GroundStore(tmp_path / "ground.db")
    monkeypatch.setattr(ground_status, "ground_store", store)
    monkeypatch.setattr(
        ground_status,
        "extract_events",
        AsyncMock(
            return_value=[
                {
                    "type": "CABIN_READY",
                    "time": None,
                    "value": None,
                    "evidence": "салон готов",
                }
            ]
        ),
    )
    ctx = SimpleNamespace(
        user_data={}, bot=SimpleNamespace(send_chat_action=AsyncMock())
    )
    open_event = update_for("GROUND STATUS")
    await bot.handle_message(open_event, ctx)
    assert ctx.user_data["ground_selecting"]
    assert (
        "Введите номер рейса"
        in open_event.effective_message.reply_text.call_args.args[0]
    )
    select = update_for("ABC123")
    await bot.handle_message(select, ctx)
    assert ctx.user_data["ground_flight"] == "ABC123"
    data = update_for("салон готов")
    data.effective_message.message_id = 112
    await bot.handle_message(data, ctx)
    assert "Салон готов" in data.effective_message.reply_text.call_args.args[0]
    assert len(store.events(42, "ABC123")) == 1


@pytest.mark.asyncio
async def test_ground_link_button_only_shows_notice():
    ctx = SimpleNamespace(user_data={})
    event = update_for(None)
    event.callback_query = SimpleNamespace(data="ground:link", answer=AsyncMock())
    await bot.handle_button(event, ctx)
    event.callback_query.answer.assert_awaited_once_with(
        "Функция ссылки ещё не реализована.", show_alert=True
    )
