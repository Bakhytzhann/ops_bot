"""Compact UI and Leon seconds/reporting regressions using verified response shapes."""

from time import monotonic
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot import bot
from bot import flight_browser as ui
from config.access import current_chat_id, current_user_id
from services.briefing_notams import extract_notams
from services.crew_format import format_flight_crew
from services.flight_display import flight_header
from services.flight_duty import (
    duty_rows,
    flight_ftl,
    reporting_interval,
    seconds_duration,
)
from services.flight_summary import eet_summary
from services.snapshot import render_snapshot
from services.snapshot_models import FlightSnapshot, SourceBlock, utc
from tests.test_snapshot_features import BRIEFING
from tests.test_weekly_browser import flight, update


@pytest.fixture
def card():
    f = flight(7)
    f.update(
        departure="UAAA",
        arrival="UGTB",
        departureIATA="ALA",
        departureUTC="2026-11-13T00:30:00Z",
        arrivalUTC="2026-11-13T02:00:00Z",
        departureLocal="2026-11-13T05:30:00",
        arrivalLocal="2026-11-13T06:00:00",
        aircraftType="C25B",
    )
    crew = [
        {
            "flightNid": 7,
            "crewMemberList": [
                {
                    "position": {"name": role},
                    "contact": {"name": "Test", "surname": name},
                }
                for role, name in (("CPT", "Captain"), ("FO", "Pilot"), ("CC", "Crew"))
            ],
        }
    ]
    records, recognized = extract_notams(BRIEFING, ["UAAA", "UGTB"])
    report = {
        "station": "UAAA",
        "product": "METAR",
        "raw": "UAAA 130000Z 00000KT 9999 SCT020 10/05 Q1015",
        "metadata": "",
        "source": "Leon MCP",
    }
    return FlightSnapshot(
        f,
        blocks={
            "crew": SourceBlock(
                "Leon",
                format_flight_crew(
                    "ABC1265", "2026-11-13", crew, compact=True, flight_nid=7
                ),
                "ready",
                details={"codes": ["VVI", "YMA", "RZA"]},
            ),
            "flighttime": SourceBlock(
                "ForeFlight", "EET 01:30", "ready", details={"eet_minutes": 90}
            ),
            "weather": SourceBlock(
                "Leon/AWC",
                "FULL METAR/TAF\n" + "W" * 5000,
                "ready",
                details={
                    "include_taf": True,
                    "reports": [
                        report,
                        {
                            "station": "UAAA",
                            "product": "TAF",
                            "raw": "TAF UAAA 130000Z 1300/1400 "
                            + " BECMG 1308/1310 9999 SCT030" * 200,
                            "metadata": "",
                            "source": "Leon MCP",
                        },
                    ],
                },
            ),
            "route": SourceBlock(
                "ForeFlight",
                "ROUTE DCT TEST\nALTN UACC",
                "ready",
                details={"route": "DCT TEST", "alternate": "UACC"},
            ),
            "notams": SourceBlock(
                "ForeFlight briefing",
                "NOTAM briefing",
                "partial",
                details={"notams": records, "recognized": recognized},
            ),
            "duty": SourceBlock("Leon", "DUTY detailed", "partial"),
        },
    )


def test_card_is_compact_without_repeated_source_boilerplate(card):
    text = render_snapshot(card)
    assert len(text) < 1500
    for value in (
        "FLT ABC1265 · 13NOV",
        "UAAA 0030z → 0200z UGTB",
        "полётное 01:30 · CREW VVI/YMA/RZA",
        "00000KT · 9999 · SCT020 · +10°C · Q1015",
        "RTE DCT TEST",
        "UACC (ALTN)",
        "UAAA · AFFECT 1 · REVIEW 1 · ACTIVE",
    ):
        assert value in text
    for value in (
        "DUTY",
        "ForeFlight",
        "Источник",
        "Обновлено",
        "FULL METAR/TAF",
        "Факт",
        "block time",
        "взлёт",
    ):
        assert value not in text
    assert text.count("ABC1265") == 1 and text.count("13NOV") == 1
    assert "crewMemberNid" not in text


def test_card_buttons_exact_and_switch_directions(card):
    markup = ui.card_keyboard(card, "token", 0)
    assert [[b.text for b in row] for row in markup.inline_keyboard] == [
        ["DUTY / REST", "WX / TAF"],
        ["RTE / ALTN", "NOTAM"],
        ["Z → LT", "ICAO → IATA"],
        ["Bot Summary"],
        ["REFRESH", "← SKED"],
    ]
    buttons = ui.preference_buttons("token", {"time": "LT", "codes": "IATA"})
    assert [b.text for b in buttons] == ["LT → Z", "IATA → ICAO"]


def test_sked_dates_inside_flight_buttons_only(card):
    listing = {
        "flights": [card.flight],
        "start": "2026-11-13",
        "end": "2026-11-15",
        "complete": True,
    }
    _, markup = ui.render_page("token", listing)
    assert markup.inline_keyboard[0][0].text == "ABC1265 13NOV UAAA 0030z 0200z UGTB"
    assert not any(
        b.text == "2026-11-13" for row in markup.inline_keyboard for b in row
    )
    assert not any(
        b.callback_data.startswith("wk:") for row in markup.inline_keyboard for b in row
    )
    card.flight["departureLocal"] = "2026-11-14T00:30:00"
    assert "14NOV ALA 0030 LT" in ui.flight_label(
        card.flight, {"time": "LT", "codes": "IATA"}
    )


def test_eet_never_uses_actual_or_block_time():
    block = SourceBlock(
        "ForeFlight",
        "block time 15:00",
        details={"eet_minutes": None, "totalTimeMinutes": 900},
    )
    assert eet_summary(block) == "нет данных"
    block.details["eet_minutes"] = 90
    assert eet_summary(block) == "01:30"


def test_local_card_and_midnight_offset(card):
    text = render_snapshot(card, {"time": "LT", "codes": "IATA"})
    assert "ALA 0530 LT → 0600 LT UGTB (ICAO)" in text
    assert "UACC (ICAO)" in text
    card.flight["arrivalUTC"] = "2026-11-14T02:00:00Z"
    assert "0200z (+1d)" in flight_header(card.flight)


def calc(start="23:30", end="02:00", on="22:30", off="02:30", length=14400):
    return {
        "dutyLength": length,
        "fdpSectors": [
            {
                "nid": 7,
                "schedule": {
                    "date": "2026-11-13T00:00:00Z",
                    "startTime": start,
                    "endTime": end,
                },
                "reportOn": {"time": on},
                "reportOff": {"time": off},
            }
        ],
    }


def envelope(calculation, roster=None):
    return {
        "ftl": {
            "ftlCalculationsForCrewMember": {
                "calculations": [
                    {"ftlCalculations": [calculation], "rosterDuties": roster or []}
                ]
            }
        }
    }


def test_reporting_midnight_and_seconds():
    start, end = reporting_interval(calc())
    assert start == utc("2026-11-13T22:30:00Z") and end == utc("2026-11-14T02:30:00Z")
    assert seconds_duration(14400) == "04:00"
    start, end = reporting_interval(calc("00:30", "01:30", "23:30", "02:00", 9000))
    assert start == utc("2026-11-12T23:30:00Z") and end == utc("2026-11-13T02:00:00Z")
    assert reporting_interval(calc(length=240)) is None
    assert reporting_interval(calc(on="bad")) is None


@pytest.mark.parametrize("value", [-1, None, True, float("inf"), float("nan")])
def test_invalid_durations_are_not_rendered_as_hours(value):
    assert seconds_duration(value) == "нет данных"


def test_violations_only_for_explicit_true():
    c = calc()
    c["restAfterDuty"] = {
        "actual": 32400,
        "limit": 36000,
        "limitType": "MIN",
        "offence": None,
    }
    c["fdpLength"] = {
        "actual": 14400,
        "limit": 18000,
        "limitType": "MAX",
        "offence": False,
    }
    text = "\n".join(flight_ftl(envelope(c), 7))
    assert "Rest After: 09:00 / MIN 10:00" in text and "FTL violations" not in text
    c["restAfterDuty"]["offence"] = True
    text = "\n".join(flight_ftl(envelope(c), 7))
    assert text.split("FTL violations\n")[1] == "Rest After: 09:00 / 10:00"


def test_ftl_roster_does_not_merge_calendar_on_day():
    calendar = {
        "login": {"loginNid": 9},
        "definition": {"cdrType": "per_type", "legend": "On Duty"},
        "startTimeIso": "2026-11-13T00:00:00Z",
        "endTimeIso": "2026-11-14T00:00:00Z",
    }
    roster = {
        "type": "DUTY",
        "startTimeIso": "2026-11-13T10:00:00Z",
        "endTimeIso": "2026-11-13T17:00:00Z",
    }
    assert duty_rows([calendar], envelope(calc(), [roster]), 9) == [roster]


def test_nested_count_violation_does_not_become_a_time_duration():
    c = calc()
    c["ftlCumulativeLimits"] = {
        "cumulativeDaysOff": [
            {"label": "Days off", "actual": 1, "limit": 2, "offence": True}
        ]
    }
    text = "\n".join(flight_ftl(envelope(c), 7))
    assert text.split("FTL violations\n")[1] == "Days off"


@pytest.mark.asyncio
async def test_details_refresh_and_back_edit_same_message(card, monkeypatch):
    user, chat = (
        current_user_id.set(42),
        current_chat_id.set(42),
    )
    try:
        listing = {
            "scope": (42, None),
            "expires": monotonic() + 60,
            "flights": [card.flight],
            "snapshots": {0: card},
            "start": "2026-11-13",
            "end": "2026-11-15",
            "complete": True,
        }
        ctx = SimpleNamespace(
            bot_data={"weekly_flight_lists": {"token": listing}}, user_data={}
        )
        monkeypatch.setattr(ui, "load_flight_snapshot", AsyncMock(return_value=card))
        for action, page in (
            ("duty", 0),
            ("weather", 0),
            ("weather", 1),
            ("route", 0),
            ("notams", 0),
            ("card", 0),
            ("refresh", 0),
        ):
            u = update(data=f"fc:token:0:{action}:{page}")
            await bot.handle_button(u, ctx)
            u.effective_message.reply_text.assert_not_awaited()
            assert len(u.callback_query.edit_message_text.call_args.args[0]) <= 3800
            buttons = u.callback_query.edit_message_text.call_args.kwargs[
                "reply_markup"
            ].inline_keyboard
            if action not in {"card", "refresh"}:
                assert any(
                    b.callback_data == "fc:token:0:card:0"
                    for row in buttons
                    for b in row
                )
        back = update(data="wk:token:0")
        await bot.handle_button(back, ctx)
        back.effective_message.reply_text.assert_not_awaited()
        assert "SKED:" in back.callback_query.edit_message_text.call_args.args[0]
        monkeypatch.setattr(
            ui,
            "get_flight_schedule",
            AsyncMock(return_value={"flights": [card.flight], "complete": True}),
        )
        u = update(data="op:week")
        await bot.handle_button(u, ctx)
        u.effective_message.reply_text.assert_not_awaited()
        u.callback_query.edit_message_text.assert_awaited_once()
    finally:
        current_chat_id.reset(chat)
        current_user_id.reset(user)
