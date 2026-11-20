"""SKED session/navigation and source-field regressions; all APIs mocked."""

from datetime import date
from time import monotonic
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent.core import agent
from bot import bot
from bot import flight_browser as browser
from config.access import current_chat_id, current_user_id
from integrations.leon import tools
from services.flight_display import airport, flight_time
from services.flight_duty import duty_rows, flight_ftl
from services.snapshot import render_snapshot
from services.snapshot_models import FlightSnapshot, SourceBlock
from tests.test_weekly_browser import flight, update


@pytest.fixture
def identity():
    user = current_user_id.set(42)
    chat = current_chat_id.set(42)
    yield
    current_chat_id.reset(chat)
    current_user_id.reset(user)


@pytest.fixture
def session():
    f = flight(7)
    f.update(
        departureIATA="TAS", departureLocal="2026-11-11T17:00:00", aircraftType="C25B"
    )
    snapshot = FlightSnapshot(
        f,
        blocks={
            name: SourceBlock("Test", name + " source text", "ready")
            for name in ("crew", "weather", "duty", "flighttime", "notams", "route")
        },
    )
    listing = {
        "scope": (42, None),
        "expires": monotonic() + 60,
        "flights": [f],
        "snapshots": {0: snapshot},
        "start": "2026-12-29",
        "end": "2027-01-04",
        "complete": True,
    }
    return SimpleNamespace(
        bot_data={"weekly_flight_lists": {"token": listing}}, user_data={}
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "direction,start,end",
    [("next", "2027-01-05", "2027-01-11"),
     ("prev", "2026-12-22", "2026-12-28")],
)
async def test_next_window_crosses_year(identity, session, monkeypatch, direction, start, end):
    query = AsyncMock(return_value={"flights": [], "complete": True})
    monkeypatch.setattr(browser, "get_flight_schedule", query)
    old = session.bot_data["weekly_flight_lists"]["token"]
    old["preferences"] = {"time": "LT", "codes": "IATA"}
    u = update(data=f"sk:token:{direction}")
    await bot.handle_button(u, session)
    query.assert_awaited_once_with(start, end)
    new = list(session.bot_data["weekly_flight_lists"].values())[-1]
    assert new["preferences"] == old["preferences"]
    assert "нет рейсов" in u.callback_query.edit_message_text.call_args.args[0]


@pytest.mark.asyncio
async def test_refresh_preserves_period(identity, session, monkeypatch):
    query = AsyncMock(return_value={"flights": [], "complete": True})
    monkeypatch.setattr(browser, "get_flight_schedule", query)
    await browser.handle_selection(update(data="sk:token:refresh"), session)
    query.assert_awaited_once_with("2026-12-29", "2027-01-04")


@pytest.mark.asyncio
async def test_session_toggles_survive_card_details_and_back(
    identity, session, monkeypatch
):
    load = AsyncMock()
    monkeypatch.setattr(browser, "load_flight_snapshot", load)
    model = AsyncMock()
    monkeypatch.setattr(agent, "chat", model)
    await browser.handle_selection(update(data="sv:token:view"), session)
    card = update(data="fc:token:0:card:0")
    await browser.handle_selection(card, session)
    text = card.callback_query.edit_message_text.call_args.args[0]
    assert "TAS" in text and "UAAA (ICAO)" in text and "1700 LT" in text
    for action in ("eet", "crew", "duty", "route", "notams", "card"):
        u = update(data=f"fc:token:0:{action}:0")
        await browser.handle_selection(u, session)
        u.callback_query.edit_message_text.assert_awaited()
        markup = u.callback_query.edit_message_text.call_args.kwargs["reply_markup"]
        assert any(
            b.callback_data
            == ("fc:token:0:card:0" if action == "notams" else "wk:token:0")
            for row in markup.inline_keyboard
            for b in row
        )
    back = update(data="wk:token:0")
    await browser.handle_selection(back, session)
    text = back.callback_query.edit_message_text.call_args.args[0]
    assert "TAS 1700 LT" in text
    load.assert_not_awaited()
    model.assert_not_awaited()
    # A fresh SKED must not inherit another screen's preferences.
    query = AsyncMock(return_value={"flights": [], "complete": True})
    monkeypatch.setattr(browser, "get_flight_schedule", query)
    await browser.show_week(update(), session, today=date(2026, 11, 13))
    assert (
        list(session.bot_data["weekly_flight_lists"].values())[-1]["preferences"] == {}
    )


def test_lt_never_falls_back_to_utc_and_preserves_airport_wall_clock():
    f = flight(1)
    assert flight_time(f, "departure", {"time": "LT"}) == "нет данных"
    f["departureLocal"] = "2026-11-12T01:00:00+13:00"
    assert flight_time(f, "departure", {"time": "LT"}) == "0100 LT"
    assert "12 Nov 2026" in flight_time(f, "departure", {"time": "LT"}, full=True)
    assert airport(f, "departure", {"codes": "IATA"}) == "UTTT (ICAO)"


@pytest.mark.asyncio
async def test_sked_sorts_by_instant_and_distinguishes_empty_from_failure(
    identity, monkeypatch
):
    first, second = flight(1), flight(2)
    first["departureUTC"] = "2026-11-11T15:00:00+05:00"
    second["departureUTC"] = "2026-11-11T11:00:00Z"
    query = AsyncMock(return_value={"flights": [second, first], "complete": True})
    monkeypatch.setattr(browser, "get_flight_schedule", query)
    ctx = SimpleNamespace(bot_data={}, user_data={})
    await browser.show_week(update(), ctx)
    assert list(ctx.bot_data["weekly_flight_lists"].values())[-1]["flights"] == [
        first,
        second,
    ]
    query.return_value = {"flights": [], "complete": True}
    u = update()
    await browser.show_week(u, ctx)
    assert "нет рейсов" in u.effective_message.reply_text.call_args.args[0]
    query.side_effect = TimeoutError()
    await browser.show_week(u, ctx)
    text = u.effective_message.reply_text.call_args.args[0]
    assert "нет данных" in text and "нет рейсов" not in text


@pytest.mark.asyncio
async def test_filtered_search_does_not_mark_complete_source_incomplete(
    identity, monkeypatch
):
    monkeypatch.setattr(
        browser,
        "get_flight_schedule",
        AsyncMock(
            return_value={
                "flights": [flight(1), flight(2, number="ABC123")],
                "complete": True,
            }
        ),
    )
    u = update()
    await browser.show_week(
        u, SimpleNamespace(bot_data={}, user_data={}), search="ABC1265"
    )
    assert "неполное" not in u.effective_message.reply_text.call_args.args[0]


@pytest.mark.asyncio
async def test_schedule_keeps_verified_leon_fields(identity, monkeypatch):
    monkeypatch.setattr(
        tools,
        "call_leon",
        AsyncMock(
            return_value={
                "flightList": [
                    {
                        "flightNid": 1,
                        "flightNo": "ABC1234",
                        "startAirport": {"code": {"icao": "UAAA", "iata": "ALA"}},
                        "endAirport": {"code": {"icao": "UACC"}},
                        "startTimeUTC": "2026-11-13T00:30:00Z",
                        "startTimeLocal": "2026-11-13T05:30:00",
                        "endTimeUTC": "2026-11-13T02:00:00Z",
                        "endTimeLocal": "2026-11-13T07:00:00",
                        "acft": {"registration": "TEST", "acftType": {"icao": "C25B"}},
                    }
                ],
                "shouldRerunRequestForNextPageWithOffset": False,
            }
        ),
    )
    row = (await tools.get_flight_schedule("2026-11-13"))["flights"][0]
    assert row["departureIATA"] == "ALA" and row["arrivalIATA"] is None
    assert row["aircraftType"] == "C25B" and row["aircraft"] == "TEST"
    assert browser.flight_label(row) == "ABC1234 13NOV UAAA 0030z 0200z UACC"


def test_per_type_on_is_duty_and_vacation_is_excluded():
    def row(legend):
        return {
            "login": {"loginNid": 9},
            "definition": {"cdrType": "per_type", "legend": legend},
            "startTimeIso": "2026-11-13T00:00:00Z",
            "endTimeIso": "2026-11-13T05:00:00Z",
        }

    assert duty_rows([row("VACATION")], {}, 9) == []
    assert duty_rows([row("On Duty"), row("VACATION")], {}, 9) == [row("On Duty")]
    assert duty_rows([row("On Duty")], {}, 10) == []


def test_exact_sector_ftl_rest_and_roster_fallback():
    metric = {"actual": 32400, "limit": 36000, "limitType": "MIN", "offence": None}
    calculation = {
        "fdpSectors": [{"nid": 7}],
        "restAfterDuty": metric,
        "nextEOBT": "2026-11-14T12:00:00Z",
    }
    roster = {
        "type": "DUTY",
        "startTimeIso": "2026-11-13T00:00:00Z",
        "endTimeIso": "2026-11-13T05:00:00Z",
    }
    data = {
        "ftl": {
            "ftlCalculationsForCrewMember": {
                "calculations": [
                    {
                        "ftlCalculations": [calculation],
                        "rosterDuties": [roster],
                    }
                ]
            }
        }
    }
    text = "\n".join(flight_ftl(data, 7))
    assert "Rest After: 09:00 / MIN 10:00" in text
    assert "FTL violations" not in text
    assert "14 Nov 2026 12:00Z" in text
    assert "нет однозначного" in "\n".join(flight_ftl(data, 8))
    assert duty_rows(RuntimeError(), data, 9) == [roster]


@pytest.mark.parametrize(
    "ftl", [{"ftl": None}, {"ftl": {"ftlCalculationsForCrewMember": None}}]
)
def test_malformed_ftl_preserves_valid_roster(ftl):
    roster = {
        "login": {"loginNid": 9},
        "definition": {"cdrType": "duty"},
        "startTimeIso": "2026-11-13T00:00:00Z",
        "endTimeIso": "2026-11-13T05:00:00Z",
    }
    assert duty_rows([roster], ftl, 9) == [roster]
    with pytest.raises(TypeError):
        flight_ftl(ftl, 7)


def test_report_contains_sources_and_card_is_bounded():
    snapshot = FlightSnapshot(
        flight(7),
        blocks={
            key: SourceBlock("Test", "X" * 7000, "partial")
            for key in ("crew", "duty", "weather", "route", "flighttime", "notams")
        },
    )
    snapshot.blocks["crew"] = SourceBlock(
        "Leon", "CPT: Test Pilot\nFO: Other Pilot", "ready"
    )
    card = render_snapshot(snapshot)
    assert len(card) <= 3800
    for title in (
        "ABC1265",
        "WX",
        "NOTAM",
    ):
        assert title in card
    assert "DUTY / REST" not in card
    report = render_snapshot(snapshot, report=True)
    assert report.startswith("Bot Report") and "partial" in report and "Test" in report
    assert len(report) > len(card)


@pytest.mark.asyncio
async def test_new_callbacks_reject_other_chat(identity, session):
    for data in ("sk:token:7", "sv:token:codes", "fc:token:0:report:0"):
        u = update(data=data, chat=123)
        await bot.handle_button(u, session)
        u.callback_query.edit_message_text.assert_not_awaited()
        assert u.callback_query.answer.call_args.kwargs["show_alert"]
