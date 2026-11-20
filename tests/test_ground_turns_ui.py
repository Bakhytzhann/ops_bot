from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from bot import ground_turns as ui
from tests.test_bot_commands import update_for


def _click(button):
    update = update_for(None)
    update.callback_query = SimpleNamespace(
        id="callback-id",
        data=button.callback_data,
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )
    return update


def _buttons(call):
    return [
        button
        for row in call.kwargs["reply_markup"].inline_keyboard
        for button in row
    ]


@pytest.mark.asyncio
async def test_ground_card_only_offers_time_statistics_and_back(monkeypatch):
    now = datetime.now(UTC)
    active = {
        "key": "UACC:dep:123",
        "station": "UACC",
        "registration": "UP-ABC",
        "outbound_id": 123,
        "outbound_number": "ABC123",
        "status": "active",
        "reference_time": now,
    }
    load = AsyncMock(
        return_value={
            "cards": [active],
            "schedule_complete": True,
            "watch_complete": True,
            "fetched_at": now,
        }
    )
    monkeypatch.setattr(ui, "load_ground_flights", load)
    monkeypatch.setattr(
        ui,
        "ground_turns",
        SimpleNamespace(
            observe=Mock(),
            events=Mock(return_value=[]),
            recent_outs=Mock(return_value={}),
        ),
    )
    update = update_for("GROUND STATUS")
    context = SimpleNamespace(user_data={})
    await ui.show_ground_turns(update, context)
    home = _buttons(update.effective_message.reply_text.call_args)
    opened = _click(next(button for button in home if "ABC123" in button.text))
    await ui.ground_turn_callback(opened, context)
    card = _buttons(opened.callback_query.edit_message_text.call_args)
    assert [button.text for button in card] == [
        "LT", "Статистика станции", "← К рейсам"
    ]
    assert "Пока нет ручных отметок" in (
        opened.callback_query.edit_message_text.call_args.args[0]
    )
    assert context.user_data[ui.STATE_KEY]["current"] == 0
    load.assert_awaited_once_with(refresh=False)


def test_latest_manual_correction_and_notes_are_visible():
    when = datetime(2026, 11, 18, 12, 45, tzinfo=UTC)
    events = [
        {"id": 1, "kind": "LANDING", "event_at": when,
         "source": "telegram", "detail": {}},
        {"id": 2, "kind": "LANDING", "event_at": when.replace(minute=48),
         "source": "telegram", "detail": {}},
        {"id": 3, "kind": "NOTE", "event_at": when,
         "source": "telegram", "detail": {"text": "ТЗ прибыл поздно"}},
    ]
    turn = {
        "station": "UAAA", "registration": "UP-ABC",
        "inbound_id": 1, "inbound_number": "ABC123",
    }
    card = ui._card_text(turn, events)
    assert "12:48Z" in card
    assert "12:45Z" not in card
    assert "ТЗ прибыл поздно" in card
    assert "DEPARTURE" not in card


def test_ground_manual_utc_time_crosses_midnight_without_guessing_future_actuals():
    now = datetime(2026, 11, 18, 0, 15, tzinfo=UTC)
    assert ui._parse_time("23:50", now, past=True) == datetime(
        2026, 11, 17, 23, 50, tzinfo=UTC
    )
    with pytest.raises(ValueError, match="позже"):
        ui._parse_time("00:30", now, past=True)
