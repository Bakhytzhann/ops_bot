import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent.core import FlightAgent
from bot import bot
from config.settings import settings


def update_for(user_id):
    return SimpleNamespace(
        effective_chat=SimpleNamespace(id=user_id, type="private"),
        effective_user=SimpleNamespace(id=user_id, is_bot=False),
        effective_message=SimpleNamespace(text="/sked"),
    )


@pytest.mark.asyncio
async def test_other_user_proceeds_while_first_waits(monkeypatch):
    monkeypatch.setattr(settings, "telegram_allowed_user_ids", "1,2")
    entered = asyncio.Event()
    release = asyncio.Event()
    finished = []

    async def handler(update, context):
        if update.effective_user.id == 1:
            entered.set()
            await release.wait()
        finished.append(update.effective_user.id)

    monkeypatch.setattr(bot, "_handle_message", handler)
    first = asyncio.create_task(bot.handle_message(update_for(1), None))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.wait_for(bot.handle_message(update_for(2), None), 1)
        assert finished == [2]
    finally:
        release.set()
        await first


@pytest.mark.asyncio
async def test_same_user_messages_remain_ordered(monkeypatch):
    monkeypatch.setattr(settings, "telegram_allowed_user_ids", "1,2")
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def handler(update, context):
        calls.append("start")
        entered.set()
        await release.wait()
        calls.append("end")

    monkeypatch.setattr(bot, "_handle_message", handler)
    first = asyncio.create_task(bot.handle_message(update_for(1), None))
    second = None
    try:
        await asyncio.wait_for(entered.wait(), 1)
        second = asyncio.create_task(bot.handle_message(update_for(1), None))
        await asyncio.sleep(0)
        assert calls == ["start"]
    finally:
        release.set()
        await asyncio.gather(first, *([second] if second else []))
    assert calls == ["start", "end", "start", "end"]


@pytest.mark.asyncio
async def test_model_kept_loaded_and_generation_serialized():
    agent = FlightAgent()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def generate_answer(**kwargs):
        entered.set()
        await release.wait()
        return SimpleNamespace(
            message=SimpleNamespace(content="ready", tool_calls=[]),
            load_duration=0,
        )

    agent.client = SimpleNamespace(chat=AsyncMock(side_effect=generate_answer))
    first = asyncio.create_task(agent.chat("hello"))
    second = None
    try:
        await asyncio.wait_for(entered.wait(), 1)
        second = asyncio.create_task(agent.chat("hello again"))
        await asyncio.sleep(0)
        assert agent.client.chat.await_count == 1
    finally:
        release.set()
        await asyncio.gather(first, *([second] if second else []))
    assert agent.client.chat.await_count == 2
    assert (
        agent.client.chat.call_args.kwargs["keep_alive"] == settings.ollama_keep_alive
    )


@pytest.mark.asyncio
async def test_warmup_generates_no_answer():
    agent = FlightAgent()
    agent.client = SimpleNamespace(generate=AsyncMock())
    await agent.warmup()
    assert agent.client.generate.call_args.kwargs["prompt"] == ""
    assert (
        agent.client.generate.call_args.kwargs["keep_alive"]
        == settings.ollama_keep_alive
    )


def test_telegram_concurrency_enabled(monkeypatch):
    from pydantic import SecretStr

    monkeypatch.setattr(settings, "telegram_bot_token", SecretStr("123:test"))
    application = bot.create_application()
    assert application.concurrent_updates == settings.telegram_max_parallel
    assert application.post_init is bot.setup_telegram
