from datetime import date
from time import monotonic
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot import bot
from bot import flight_browser as browser
from bot.keyboards import SCHEDULE_BUTTON, home_keyboard
from config.access import current_chat_id, current_user_id
from config.settings import settings
from services import snapshot as flight_card
from services.snapshot_models import FlightSnapshot, SourceBlock
from services.source_cache import SourceCache


def flight(nid, number="ABC1265", departure="UTTT"):
    return {
        "flightNid": nid,
        "flightNumber": number,
        "departure": departure,
        "arrival": "UAAA",
        "departureUTC": "2026-11-11T12:00:00Z",
        "arrivalUTC": "2026-11-11T13:30:00Z",
        "aircraft": "TEST",
    }


def update(text="/week", data=None, thread=None, user=42, chat=None):
    message = SimpleNamespace(
        text=text,
        message_thread_id=thread,
        reply_to_message=None,
        reply_text=AsyncMock(),
    )
    return SimpleNamespace(
        effective_message=message,
        effective_user=SimpleNamespace(id=user, is_bot=False),
        effective_chat=SimpleNamespace(id=chat or user, type="private"),
        callback_query=SimpleNamespace(
            data=data, answer=AsyncMock(), edit_message_text=AsyncMock()
        ),
    )


@pytest.fixture
def context(monkeypatch):
    monkeypatch.setattr(settings, "telegram_allowed_user_ids", "42")
    return SimpleNamespace(bot_data={}, user_data={})


@pytest.fixture
def identity(monkeypatch):
    monkeypatch.setattr(flight_card, "cache", SourceCache())
    monkeypatch.setattr(flight_card, "load_foreflight", AsyncMock(return_value={}))
    u, c = current_user_id.set(42), current_chat_id.set(42)
    yield
    current_chat_id.reset(c)
    current_user_id.reset(u)


@pytest.mark.asyncio
async def test_sked_is_exactly_three_days_and_buttons_contain_route(
    context, identity, monkeypatch
):
    query = AsyncMock(return_value={"flights": [flight(1)], "complete": True})
    monkeypatch.setattr(browser, "get_flight_schedule", query)
    u = update()
    await browser.show_week(u, context, today=date(2026, 12, 29))
    query.assert_awaited_once_with("2026-12-29", "2026-12-31")
    markup = u.effective_message.reply_text.call_args.kwargs["reply_markup"]
    label = markup.inline_keyboard[0][0].text
    assert label == "ABC1265 11NOV UTTT 1200z 1330z UAAA"
    assert len(markup.inline_keyboard[0][0].callback_data.encode()) <= 64


def test_all_flights_reachable_across_pages():
    rows = [flight(i) for i in range(1, 32)]
    listing = {
        "flights": rows,
        "start": "2026-11-11",
        "end": "2026-11-17",
        "complete": True,
    }
    ids = []
    for page in range(3):
        _, markup = browser.render_page("token", listing, page)
        ids.extend(
            b.callback_data
            for row in markup.inline_keyboard
            for b in row
            if b.callback_data.startswith("fl:")
        )
    assert ids == [f"fl:token:{i}" for i in range(31)]
    with pytest.raises(ValueError):
        browser.render_page("token", listing, 3)


@pytest.mark.asyncio
async def test_shared_list_selects_exact_nid_for_another_member(
    context, identity, monkeypatch
):
    context.bot_data["weekly_flight_lists"] = {
        "token": {
            "scope": (42, None),
            "expires": monotonic() + 60,
            "flights": [flight(111), flight(222)],
        }
    }
    snapshot = FlightSnapshot(
        flight(222),
        blocks={
            name: SourceBlock("Test", "Crew and METAR", "ready")
            for name in ("crew", "weather", "duty", "flighttime", "notams")
        },
    )
    load = AsyncMock(return_value=snapshot)
    monkeypatch.setattr(browser, "load_flight_snapshot", load)
    u = update(data="fl:token:1", user=42)
    await bot.handle_button(u, context)
    load.assert_awaited_once()
    assert load.call_args.args == (flight(222),)
    assert load.call_args.kwargs["refresh"] is False
    assert callable(load.call_args.kwargs["on_update"])
    assert "ABC1265" in u.callback_query.edit_message_text.call_args.args[0]
    u.effective_message.reply_text.assert_not_awaited()
    assert (
        context.user_data["flight_navigation"][(42, None)]["selected_flight_id"] == 222
    )


@pytest.mark.parametrize(
    "scope,expiry",
    [((42, 9), 60), ((123, None), 60), ((42, None), -1)],
)
@pytest.mark.asyncio
async def test_old_or_cross_scope_buttons_do_not_load_data(
    context, identity, monkeypatch, scope, expiry
):
    context.bot_data["weekly_flight_lists"] = {
        "token": {
            "scope": scope,
            "expires": monotonic() + expiry,
            "flights": [flight(111)],
        }
    }
    load = AsyncMock()
    monkeypatch.setattr(browser, "load_flight_snapshot", load)
    with pytest.raises(ValueError):
        await browser.handle_selection(update(data="fl:token:0"), context)
    load.assert_not_awaited()


@pytest.mark.parametrize("text", ["/week@OPS_bot", SCHEDULE_BUTTON])
@pytest.mark.asyncio
async def test_week_command_and_persistent_button_bypass_model(
    context, monkeypatch, text
):
    async def show(u, c):
        assert current_user_id.get() == 42
        assert current_chat_id.get() == 42

    show_mock = AsyncMock(side_effect=show)
    model = AsyncMock()
    monkeypatch.setattr(bot, "show_week", show_mock)
    monkeypatch.setattr(bot.agent, "chat", model)
    previous = current_chat_id.get()
    await bot.handle_message(update(text=text), context)
    show_mock.assert_awaited_once()
    model.assert_not_awaited()
    assert current_chat_id.get() == previous


@pytest.mark.asyncio
async def test_page_navigation_edits_existing_list(context, identity):
    context.bot_data["weekly_flight_lists"] = {
        "token": {
            "scope": (42, None),
            "expires": monotonic() + 60,
            "flights": [flight(i) for i in range(20)],
            "start": "2026-11-11",
            "end": "2026-11-17",
            "complete": False,
        }
    }
    u = update(data="wk:token:1")
    await browser.handle_selection(u, context)
    assert "Страница 2/2" in u.callback_query.edit_message_text.call_args.args[0]
    assert "неполное" in u.callback_query.edit_message_text.call_args.args[0]
    u.effective_message.reply_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_card_refreshes_exact_leg_then_fetches_crew_and_weather(
    identity, monkeypatch
):
    first, second = flight(111), flight(222, departure="UACC")
    monkeypatch.setattr(
        flight_card,
        "get_flight_schedule",
        AsyncMock(return_value={"flights": [first, second], "complete": True}),
    )
    crew = AsyncMock(return_value={"crew": [{"crewMemberList": []}]})
    weather = AsyncMock(return_value={"answer": "UACC and UAAA METAR"})
    monkeypatch.setattr(flight_card, "get_flight_crew", crew)
    monkeypatch.setattr(flight_card, "selected_flight_weather", weather)
    snapshot = await flight_card.load_flight_snapshot(flight(222))
    text = flight_card.render_snapshot(snapshot)
    crew.assert_awaited_once_with(222)
    weather.assert_awaited_once_with(second, "2026-11-11", include_taf=True)
    assert "UACC 1200z → 1330z UAAA" in text
    assert snapshot.blocks["weather"].text == "UACC and UAAA METAR"


@pytest.mark.asyncio
async def test_card_preserves_weather_if_crew_fails(identity, monkeypatch):
    monkeypatch.setattr(
        flight_card,
        "get_flight_schedule",
        AsyncMock(return_value={"flights": [flight(111)], "complete": True}),
    )
    monkeypatch.setattr(
        flight_card, "get_flight_crew", AsyncMock(side_effect=RuntimeError())
    )
    monkeypatch.setattr(
        flight_card,
        "selected_flight_weather",
        AsyncMock(return_value={"answer": "RAW METAR"}),
    )
    snapshot = await flight_card.load_flight_snapshot(flight(111))
    text = flight_card.render_snapshot(snapshot)
    assert "CREW" not in text and "RuntimeError" not in text
    assert snapshot.blocks["weather"].text == "RAW METAR"


@pytest.mark.asyncio
async def test_removed_flight_does_not_show_stale_crew(identity, monkeypatch):
    monkeypatch.setattr(
        flight_card,
        "get_flight_schedule",
        AsyncMock(return_value={"flights": [], "complete": True}),
    )
    crew = AsyncMock()
    monkeypatch.setattr(flight_card, "get_flight_crew", crew)
    with pytest.raises(ValueError):
        await flight_card.load_flight_snapshot(flight(111))
    crew.assert_not_awaited()


def test_home_keyboard_persists():
    keyboard = home_keyboard()
    assert keyboard.is_persistent is True
    assert keyboard.one_time_keyboard is False
    assert keyboard.keyboard[0][0].text == SCHEDULE_BUTTON


@pytest.mark.asyncio
async def test_start_shows_compact_default_keyboard(context):
    u = update(text="/start@OPS_bot")
    await bot.handle_message(u, context)
    u.effective_message.reply_text.assert_awaited_once()
    assert (
        u.effective_message.reply_text.call_args.kwargs["reply_markup"]
        == home_keyboard()
    )


def test_real_crew_shape_shows_name_and_position_without_contacts():
    from services.crew_format import format_flight_crew

    text = format_flight_crew(
        "ABC1265",
        "2026-11-11",
        [
            {
                "flightNid": 111,
                "crewMemberList": [
                    {
                        "login": {"loginNid": 123},
                        "contact": {
                            "name": "Test",
                            "surname": "Pilot",
                            "emailWork": "hidden@example.test",
                        },
                        "position": {"name": "CPT"},
                    }
                ],
            }
        ],
    )
    assert "CPT: Test Pilot" in text
    assert "hidden@example.test" not in text


@pytest.mark.asyncio
async def test_search_keeps_ambiguous_matches_as_exact_flight_buttons(
    context, identity, monkeypatch
):
    query = AsyncMock(
        return_value={
            "flights": [flight(111), flight(222), flight(333, number="ABC999")],
            "complete": True,
        }
    )
    monkeypatch.setattr(browser, "get_flight_schedule", query)
    u = update()
    await browser.show_week(u, context, search="ABC1265 на 2026-11-11")
    markup = u.effective_message.reply_text.call_args.kwargs["reply_markup"]
    buttons = [
        b
        for row in markup.inline_keyboard
        for b in row
        if b.callback_data.startswith("fl:")
    ]
    assert len(buttons) == 2
    assert "Поиск:" in u.effective_message.reply_text.call_args.args[0]


@pytest.mark.asyncio
async def test_ai_does_not_block_back_navigation_or_overwrite_new_screen(
    context, identity, monkeypatch
):
    import asyncio

    from services import ai_analysis

    snapshot = FlightSnapshot(flight(111))
    context.bot_data["weekly_flight_lists"] = {
        "token": {
            "scope": (42, None),
            "expires": monotonic() + 60,
            "flights": [flight(111)],
            "snapshots": {0: snapshot},
            "start": "2026-11-11",
            "end": "2026-11-17",
            "complete": True,
        }
    }
    started, finish = asyncio.Event(), asyncio.Event()

    async def analyze(*args):
        started.set()
        await finish.wait()
        return "AI result"

    monkeypatch.setattr(ai_analysis, "analyze_sources", analyze)
    ai = update(data="fc:token:0:ai:0")
    task = asyncio.create_task(browser.handle_selection(ai, context))
    await asyncio.wait_for(started.wait(), timeout=1)
    back = update(data="wk:token:0")
    await asyncio.wait_for(browser.handle_selection(back, context), timeout=1)
    finish.set()
    await task
    ai.callback_query.edit_message_text.assert_not_awaited()
    back.callback_query.edit_message_text.assert_awaited_once()
