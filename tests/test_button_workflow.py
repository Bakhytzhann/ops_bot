import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent.core import agent
from bot import bot
from bot.commands import COMMANDS, MENU_GROUPS, menu_keyboard, telegram_commands
from bot.keyboards import HOME_SECTIONS
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
@pytest.mark.parametrize("label,section", HOME_SECTIONS.items())
async def test_home_sections_show_operation_buttons(context, label, section):
    update = update_for(label)
    await bot.handle_message(update, context)
    assert update.effective_message.reply_text.call_args.kwargs[
        "reply_markup"
    ] == menu_keyboard(section)
    agent.chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_startup_only_configures_telegram(context, monkeypatch):
    monkeypatch.setattr(bot, "BOT_USERNAME", "OPS_bot")
    telegram = SimpleNamespace(username="OPS_bot", set_my_commands=AsyncMock())
    await bot.setup_telegram(SimpleNamespace(bot=telegram))
    telegram.set_my_commands.assert_awaited_once()
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


def test_menus_and_commands_have_no_ai_or_flight_watch():
    assert not {"ai", "watch"} & COMMANDS.keys()
    assert not {"ai", "watch"} & {command.command for command in telegram_commands()}
    for group in [None, *MENU_GROUPS]:
        markup = menu_keyboard(group)
        assert not any(
            b.callback_data in {"op:ai", "op:watch"}
            for row in markup.inline_keyboard
            for b in row
        )
