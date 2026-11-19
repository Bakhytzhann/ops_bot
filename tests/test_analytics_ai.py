import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from services import ai_analysis, analytics_ai
from services.source_cache import SourceCache


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "priorities"),
    [
        ('{"priority_ids":["fuel"]}', ["fuel"]),
        ('{"priority_ids":["invented"]}', None),
        ('{"priority_ids":["fuel","fuel"]}', None),
        (OSError("offline"), None),
    ],
)
async def test_analytics_model_only_prioritizes_verified_metrics(
    monkeypatch, response, priorities
):
    monkeypatch.setattr(ai_analysis, "cache", SourceCache())
    monkeypatch.setattr(ai_analysis.agent, "_inference_slots", asyncio.Semaphore(1))
    chat = AsyncMock()
    if isinstance(response, OSError):
        chat.side_effect = response
    else:
        chat.return_value = SimpleNamespace(message=SimpleNamespace(content=response))
    monkeypatch.setattr(ai_analysis.agent.client, "chat", chat)
    render = Mock(return_value="verified report")
    monkeypatch.setattr(analytics_ai, "render_insights", render)
    report = {
        "period": "7 дней",
        "overview": "6 рейсов",
        "metrics": [
            {
                "id": key,
                "title": key,
                "finding": "Зафиксировано в Journey Log",
                "coverage": "4/6",
                "purpose": "Проверить смету",
                "missing": "Сверить источник",
                "action": "Коммерция проверяет смету с диспетчером",
                "sources": ["Journey Log"],
            }
            for key in ("time", "fuel")
        ],
        "raw_rows": ["private data"],
    }

    assert await analytics_ai.analyze_metrics(report, 1) == "verified report"
    if priorities is None:
        render.assert_called_once_with(report)
    else:
        render.assert_called_once_with(report, priorities=priorities)
    assert "private data" not in chat.call_args.kwargs["messages"][1]["content"]
