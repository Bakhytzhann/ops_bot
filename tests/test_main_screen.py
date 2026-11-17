"""Transient single-screen navigation and callback behavior."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, MessageEntity
from telegram.error import BadRequest, TimedOut

from bot import admission, bot, flight_browser
from bot.keyboards import home_keyboard
from bot.main_screen import MainScreen, dispatch_screen, text_pages
from services.snapshot_models import FlightSnapshot
from tests.test_bot_commands import update_for
from tests.test_weekly_browser import flight


@pytest.fixture
def screen_context(monkeypatch):
    monkeypatch.setattr(admission, "is_allowed", lambda update: True)
    monkeypatch.setattr(bot, "is_allowed", lambda update: True)
    next_id = 100

    async def send(**kwargs):
        nonlocal next_id
        next_id += 1
        return SimpleNamespace(message_id=next_id, **kwargs)

    return SimpleNamespace(
        user_data={},
        bot_data={},
        bot=SimpleNamespace(
            send_message=AsyncMock(side_effect=send),
            edit_message_text=AsyncMock(return_value=SimpleNamespace(message_id=102)),
            send_chat_action=AsyncMock(),
            delete_my_commands=AsyncMock(),
            delete_message=AsyncMock(),
        ),
    )


def callback(data, message_id):
    event = update_for(None)
    event.effective_message.message_id = message_id
    event.callback_query = SimpleNamespace(
        data=data,
        message=event.effective_message,
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )
    return event


def test_utf16_pagination():
    text = "😀" * 2200
    pages = text_pages(text)
    assert "".join(page for page, _ in pages) == text
    assert all(len(page.encode("utf-16-le")) // 2 <= 3900 for page, _ in pages)


@pytest.mark.asyncio
async def test_start_installs_keyboard_then_one_editable_screen(screen_context):
    ctx = screen_context
    await bot.screen_message_handler(update_for("/start"), ctx)
    assert ctx.bot.send_message.await_count == 1
    assert (
        ctx.bot.send_message.call_args_list[0].kwargs["reply_markup"] == home_keyboard()
    )
    assert "main_screen_message_id" not in ctx.user_data
    await bot.screen_message_handler(update_for("FLEET DOCS"), ctx)
    assert (
        ctx.bot.edit_message_text.call_args.kwargs["message_id"]
        == ctx.user_data["main_screen_message_id"]
    )
    assert ctx.bot.send_message.await_count == 2


@pytest.mark.asyncio
async def test_all_home_actions_share_screen_without_inline_menu(screen_context):
    ctx = screen_context
    await bot.screen_message_handler(update_for("/start"), ctx)
    for title in (
        "GROUND STATUS",
        "OPS BRIEF",
        "ANALYTICS",
        "CHARTER CHECK",
        "FLEET DOCS",
    ):
        await bot.screen_message_handler(update_for(title), ctx)
        markup = ctx.bot.edit_message_text.call_args.kwargs["reply_markup"]
        assert all(
            button.callback_data != "op:menu"
            for row in markup.inline_keyboard
            for button in row
        )
    assert ctx.bot.send_message.await_count == 6


@pytest.mark.asyncio
async def test_fleet_docs_click_edits_same_message(screen_context):
    ctx = screen_context
    await bot.screen_message_handler(update_for("FLEET DOCS"), ctx)
    screen_id = ctx.user_data["main_screen_message_id"]
    event = callback("docs:aircraft", screen_id)
    await bot.screen_button_handler(event, ctx)
    assert "AIRCRAFT" in ctx.bot.edit_message_text.call_args.kwargs["text"]
    assert ctx.bot.edit_message_text.call_args.kwargs["message_id"] == screen_id


@pytest.mark.asyncio
async def test_docs_back_returns_to_docs_root(screen_context):
    ctx = screen_context
    await bot.screen_message_handler(update_for("FLEET DOCS"), ctx)
    screen_id = ctx.user_data["main_screen_message_id"]
    await bot.screen_button_handler(callback("docs:checklists", screen_id), ctx)
    assert "PRINTABLE CHECKLISTS" in ctx.bot.edit_message_text.call_args.kwargs["text"]
    await bot.screen_button_handler(callback("screen:back", screen_id), ctx)
    assert (
        ctx.bot.edit_message_text.call_args.kwargs["text"]
        == "FLEET DOCS · выберите раздел"
    )


@pytest.mark.asyncio
async def test_schedule_keeps_previous_context_until_data_arrives(
    screen_context, monkeypatch
):
    ctx = screen_context
    await bot.screen_message_handler(update_for("/start"), ctx)
    await bot.screen_message_handler(update_for("FLEET DOCS"), ctx)
    before = ctx.bot.edit_message_text.await_count

    async def schedule(*args):
        # Old data is still visible; a separate processing message was sent.
        assert ctx.bot.edit_message_text.await_count == before
        assert (
            ctx.bot.send_message.call_args.kwargs["text"] == "SKED · Обработка запроса…"
        )
        return {"flights": [], "complete": True}

    monkeypatch.setattr(flight_browser, "get_flight_schedule", schedule)
    await bot.screen_message_handler(update_for("SKED"), ctx)
    assert "(UTC)" in ctx.bot.edit_message_text.call_args.kwargs["text"]


@pytest.mark.asyncio
async def test_one_step_back_restores_prior_screen(screen_context):
    ctx = screen_context
    await bot.screen_message_handler(update_for("/start"), ctx)
    await bot.screen_message_handler(update_for("FLEET DOCS"), ctx)
    await bot.screen_message_handler(update_for("ANALYTICS"), ctx)
    screen_id = ctx.user_data["main_screen_message_id"]
    await bot.screen_button_handler(callback("screen:back", screen_id), ctx)
    assert "FLEET DOCS" in ctx.bot.edit_message_text.call_args.kwargs["text"]
    assert not ctx.user_data.get("screen_back")


@pytest.mark.asyncio
async def test_previous_request_does_not_block_or_overwrite_newer(screen_context):
    ctx = screen_context
    await bot.screen_message_handler(update_for("/start"), ctx)
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow(update, _):
        entered.set()
        await release.wait()
        await update.effective_message.reply_text("OLD")

    async def fast(update, _):
        await update.effective_message.reply_text("NEW")

    first = asyncio.create_task(dispatch_screen(update_for("SKED"), ctx, slow))
    await entered.wait()
    await dispatch_screen(update_for("FLEET DOCS"), ctx, fast)
    release.set()
    await first
    assert ctx.bot.edit_message_text.call_args.kwargs["text"] == "NEW"


@pytest.mark.asyncio
async def test_old_callback_and_old_page_are_rejected(screen_context):
    ctx = screen_context
    await bot.screen_message_handler(update_for("FLEET DOCS"), ctx)
    current_id = ctx.user_data["main_screen_message_id"]
    stale = callback("docs:aircraft", current_id + 7)
    await bot.screen_button_handler(stale, ctx)
    assert stale.callback_query.answer.call_args.kwargs["show_alert"] is True
    stale_page = callback("screen:wrong:0", current_id)
    await bot.screen_button_handler(stale_page, ctx)
    assert stale_page.callback_query.answer.call_args.kwargs["show_alert"] is True


@pytest.mark.asyncio
async def test_main_screen_does_not_append_duplicate_menu(screen_context):
    ctx = screen_context
    ctx.user_data.update(main_screen_message_id=123, screen_generation=0)
    screen = MainScreen(update_for("SKED"), ctx)
    markup = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Меню", callback_data="op:menu")],
            [InlineKeyboardButton("Action", callback_data="docs:aircraft")],
        ]
    )
    await screen.render("TEST", reply_markup=markup)
    buttons = [
        button.callback_data
        for row in ctx.bot.edit_message_text.call_args.kwargs[
            "reply_markup"
        ].inline_keyboard
        for button in row
    ]
    assert buttons == ["docs:aircraft"]


@pytest.mark.asyncio
async def test_restart_uses_new_transient_screen(screen_context):
    ctx = screen_context
    await bot.screen_message_handler(update_for("/start"), ctx)
    await bot.screen_message_handler(update_for("FLEET DOCS"), ctx)
    old = ctx.user_data["main_screen_message_id"]
    await bot.screen_message_handler(update_for("/start"), ctx)
    assert "main_screen_message_id" not in ctx.user_data
    await bot.screen_message_handler(update_for("FLEET DOCS"), ctx)
    assert ctx.user_data["main_screen_message_id"] != old
    assert ctx.bot.send_message.await_count == 4


@pytest.mark.asyncio
async def test_deleted_screen_is_recreated_and_timeout_is_not_masked(screen_context):
    ctx = screen_context
    await bot.screen_message_handler(update_for("/start"), ctx)
    await bot.screen_message_handler(update_for("FLEET DOCS"), ctx)
    old = ctx.user_data["main_screen_message_id"]
    ctx.bot.edit_message_text.side_effect = BadRequest("Message to edit not found")
    await bot.screen_message_handler(update_for("FLEET DOCS"), ctx)
    assert ctx.user_data["main_screen_message_id"] != old
    ctx.bot.edit_message_text.side_effect = TimedOut()
    with pytest.raises(TimedOut):
        await bot.screen_message_handler(update_for("FLEET DOCS"), ctx)


@pytest.mark.asyncio
async def test_entity_pagination_offsets(screen_context):
    ctx = screen_context
    screen = MainScreen(update_for("SKED"), ctx)
    text = "😀" * 2200
    await screen.render(text, entities=[MessageEntity("bold", 0, 4400)])
    await screen.page(ctx.user_data["main_screen_pages"], 1)
    entity = ctx.bot.edit_message_text.call_args.kwargs["entities"][0]
    assert entity.offset == 0
    assert entity.length == 500


@pytest.mark.asyncio
async def test_actual_schedule_request_is_superseded_by_docs(
    screen_context, monkeypatch
):
    ctx = screen_context
    await bot.screen_message_handler(update_for("/start"), ctx)
    entered, release = asyncio.Event(), asyncio.Event()

    async def schedule(*args):
        entered.set()
        await release.wait()
        return {"flights": [flight(1)], "complete": True}

    monkeypatch.setattr(flight_browser, "get_flight_schedule", schedule)
    first = asyncio.create_task(bot.screen_message_handler(update_for("SKED"), ctx))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.wait_for(
            bot.screen_message_handler(update_for("FLEET DOCS"), ctx), 1
        )
        before = ctx.bot.edit_message_text.await_count
    finally:
        release.set()
        await first
    assert ctx.bot.edit_message_text.await_count == before
    assert ctx.bot.edit_message_text.call_args.kwargs["text"].startswith("FLEET DOCS")


@pytest.mark.asyncio
async def test_rapid_flight_selection_preserves_latest_screen_and_identity(
    screen_context, monkeypatch
):
    ctx = screen_context
    monkeypatch.setattr(
        flight_browser,
        "get_flight_schedule",
        AsyncMock(
            return_value={
                "flights": [flight(1, "ABC001"), flight(2, "ABC002")],
                "complete": True,
            }
        ),
    )
    await bot.screen_message_handler(update_for("SKED"), ctx)
    token = next(iter(ctx.bot_data["weekly_flight_lists"]))
    screen_id = ctx.user_data["main_screen_message_id"]
    entered, release = asyncio.Event(), asyncio.Event()

    async def load(selected, **kwargs):
        if selected["flightNid"] == 1:
            entered.set()
            await release.wait()
        return FlightSnapshot(selected)

    monkeypatch.setattr(flight_browser, "load_flight_snapshot", load)
    first = asyncio.create_task(
        bot.screen_button_handler(callback(f"fl:{token}:0", screen_id), ctx)
    )
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.wait_for(
            bot.screen_button_handler(callback(f"fl:{token}:1", screen_id), ctx), 1
        )
        assert "ABC002" in ctx.bot.edit_message_text.call_args.kwargs["text"]
        before = ctx.bot.edit_message_text.await_count
    finally:
        release.set()
        await first
    assert ctx.bot.edit_message_text.await_count == before
    assert ctx.user_data["flight_navigation"][(42, None)]["selected_flight_id"] == 2


@pytest.mark.asyncio
async def test_overlapping_initial_requests_create_only_one_editable_screen(
    screen_context,
):
    ctx = screen_context
    original_send = ctx.bot.send_message.side_effect
    entered, release = asyncio.Event(), asyncio.Event()

    async def send(**kwargs):
        if kwargs["text"] == "OLD":
            entered.set()
            await release.wait()
        return await original_send(**kwargs)

    ctx.bot.send_message.side_effect = send

    async def handler(update, context):
        await update.effective_message.reply_text(update.effective_message.text)

    first = asyncio.create_task(dispatch_screen(update_for("OLD"), ctx, handler))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        second = asyncio.create_task(dispatch_screen(update_for("NEW"), ctx, handler))
        await asyncio.sleep(0)
    finally:
        release.set()
        await asyncio.gather(first, second)
    assert ctx.bot.send_message.await_count == 2
    assert ctx.bot.edit_message_text.call_args.kwargs["text"] == "NEW"
    assert (
        ctx.bot.edit_message_text.call_args.kwargs["message_id"]
        == ctx.user_data["main_screen_message_id"]
    )


@pytest.mark.asyncio
async def test_transient_state_after_process_restart_creates_new_screen(screen_context):
    ctx = screen_context
    await bot.screen_message_handler(update_for("FLEET DOCS"), ctx)
    old_id = ctx.user_data["main_screen_message_id"]
    ctx.user_data.clear()
    await bot.screen_message_handler(update_for("FLEET DOCS"), ctx)
    assert ctx.user_data["main_screen_message_id"] != old_id
    assert ctx.bot.send_message.await_count == 4


@pytest.mark.asyncio
async def test_denied_requests_do_not_create_screen(screen_context, monkeypatch):
    monkeypatch.setattr(admission, "is_allowed", lambda update: False)
    monkeypatch.setattr(bot, "is_allowed", lambda update: False)
    await bot.screen_message_handler(update_for("FLEET DOCS"), screen_context)
    screen_context.bot.send_message.assert_not_awaited()
    assert "main_screen_message_id" not in screen_context.user_data


@pytest.mark.asyncio
async def test_home_command_deleted_and_old_content_survives_processing(
    screen_context, monkeypatch
):
    ctx = screen_context
    await bot.screen_message_handler(update_for("FLEET DOCS"), ctx)
    old_screen = ctx.user_data["main_screen_message_id"]
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow(*args):
        entered.set()
        await release.wait()
        return {"flights": [], "complete": True}

    monkeypatch.setattr(flight_browser, "get_flight_schedule", slow)
    event = update_for("SKED")
    event.effective_message.message_id = 42
    task = asyncio.create_task(bot.screen_message_handler(event, ctx))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert (
            ctx.bot.send_message.call_args.kwargs["text"] == "SKED · Обработка запроса…"
        )
        assert ctx.user_data["last_completed_screen_id"] == old_screen
        assert not any(
            call.kwargs.get("message_id") == old_screen
            for call in ctx.bot.delete_message.await_args_list
        )
        assert any(
            call.kwargs.get("message_id") == 42
            for call in ctx.bot.delete_message.await_args_list
        )
    finally:
        release.set()
        await task
    assert any(
        call.kwargs.get("message_id") == old_screen
        for call in ctx.bot.delete_message.await_args_list
    )


@pytest.mark.asyncio
async def test_stale_loading_never_replaces_latest_selection(
    screen_context, monkeypatch
):
    ctx = screen_context
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow(*args):
        entered.set()
        await release.wait()
        return {"flights": [], "complete": True}

    monkeypatch.setattr(flight_browser, "get_flight_schedule", slow)
    first = asyncio.create_task(bot.screen_message_handler(update_for("SKED"), ctx))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await bot.screen_message_handler(update_for("FLEET DOCS"), ctx)
        latest = ctx.user_data["main_screen_message_id"]
        assert "FLEET DOCS" in ctx.bot.edit_message_text.call_args.kwargs["text"]
    finally:
        release.set()
        await first
    assert ctx.user_data["main_screen_message_id"] == latest
    assert ctx.bot.edit_message_text.call_args.kwargs["message_id"] == latest
