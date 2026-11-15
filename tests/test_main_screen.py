import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram import MessageEntity
from telegram.error import BadRequest, TimedOut

from bot import bot, flight_browser
from bot.keyboards import home_keyboard
from bot.main_screen import MainScreen, screen_message_id, text_pages
from tests.test_bot_commands import update_for


@pytest.fixture
def screen_context():
    next_id = 100

    async def send(**kwargs):
        nonlocal next_id
        next_id += 1
        return SimpleNamespace(message_id=next_id, **kwargs)

    async def edit(**kwargs):
        return SimpleNamespace(**kwargs)

    return SimpleNamespace(
        user_data={},
        bot_data={},
        bot=SimpleNamespace(
            send_message=AsyncMock(side_effect=send),
            edit_message_text=AsyncMock(side_effect=edit),
            delete_message=AsyncMock(),
            send_chat_action=AsyncMock(),
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


@pytest.mark.asyncio
async def test_sections_edit_one_message_and_keyboard_is_unchanged(screen_context):
    ctx = screen_context
    await bot.screen_message_handler(update_for("/start"), ctx)
    main_id = screen_message_id(42, 42)
    assert ctx.bot.send_message.await_count == 2
    carrier = ctx.bot.send_message.call_args_list[0].kwargs
    assert carrier["reply_markup"] == home_keyboard()
    ctx.bot.delete_message.assert_not_awaited()
    for text in ["FLEET DOCS", "ANALYTICS", "GROUND STATUS", "MENU"]:
        await bot.screen_message_handler(update_for(text), ctx)
        assert ctx.bot.edit_message_text.call_args.kwargs["message_id"] == main_id
    assert ctx.bot.send_message.await_count == 2


@pytest.mark.asyncio
async def test_screen_id_survives_context_restart(screen_context):
    await bot.screen_message_handler(update_for("FLEET DOCS"), screen_context)
    main_id = screen_message_id(42, 42)
    screen_context.user_data.clear()
    await bot.screen_message_handler(update_for("ANALYTICS"), screen_context)
    assert (
        screen_context.bot.edit_message_text.call_args.kwargs["message_id"] == main_id
    )
    assert screen_context.bot.send_message.await_count == 2


@pytest.mark.asyncio
async def test_start_restores_reply_keyboard_for_existing_screen(screen_context):
    ctx = screen_context
    await bot.screen_message_handler(update_for("/start"), ctx)
    main_id = screen_message_id(42, 42)
    await bot.screen_message_handler(update_for("/start"), ctx)
    assert (
        ctx.bot.send_message.call_args_list[-2].kwargs["reply_markup"]
        == home_keyboard()
    )
    assert screen_message_id(42, 42) != main_id
    assert ctx.bot.send_message.await_count == 4
    ctx.bot.edit_message_text.assert_not_awaited()
    ctx.bot.delete_message.assert_awaited_once_with(chat_id=42, message_id=main_id)


@pytest.mark.asyncio
async def test_start_after_deleted_chat_recreates_visible_screen(screen_context):
    ctx = screen_context
    await bot.screen_message_handler(update_for("/prepare"), ctx)
    old_id = screen_message_id(42, 42)
    # Telegram may retain the old message server-side, so edits would succeed.
    # Deletion of a cleared message can also fail: neither should block /start.
    ctx.bot.delete_message.side_effect = BadRequest("Message to delete not found")
    await bot.screen_message_handler(update_for("/start"), ctx)
    new_id = screen_message_id(42, 42)
    assert new_id != old_id
    assert ctx.bot.send_message.call_args.kwargs["text"].startswith("OPS Bot")
    assert (
        ctx.bot.send_message.call_args_list[-2].kwargs["reply_markup"]
        == home_keyboard()
    )
    assert not ctx.user_data.get("operation_inputs")
    ctx.bot.edit_message_text.assert_not_awaited()
    await bot.screen_message_handler(update_for("FLEET DOCS"), ctx)
    assert ctx.bot.edit_message_text.call_args.kwargs["message_id"] == new_id
    stale = callback("op:menu", old_id)
    await bot.screen_button_handler(stale, ctx)
    assert stale.callback_query.answer.call_args.kwargs["show_alert"]


@pytest.mark.asyncio
async def test_schedule_loads_and_replaces_loading_in_same_message(
    screen_context, monkeypatch
):
    ctx = screen_context
    await bot.screen_message_handler(update_for("/start"), ctx)
    main_id = screen_message_id(42, 42)

    async def schedule(*args):
        assert ctx.bot.edit_message_text.call_args.kwargs["text"] == "LOADING…"
        return {"flights": [], "complete": True}

    monkeypatch.setattr(flight_browser, "get_flight_schedule", schedule)
    await bot.screen_message_handler(update_for("SKED"), ctx)
    edits = ctx.bot.edit_message_text.call_args_list
    assert edits[-2].kwargs["text"] == "LOADING…"
    assert "(UTC)" in edits[-1].kwargs["text"]
    assert all(call.kwargs["message_id"] == main_id for call in edits)
    assert ctx.bot.send_message.await_count == 2


@pytest.mark.asyncio
async def test_fleet_docs_inline_transition_edits_main_message(screen_context):
    ctx = screen_context
    await bot.screen_message_handler(update_for("FLEET DOCS"), ctx)
    main_id = screen_message_id(42, 42)
    event = callback("docs:aircraft", main_id)
    await bot.screen_button_handler(event, ctx)
    assert "AIRCRAFT:" in ctx.bot.edit_message_text.call_args.kwargs["text"]
    assert ctx.bot.edit_message_text.call_args.kwargs["message_id"] == main_id
    event.effective_message.reply_text.assert_not_awaited()
    event.callback_query.edit_message_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_deleted_screen_is_recreated_but_network_errors_are_not(screen_context):
    ctx = screen_context
    await bot.screen_message_handler(update_for("/start"), ctx)
    old_id = screen_message_id(42, 42)
    ctx.bot.edit_message_text.side_effect = BadRequest("Message to edit not found")
    await bot.screen_message_handler(update_for("FLEET DOCS"), ctx)
    assert screen_message_id(42, 42) != old_id
    assert ctx.bot.send_message.await_count == 3
    ctx.bot.edit_message_text.side_effect = TimedOut()
    with pytest.raises(TimedOut):
        await bot.screen_message_handler(update_for("FLEET DOCS"), ctx)
    assert ctx.bot.send_message.await_count == 3


@pytest.mark.asyncio
async def test_identical_edit_does_not_create_new_message(screen_context):
    ctx = screen_context
    await bot.screen_message_handler(update_for("FLEET DOCS"), ctx)
    ctx.bot.edit_message_text.side_effect = BadRequest("Message is not modified")
    await bot.screen_message_handler(update_for("FLEET DOCS"), ctx)
    assert ctx.bot.send_message.await_count == 2


@pytest.mark.asyncio
async def test_callbacks_from_old_messages_are_rejected(screen_context):
    ctx = screen_context
    await bot.screen_message_handler(update_for("/start"), ctx)
    event = callback("docs:aircraft", 50)
    await bot.screen_button_handler(event, ctx)
    assert event.callback_query.answer.call_args.kwargs["show_alert"]
    ctx.bot.edit_message_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_long_reports_page_inline_without_extra_messages(
    screen_context, monkeypatch
):
    ctx = screen_context
    monkeypatch.setattr(
        bot, "execute_command", AsyncMock(return_value="A" * 3900 + "B" * 500)
    )
    await bot.screen_message_handler(update_for("/report ABC1265"), ctx)
    main_id = screen_message_id(42, 42)
    assert ctx.bot.edit_message_text.call_args.kwargs["text"] == "A" * 3900
    state = ctx.user_data["main_screen_pages"]
    await bot.screen_button_handler(
        callback(f"screen:{state['token']}:1", main_id), ctx
    )
    assert ctx.bot.edit_message_text.call_args.kwargs["text"] == "B" * 500
    assert ctx.bot.send_message.await_count == 2
    await bot.screen_message_handler(update_for("FLEET DOCS"), ctx)
    stale = callback(f"screen:{state['token']}:0", main_id)
    await bot.screen_button_handler(stale, ctx)
    assert stale.callback_query.answer.call_args.kwargs["show_alert"]


@pytest.mark.asyncio
async def test_prompt_plain_input_cancel_and_result_use_main_screen(
    screen_context, monkeypatch
):
    ctx = screen_context
    await bot.screen_message_handler(update_for("/prepare"), ctx)
    main_id = screen_message_id(42, 42)
    assert ctx.user_data["operation_inputs"][(42, None)]["message_id"] == main_id
    execute = AsyncMock(return_value="Prepared")
    monkeypatch.setattr(bot, "execute_command", execute)
    await bot.screen_message_handler(update_for("ABC1265"), ctx)
    execute.assert_awaited_once_with("prepare", "ABC1265")
    assert ctx.bot.edit_message_text.call_args.kwargs["text"] == "Prepared"
    await bot.screen_message_handler(update_for("/prepare"), ctx)
    await bot.screen_button_handler(callback("op:cancel", main_id), ctx)
    assert not ctx.user_data["operation_inputs"]
    assert ctx.bot.send_message.await_count == 2


@pytest.mark.asyncio
async def test_two_users_have_independent_screens(screen_context, monkeypatch):
    monkeypatch.setattr(bot, "is_allowed", lambda update: True)
    monkeypatch.setattr("bot.admission.is_allowed", lambda update: True)
    await bot.screen_message_handler(update_for("FLEET DOCS", user=42), screen_context)
    await bot.screen_message_handler(update_for("FLEET DOCS", user=43), screen_context)
    assert screen_message_id(42, 42) != screen_message_id(43, 43)


@pytest.mark.asyncio
async def test_unauthorized_user_does_not_create_screen(screen_context):
    await bot.screen_message_handler(update_for("FLEET DOCS", user=99), screen_context)
    screen_context.bot.send_message.assert_not_awaited()
    assert screen_message_id(99, 99) is None


@pytest.mark.asyncio
async def test_screen_operations_are_serialized(screen_context, monkeypatch):
    ctx = screen_context
    entered, release = asyncio.Event(), asyncio.Event()

    async def execute(*args):
        entered.set()
        await release.wait()
        return "Report"

    monkeypatch.setattr(bot, "execute_command", execute)
    first = asyncio.create_task(
        bot.screen_message_handler(update_for("/report ABC1265"), ctx)
    )
    await asyncio.wait_for(entered.wait(), 1)
    second = asyncio.create_task(
        bot.screen_message_handler(update_for("FLEET DOCS"), ctx)
    )
    try:
        await asyncio.sleep(0)
        assert ctx.bot.send_message.call_args.kwargs["text"] == "LOADING…"
    finally:
        release.set()
        await asyncio.gather(first, second)
    assert ctx.bot.edit_message_text.call_args.kwargs["text"].startswith("FLEET DOCS")
    assert ctx.bot.send_message.await_count == 2


@pytest.mark.asyncio
async def test_utf16_pagination_preserves_entities(screen_context):
    text = "😀" * 2200
    assert "".join(page for page, _ in text_pages(text)) == text
    screen = MainScreen(update_for("MENU"), screen_context)
    await screen.render(text, entities=[MessageEntity("bold", 0, 4400)])
    state = screen_context.user_data["main_screen_pages"]
    await screen.page(state, 1)
    kwargs = screen_context.bot.edit_message_text.call_args.kwargs
    assert len(kwargs["text"].encode("utf-16-le")) // 2 <= 3900
    assert kwargs["entities"][0].offset == 0
    assert kwargs["entities"][0].length == 500


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [ValueError("Invalid date"), TimeoutError()])
async def test_loading_is_replaced_by_error_in_same_screen(
    screen_context, monkeypatch, error
):
    ctx = screen_context
    monkeypatch.setattr(bot, "execute_command", AsyncMock(side_effect=error))
    await bot.screen_message_handler(update_for("/report ABC1265"), ctx)
    assert ctx.bot.send_message.call_args.kwargs["text"] == "LOADING…"
    last = ctx.bot.edit_message_text.call_args.kwargs
    assert last["text"] != "LOADING…"
    assert last["message_id"] == screen_message_id(42, 42)
    assert ctx.bot.send_message.await_count == 2


def test_application_registers_main_screen_handlers(monkeypatch):
    from pydantic import SecretStr

    from config.settings import settings

    monkeypatch.setattr(settings, "telegram_bot_token", SecretStr("123:test"))
    application = bot.create_application()
    callbacks = [handler.callback for handler in application.handlers[0]]
    assert bot.screen_message_handler in callbacks
    assert bot.screen_button_handler in callbacks
    assert bot.handle_message not in callbacks
