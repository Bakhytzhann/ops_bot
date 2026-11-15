from copy import deepcopy
from time import monotonic
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot import bot
from bot import flight_browser as browser
from services.snapshot_models import SourceBlock
from tests.test_compact_card import card as card
from tests.test_weekly_browser import update


def test_single_switch_defaults_to_zulu_icao(card):
    buttons = browser.preference_buttons("token", {})
    assert len(buttons) == 1
    assert buttons[0].text == "LT · IATA"
    assert buttons[0].callback_data == "sv:token:view"
    label = browser.flight_label(card.flight)
    assert "UAAA 0030z 0200z UGTB" in label


@pytest.mark.parametrize("key", ["view", "time", "codes"])
def test_combined_switch_and_legacy_callbacks_toggle_both_settings(key):
    preferences = {}
    browser.toggle_preference(preferences, key)
    assert preferences == {"time": "LT", "codes": "IATA"}
    assert browser.preference_buttons("token", preferences)[0].text == "UTC · ICAO"
    browser.toggle_preference(preferences, key)
    assert preferences == {"time": "Z", "codes": "ICAO"}


def test_card_uses_freed_button_for_cached_briefing_link(card):
    url = "https://docs.example.test/briefing?signature=test"
    card.blocks["briefing"] = SourceBlock("ForeFlight", details={"url": url})
    row = browser.card_keyboard(card, "token", 0).inline_keyboard[2]
    assert [button.text for button in row] == ["LT · IATA", "Briefing"]
    assert row[0].callback_data == "fc:token:0:view:0"
    assert row[1].url == url and row[1].callback_data is None


@pytest.mark.asyncio
async def test_switch_is_shared_between_sked_and_card_without_reload(card, monkeypatch):
    listing = {
        "scope": (42, None),
        "expires": monotonic() + 60,
        "flights": [card.flight],
        "snapshots": {0: card},
        "complete": True,
        "start": "2026-11-13",
        "end": "2026-11-15",
    }
    context = SimpleNamespace(
        bot_data={"weekly_flight_lists": {"token": listing}}, user_data={}
    )
    loader = AsyncMock()
    monkeypatch.setattr(browser, "load_flight_snapshot", loader)
    local = update(data="sv:token:view")
    await browser.handle_selection(local, context)
    assert listing["preferences"] == {"time": "LT", "codes": "IATA"}
    await browser.handle_selection(update(data="fc:token:0:card:0"), context)
    zulu = update(data="fc:token:0:view:0")
    await browser.handle_selection(zulu, context)
    assert listing["preferences"] == {"time": "Z", "codes": "ICAO"}
    assert (
        "UAAA 0030z 0200z UGTB"
        in zulu.callback_query.edit_message_text.call_args.args[0]
    )
    back = update(data="wk:token:0")
    await browser.handle_selection(back, context)
    markup = back.callback_query.edit_message_text.call_args.kwargs["reply_markup"]
    assert any(
        button.text == "LT · IATA" for row in markup.inline_keyboard for button in row
    )
    loader.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_briefing_shows_notice_without_requests_or_edits(
    card, monkeypatch
):
    listing = {
        "scope": (42, None),
        "expires": monotonic() + 60,
        "flights": [card.flight],
        "snapshots": {0: card},
    }
    context = SimpleNamespace(
        bot_data={"weekly_flight_lists": {"token": listing}}, user_data={}
    )
    row = browser.card_keyboard(card, "token", 0).inline_keyboard[2]
    assert row[1].text == "Briefing"
    assert row[1].callback_data == "fc:token:0:briefing:0"
    loader = AsyncMock()
    monkeypatch.setattr(browser, "load_flight_snapshot", loader)
    before = deepcopy(context.bot_data)
    event = update(data=row[1].callback_data)
    await bot.handle_button(event, context)
    event.callback_query.answer.assert_awaited_once_with(
        "Брифинг ForeFlight недоступен. Нажмите REFRESH.",
        show_alert=True,
    )
    event.callback_query.edit_message_text.assert_not_awaited()
    event.effective_message.reply_text.assert_not_awaited()
    loader.assert_not_awaited()
    assert context.bot_data == before
