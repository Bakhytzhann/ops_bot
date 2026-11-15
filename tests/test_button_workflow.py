import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent.core import agent
from bot import bot
from bot.commands import COMMANDS
from bot.keyboards import HOME_PLACEHOLDERS
from tests.test_bot_commands import update_for


@pytest.fixture
def context(monkeypatch):
    monkeypatch.setattr(agent, "chat", AsyncMock())
    monkeypatch.setattr(agent, "warmup", AsyncMock())
    monkeypatch.setattr(agent.client, "chat", AsyncMock())
    return SimpleNamespace(
        user_data={}, bot_data={}, bot=SimpleNamespace(send_chat_action=AsyncMock())
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        "hello",
        "погода EGLL",
        "@OPS_bot погода EGLL",
        "Flight Watch ABC1265 на 2026-11-11",
        "/ai test",
        "/watch ABC1265 на 2026-11-11",
    ],
)
async def test_unsolicited_text_and_disabled_commands_never_call_ai(
    context, monkeypatch, text
):
    execute = AsyncMock()
    monkeypatch.setattr(bot, "execute_command", execute)
    await bot.handle_message(update_for(text), context)
    execute.assert_not_awaited()
    agent.chat.assert_not_awaited()
    agent.client.chat.assert_not_awaited()
    agent.warmup.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("data", ["op:ai", "op:watch", "op:group_watch"])
async def test_disabled_buttons_cannot_reach_tools(context, monkeypatch, data):
    prompt = AsyncMock()
    monkeypatch.setattr(bot, "prompt_for_input", prompt)
    update = update_for(None)
    update.callback_query = SimpleNamespace(data=data, answer=AsyncMock())
    await bot.handle_button(update, context)
    prompt.assert_not_awaited()
    assert update.callback_query.answer.called
    agent.chat.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("label", sorted(HOME_PLACEHOLDERS))
async def test_home_placeholders_do_not_call_operations(context, label):
    update = update_for(label)
    await bot.handle_message(update, context)
    assert "пока не подключена" in update.effective_message.reply_text.call_args.args[0]
    agent.chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_startup_only_configures_telegram(context, monkeypatch):
    monkeypatch.setattr(bot, "BOT_USERNAME", "OPS_bot")
    telegram = SimpleNamespace(username="OPS_bot", delete_my_commands=AsyncMock())
    await bot.setup_telegram(SimpleNamespace(bot=telegram))
    assert telegram.delete_my_commands.await_count == 2
    assert telegram.delete_my_commands.call_args_list[0].kwargs == {}
    assert (
        telegram.delete_my_commands.call_args_list[1].kwargs["scope"].type
        == "all_private_chats"
    )
    agent.warmup.assert_not_awaited()
    agent.client.chat.assert_not_awaited()


def test_runtime_import_does_not_load_agent_or_ollama():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import bot.bot; assert 'agent.core' not in sys.modules; assert 'ollama' not in sys.modules",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_commands_have_no_ai_or_flight_watch():
    assert not {"ai", "watch"} & COMMANDS.keys()


@pytest.mark.asyncio
async def test_retired_menu_button_is_ignored_without_loading_schedule(
    context, monkeypatch
):
    schedule = AsyncMock()
    monkeypatch.setattr(bot, "weekly_flow", schedule)
    update = update_for("MENU")
    await bot.handle_message(update, context)
    update.effective_message.reply_text.assert_not_awaited()
    schedule.assert_not_awaited()


@pytest.mark.asyncio
async def test_fleet_docs_opens_categories(context):
    from bot.fleet_docs import fleet_docs_keyboard

    update = update_for("FLEET DOCS")
    await bot.handle_message(update, context)
    assert (
        update.effective_message.reply_text.call_args.kwargs["reply_markup"]
        == fleet_docs_keyboard()
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("category", ["aircraft", "operator", "checklists"])
async def test_fleet_docs_categories_show_empty_state(context, category):
    update = update_for(None)
    update.callback_query = SimpleNamespace(data=f"docs:{category}", answer=AsyncMock())
    await bot.handle_button(update, context)
    assert (
        "утверждённые документы пока не загружены"
        in update.effective_message.reply_text.call_args.args[0]
    )


@pytest.mark.asyncio
async def test_fleet_docs_rejects_unknown_category(context):
    update = update_for(None)
    update.callback_query = SimpleNamespace(data="docs:unknown", answer=AsyncMock())
    await bot.handle_button(update, context)
    assert update.callback_query.answer.call_args.kwargs["show_alert"] is True
    update.effective_message.reply_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_fleet_docs_requires_access(context, monkeypatch):
    monkeypatch.setattr(bot, "is_allowed", lambda update: False)
    update = update_for(None)
    update.callback_query = SimpleNamespace(data="docs:aircraft", answer=AsyncMock())
    await bot.handle_button(update, context)
    assert update.callback_query.answer.call_args.kwargs["show_alert"] is True
    update.effective_message.reply_text.assert_not_awaited()
