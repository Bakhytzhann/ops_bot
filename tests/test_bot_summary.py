from copy import deepcopy
from time import monotonic
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from telegram.error import BadRequest

from agent.core import agent
from bot import bot
from bot import flight_browser as browser
from services.snapshot_models import FlightSnapshot
from tests.test_weekly_browser import flight, update


@pytest.fixture
def context(monkeypatch):
    snapshot = FlightSnapshot(flight(7))
    listing = {
        "scope": (42, None),
        "expires": monotonic() + 60,
        "flights": [snapshot.flight],
        "snapshots": {0: snapshot},
        "screen_revision": 3,
        "action": "weather",
    }
    ctx = SimpleNamespace(
        bot_data={"weekly_flight_lists": {"token": listing}},
        user_data={
            "flight_navigation": {
                (42, None): {
                    "selected_flight_id": 7,
                    "return_to": ("token", 0),
                }
            }
        },
    )
    monkeypatch.setattr(browser, "load_flight_snapshot", AsyncMock())
    monkeypatch.setattr(browser, "render_snapshot", Mock())
    monkeypatch.setattr(browser, "get_flight_schedule", AsyncMock())
    monkeypatch.setattr(agent, "chat", AsyncMock())
    monkeypatch.setattr(agent, "warmup", AsyncMock())
    monkeypatch.setattr(agent.client, "chat", AsyncMock())
    monkeypatch.setattr(httpx.AsyncClient, "request", AsyncMock())
    return ctx


@pytest.mark.parametrize(
    "action", ["card", "refresh", "crew", "weather", "duty", "route", "eet"]
)
def test_retired_summary_is_absent_and_contextual_back_remains(action):
    snapshot = FlightSnapshot(flight(7))
    for page in (0, 1):
        markup = browser.card_keyboard(snapshot, "token", 0, action, page=page, pages=2)
        rows = markup.inline_keyboard
        assert [button.text for button in rows[0]] == ["Подготовка", "REFRESH"]
        assert [button.text for button in rows[-1]] == ["LT · IATA", "← К списку"]
        assert not any(
            button.text in {"Bot Report", "Bot Summary"}
            or any(
                value in (button.callback_data or "")
                for value in (":report:", ":summary:")
            )
            for row in rows
            for button in row
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["summary", "report"])
async def test_summary_and_legacy_report_only_answer_without_side_effects(
    context, action
):
    before_data = deepcopy(context.bot_data)
    before_user = deepcopy(context.user_data)
    event = update(data=f"fc:token:0:{action}:0")
    event.callback_query.edit_message_text.side_effect = BadRequest(
        "Message is not modified"
    )
    for _ in range(2):
        await bot.handle_button(event, context)
    assert [call.args for call in event.callback_query.answer.call_args_list] == [
        ("Эта кнопка больше недоступна.",),
        ("Эта кнопка больше недоступна.",),
    ]
    event.callback_query.edit_message_text.assert_not_awaited()
    event.effective_message.reply_text.assert_not_awaited()
    assert context.bot_data == before_data
    assert context.user_data == before_user
    browser.load_flight_snapshot.assert_not_awaited()
    browser.get_flight_schedule.assert_not_awaited()
    browser.render_snapshot.assert_not_called()
    agent.chat.assert_not_awaited()
    agent.client.chat.assert_not_awaited()
    agent.warmup.assert_not_awaited()
    httpx.AsyncClient.request.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "missing_list",
        "expired",
        "other_scope",
        "missing_snapshot",
        "different_flight",
        "other_current_card",
        "out_of_range",
        "invalid_index",
        "invalid_page",
    ],
)
async def test_summary_validates_current_card_without_fetching_or_editing(
    context, case
):
    listing = context.bot_data["weekly_flight_lists"]["token"]
    data = "fc:token:0:summary:0"
    if case == "missing_list":
        data = "fc:unknown:0:summary:0"
    elif case == "expired":
        listing["expires"] = monotonic() - 1
    elif case == "other_scope":
        listing["scope"] = (99, None)
    elif case == "missing_snapshot":
        listing["snapshots"].clear()
    elif case == "different_flight":
        listing["snapshots"][0] = FlightSnapshot(flight(8))
    elif case == "other_current_card":
        context.user_data["flight_navigation"][(42, None)]["return_to"] = ("other", 0)
    elif case == "out_of_range":
        data = "fc:token:1:summary:0"
    elif case == "invalid_index":
        data = "fc:token:invalid:summary:0"
    else:
        data = "fc:token:0:summary:invalid"
    event = update(data=data)
    await bot.handle_button(event, context)
    assert event.callback_query.answer.call_args.kwargs["show_alert"]
    event.callback_query.edit_message_text.assert_not_awaited()
    event.effective_message.reply_text.assert_not_awaited()
    browser.load_flight_snapshot.assert_not_awaited()
    browser.render_snapshot.assert_not_called()
    httpx.AsyncClient.request.assert_not_awaited()
