from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent.core import agent
from bot import bot
from config.access import current_user_id
from config.settings import settings


@pytest.mark.asyncio
async def test_operation_bypasses_llm_and_resets_identity(monkeypatch):
    monkeypatch.setattr(settings, "telegram_allowed_user_ids", "42")

    async def route(question):
        assert current_user_id.get() is None
        return "Flight Watch: verified answer"

    async def execute(*args):
        return await route(args[-1])

    monkeypatch.setattr(bot, "execute_command", execute)
    model = AsyncMock()
    monkeypatch.setattr(agent, "chat", model)
    message = SimpleNamespace(
        text="/route ABC1265 на 2026-11-11",
        message_thread_id=None,
        reply_text=AsyncMock(),
    )
    update = SimpleNamespace(
        effective_message=message,
        effective_user=SimpleNamespace(id=42, is_bot=False),
        effective_chat=SimpleNamespace(id=42, type="private"),
    )
    context = SimpleNamespace(
        user_data={}, bot=SimpleNamespace(send_chat_action=AsyncMock())
    )
    previous = current_user_id.get()
    await bot._handle_message(update, context)
    model.assert_not_awaited()
    assert message.reply_text.call_args.args[0] == "Flight Watch: verified answer"
    assert current_user_id.get() == previous
