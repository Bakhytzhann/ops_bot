"""Analytics reporting never represents schedule fields as actuals or revenue."""

from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot import analytics, bot
from services.analytics_report import (
    aggregate,
    load_analytics,
    month_windows,
    period_start,
    render_analytics,
)
from tests.test_bot_commands import update_for


def make_flight(nid, tail, dep, arr, origin="UAAA", destination="UACC"):
    return {
        "flightNid": nid,
        "aircraft": tail,
        "flightNumber": f"ABC{nid}",
        "departureUTC": dep,
        "arrivalUTC": arr,
        "departure": origin,
        "arrival": destination,
        "cancelled": False,
    }


def test_month_windows_are_bounded_and_contiguous():
    start, end = date(2025, 11, 18), date(2026, 11, 18)
    windows = list(month_windows(start, end))
    assert windows[0][0] == start
    assert windows[-1][1] == end
    for first, last in windows:
        assert 0 <= (last - first).days <= 30
    for earlier, later in zip(windows, windows[1:]):
        assert earlier[1].toordinal() + 1 == later[0].toordinal()


def test_period_month_rollover():
    assert period_start("1m", date(2026, 3, 31)) == date(2026, 2, 28)
    assert period_start("12m", date(2026, 11, 18)) == date(2025, 11, 18)
    assert period_start("7d", date(2026, 11, 18)) == date(2026, 11, 12)


def test_planned_timing_aggregation_and_turns():
    now = datetime(2026, 11, 18, 20, tzinfo=UTC)
    a = make_flight(10, "UP-AAA", "2026-11-18T08:00:00Z", "2026-11-18T10:00:00Z")
    b = make_flight(
        11,
        "UP-AAA",
        "2026-11-18T11:30:00Z",
        "2026-11-18T13:00:00Z",
        "UACC",
        "UAAA",
    )
    c = make_flight(
        12,
        "UP-AAA",
        "2026-11-18T15:00:00Z",
        "2026-11-18T16:00:00Z",
        "UGTB",
        "UAAA",
    )
    summary = aggregate([a, a, b, c], now)
    assert summary["legs"] == 3
    assert summary["minutes"] == 270
    assert len(summary["turns"]) == 1
    assert summary["turns"][0]["minutes"] == 90
    assert len(summary["position_mismatches"]) == 1


def test_finance_report_does_not_invent_earnings_or_crash():
    now = datetime(2026, 11, 18, 20, tzinfo=UTC)
    summary = aggregate(
        [
            make_flight(10, "UP-AAA", "2026-11-18T08:00:00Z", "2026-11-18T10:00:00Z"),
        ],
        now,
    )
    result = {
        **summary,
        "period": "7d",
        "start": date(2026, 11, 12),
        "end": date(2026, 11, 18),
        "complete": True,
        "complete_windows": 1,
        "total_windows": 1,
    }
    finance = render_analytics(result, "finance")
    timings = render_analytics(result, "timings")
    assert "UP-AAA: 2:00" in finance
    assert "нет данных из подтверждённой финансовой системы" in finance
    assert "OUT/OFF/ON/IN" in timings
    assert "2:00" in timings


@pytest.mark.asyncio
async def test_partial_leon_windows_explicit():
    async def source(first, last):
        if first == "2026-11-12":
            return {"flights": [], "complete": False}
        return {"flights": [], "complete": True}

    result = await load_analytics(
        "7d", now=datetime(2026, 11, 18, 10, tzinfo=UTC), loader=source
    )
    assert not result["complete"]
    assert "неполная" in render_analytics(result, "management")


@pytest.mark.asyncio
async def test_button_and_period_views(monkeypatch):
    ctx = SimpleNamespace(user_data={})
    message = update_for("ANALYTICS")
    await bot.handle_message(message, ctx)
    assert "выберите период" in message.effective_message.reply_text.call_args.args[0]
    assert ctx.user_data["analytics_state"]["period"] is None
    mocked = AsyncMock()
    monkeypatch.setattr(analytics, "load_analytics", mocked)
    old = update_for(None)
    old.callback_query = SimpleNamespace(
        data="ana:invalid:period:7d",
        answer=AsyncMock(),
    )
    await bot.handle_button(old, ctx)
    assert old.callback_query.answer.call_args.kwargs["show_alert"] is True
    mocked.assert_not_awaited()
