from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot import bot
from config.access import current_chat_id, current_user_id, require_access
from config.settings import settings


@pytest.mark.parametrize(
    "chat_type,user,chat,allowed",
    [
        ("private", 42, 42, True),
        ("private", 99, 99, False),
        ("group", 42, -100123, False),
        ("supergroup", 42, -100123, False),
        ("channel", 42, -100123, False),
    ],
)
@pytest.mark.asyncio
async def test_messages_and_buttons_enforce_private_allowlist(
    monkeypatch, chat_type, user, chat, allowed
):
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id=chat, type=chat_type),
        effective_user=SimpleNamespace(id=user, is_bot=False),
        effective_message=SimpleNamespace(text="/week", reply_text=AsyncMock()),
        callback_query=SimpleNamespace(data="op:week", answer=AsyncMock()),
    )
    flow = AsyncMock()
    monkeypatch.setattr(bot, "weekly_flow", flow)
    assert bot.is_allowed(update) is allowed
    await bot.handle_button(update, SimpleNamespace(user_data={}))
    assert flow.await_count == int(allowed)
    if not allowed:
        handler = AsyncMock()
        monkeypatch.setattr(bot, "_handle_message", handler)
        await bot.handle_message(update, SimpleNamespace())
        handler.assert_not_awaited()


def test_empty_allowlist_denies_tools(monkeypatch):
    monkeypatch.setattr(settings, "telegram_allowed_user_ids", "")
    user = current_user_id.set(42)
    chat = current_chat_id.set(42)
    try:
        with pytest.raises(PermissionError):
            require_access()
    finally:
        current_chat_id.reset(chat)
        current_user_id.reset(user)


def test_allowed_user_cannot_use_tools_from_group():
    user = current_user_id.set(42)
    chat = current_chat_id.set(-100123)
    try:
        with pytest.raises(PermissionError):
            require_access()
    finally:
        current_chat_id.reset(chat)
        current_user_id.reset(user)
