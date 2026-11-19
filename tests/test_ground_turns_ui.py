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
        button for row in call.kwargs["reply_markup"].inline_keyboard for button in row
    ]


@pytest.mark.asyncio
async def test_ground_list_and_inline_mark_share_the_leon_turn(monkeypatch):
    now = datetime.now(UTC)
    active = {
        "key": "UACC:dep:123",
        "station": "UACC",
        "registration": "UP-ABC",
        "outbound_id": 123,
        "outbound_number": "ACTIVE123",
        "status": "active",
        "reference_time": now,
    }
    recent = {
        **active,
        "key": "UACC:dep:456",
        "outbound_number": "RECENT456",
        "status": "recent",
    }
    load = AsyncMock(
        return_value={
            "cards": [active, recent],
            "schedule_complete": True,
            "watch_complete": True,
            "fetched_at": now,
        }
    )
    record = Mock(return_value=True)
    monkeypatch.setattr(ui, "load_ground_flights", load)
    monkeypatch.setattr(ui, "station_remarks", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        ui,
        "ground_turns",
        SimpleNamespace(
            observe=Mock(),
            baseline=Mock(return_value=None),
            events=Mock(return_value=[]),
            recent_outs=Mock(return_value={}),
            record=record,
        ),
    )
    update = update_for("GROUND STATUS")
    context = SimpleNamespace(user_data={})
    await ui.show_ground_turns(update, context)
    home = _buttons(update.effective_message.reply_text.call_args)
    assert any("ACTIVE123" in button.text for button in home)
    assert not any("RECENT456" in button.text for button in home)

    open_card = _click(next(button for button in home if "ACTIVE123" in button.text))
    await ui.ground_turn_callback(open_card, context)
    card = _buttons(open_card.callback_query.edit_message_text.call_args)
    assert any(button.text == "↻ Статус" for button in card)

    quick = _click(next(button for button in card if button.text == "Отметить этап"))
    await ui.ground_turn_callback(quick, context)
    mark = _click(
        next(
            button
            for button in _buttons(quick.callback_query.edit_message_text.call_args)
            if button.text == "Заправка начата"
        )
    )
    await ui.ground_turn_callback(mark, context)
    assert record.call_args.args == (active, "FUEL_STARTED")
    assert record.call_args.kwargs["request_id"] == "callback:callback-id"
    assert record.call_args.kwargs["event_at"].tzinfo == UTC
    load.assert_awaited_once_with(refresh=False)


def test_ground_manual_utc_time_crosses_midnight_without_guessing_future_actuals():
    now = datetime(2026, 11, 18, 0, 15, tzinfo=UTC)
    assert ui._parse_time("23:50", now, past=True) == datetime(
        2026, 11, 17, 23, 50, tzinfo=UTC
    )
    with pytest.raises(ValueError, match="позже"):
        ui._parse_time("00:30", now, past=True)
