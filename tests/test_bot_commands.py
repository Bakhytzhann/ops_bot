from time import perf_counter
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot import bot
from bot.commands import (
    COMMANDS,
    MENU_GROUPS,
    help_text,
    menu_keyboard,
    natural_foreflight_command,
)
from bot.keyboards import home_keyboard
from config.settings import settings


def update_for(text, user=42, thread=None, reply=None, chat=None):
    message = SimpleNamespace(
        text=text,
        message_thread_id=thread,
        reply_to_message=reply,
        reply_text=AsyncMock(return_value=SimpleNamespace(message_id=900)),
    )
    return SimpleNamespace(
        effective_message=message,
        effective_user=SimpleNamespace(id=user, is_bot=False, first_name="User"),
        effective_chat=SimpleNamespace(id=chat or user, type="private"),
    )


@pytest.fixture
def context(monkeypatch):
    monkeypatch.setattr(settings, "telegram_allowed_user_ids", "42")
    monkeypatch.setattr(bot.agent, "chat", AsyncMock(return_value="AI answer"))
    return SimpleNamespace(
        user_data={}, bot=SimpleNamespace(send_chat_action=AsyncMock())
    )


@pytest.mark.asyncio
async def test_help_and_menu_use_no_model(context):
    update = update_for("/menu@OPS_bot")
    await bot.handle_message(update, context)
    assert "SKED" in update.effective_message.reply_text.call_args.args[0]
    update.effective_message.reply_text.assert_awaited_once()
    assert (
        update.effective_message.reply_text.call_args.kwargs["reply_markup"]
        == home_keyboard()
    )
    bot.agent.chat.assert_not_awaited()


@pytest.mark.parametrize("action", [name for name in COMMANDS if name != "search"])
@pytest.mark.asyncio
async def test_every_explicit_operation_bypasses_model(context, monkeypatch, action):
    direct = AsyncMock(return_value="Direct API answer")
    monkeypatch.setattr(bot, "execute_command", direct)
    update = update_for(f"/{action} ABC1265 на 2026-11-11")
    await bot.handle_message(update, context)
    direct.assert_awaited_once_with(action, "ABC1265 на 2026-11-11")
    bot.agent.chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_button_requests_input_then_reply_executes(context, monkeypatch):
    update = update_for(None)
    update.callback_query = SimpleNamespace(data="op:route", answer=AsyncMock())
    await bot.handle_button(update, context)
    assert context.user_data["operation_inputs"][(42, None)]["action"] == "route"
    direct = AsyncMock(return_value="Route answer")
    monkeypatch.setattr(bot, "execute_command", direct)
    reply = update_for("ABC1265 на 2026-11-11", reply=SimpleNamespace(message_id=900))
    await bot.handle_message(reply, context)
    direct.assert_awaited_once_with("route", "ABC1265 на 2026-11-11")
    assert not context.user_data["operation_inputs"]
    bot.agent.chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_pending_input_does_not_capture_other_messages(context):
    await bot.handle_message(update_for("/route"), context)
    for update in (
        update_for("ordinary chat"),
        update_for(
            "reply in another topic", thread=5, reply=SimpleNamespace(message_id=900)
        ),
    ):
        assert bot.accepts_message(update, context)
    other_user_context = SimpleNamespace(user_data={})
    assert bot.accepts_message(
        update_for("other member", user=42, reply=SimpleNamespace(message_id=900)),
        other_user_context,
    )


@pytest.mark.asyncio
async def test_cancel_and_expiry_do_not_call_api(context, monkeypatch):
    direct = AsyncMock()
    monkeypatch.setattr(bot, "execute_command", direct)
    await bot.handle_message(update_for("/briefing"), context)
    await bot.handle_message(update_for("/cancel"), context)
    assert not context.user_data["operation_inputs"]
    context.user_data["operation_inputs"][(42, None)] = {
        "action": "route",
        "message_id": 900,
        "expires": perf_counter() - 1,
    }
    update = update_for("ABC1265 на 2026-11-11", reply=SimpleNamespace(message_id=900))
    await bot.handle_message(update, context)
    assert "истекло" in update.effective_message.reply_text.call_args.args[0]
    direct.assert_not_awaited()


@pytest.mark.asyncio
async def test_buttons_enforce_access_and_group(context):
    for user, chat in ((99, 123), (42, 123)):
        update = update_for(None, user=user, chat=chat)
        update.callback_query = SimpleNamespace(data="op:briefing", answer=AsyncMock())
        await bot.handle_button(update, context)
        assert update.callback_query.answer.call_args.kwargs["show_alert"] is True
        update.effective_message.reply_text.assert_not_awaited()
    assert not context.user_data


@pytest.mark.asyncio
async def test_ai_command_explicitly_calls_model(context):
    await bot.handle_message(update_for("/ai объясни ETE", user=42), context)
    bot.agent.chat.assert_awaited_once_with(
        message="объясни ETE", chat_id=42, user_id=42
    )


@pytest.mark.asyncio
async def test_unknown_command_and_other_bot_never_call_model(context):
    await bot.handle_message(update_for("/unknown"), context)
    other = update_for("/route@OtherBot ABC1265 на 2026-11-11")
    await bot.handle_message(other, context)
    other.effective_message.reply_text.assert_not_awaited()
    bot.agent.chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_direct_api_failure_never_falls_back_to_model(context, monkeypatch):
    from integrations.foreflight.api_client import ForeFlightError

    monkeypatch.setattr(
        bot,
        "execute_command",
        AsyncMock(side_effect=ForeFlightError("unavailable", 503)),
    )
    update = update_for("/briefing ABC1265 на 2026-11-11")
    await bot.handle_message(update, context)
    assert "503" in update.effective_message.reply_text.call_args.args[0]
    bot.agent.chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_explicit_command_allows_other_group_member(context, monkeypatch):
    from bot import commands

    direct = AsyncMock(return_value="Route for member")
    monkeypatch.setattr(commands, "foreflight_operation", direct)
    update = update_for("/route ABC1265 на 2026-11-11", user=42)
    await bot.handle_message(update, context)
    assert "Route for member" in update.effective_message.reply_text.call_args.args[0]
    direct.assert_awaited_once_with("route", "ABC1265", "2026-11-11")
    bot.agent.chat.assert_not_awaited()


def test_help_catalog_matches_buttons_and_commands():
    for name in COMMANDS:
        assert f"/{name}" in help_text()
    callbacks = {
        b.callback_data
        for group in MENU_GROUPS
        for row in menu_keyboard(group).inline_keyboard
        for b in row
    }
    assert callbacks == {f"op:{name}" for name in COMMANDS} | {
        "op:help",
        "op:ai",
        "op:week",
        "op:menu",
    }


def test_explanation_questions_stay_with_model():
    assert natural_foreflight_command("Что такое FIR?") is None
    assert natural_foreflight_command("маршрут ABC1265 на 2026-11-11") == "route"
