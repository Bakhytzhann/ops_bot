from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent import core
from agent.prompts import MODEL_TOOL_NAMES, SYSTEM_PROMPT


@pytest.mark.asyncio
async def test_model_cannot_execute_operational_tool(monkeypatch):
    first = SimpleNamespace(
        tool_calls=[
            SimpleNamespace(
                function=SimpleNamespace(
                    name="get_flight_watch",
                    arguments={"number": "ABC1265", "date": "2026-11-11"},
                )
            )
        ],
        model_dump=lambda **kwargs: {"role": "assistant", "content": ""},
    )
    second = SimpleNamespace(tool_calls=[], content="Use /watch")
    client = SimpleNamespace(
        chat=AsyncMock(
            side_effect=[
                SimpleNamespace(message=first, load_duration=0),
                SimpleNamespace(message=second, load_duration=0),
            ]
        )
    )
    execute = AsyncMock()
    monkeypatch.setattr(core.registry, "execute", execute)
    agent = core.FlightAgent()
    agent.client = client
    assert await agent.chat("request") == "Use /watch"
    execute.assert_not_awaited()
    names = {t["function"]["name"] for t in client.chat.call_args.kwargs["tools"]}
    assert names <= MODEL_TOOL_NAMES
    assert "get_flight_watch" not in names


def test_prompt_matches_conversational_role():
    for command in (
        "/menu",
        "/ai",
        "/flighttime",
        "/route",
        "/briefing",
        "/overflight",
        "/compare",
    ):
        assert command in SYSTEM_PROMPT
    assert "no live Leon, ForeFlight or weather tools" in SYSTEM_PROMPT
