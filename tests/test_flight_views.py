from datetime import UTC, datetime, timedelta
from time import monotonic
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot import flight_browser
from bot.notam_browser import render_notams
from config.access import current_chat_id, current_user_id
from services import flight_weather
from services import snapshot as snapshots
from services.flight_duty import seconds_duration
from services.flight_summary import notam_summary, render_card, wx_summary
from services.flight_weather import compact_metar, full_weather, station_roles
from services.notam_filters import notam_sets
from services.snapshot_models import Notam, SourceBlock
from services.source_cache import SourceCache
from tests.test_compact_card import card as card
from tests.test_weekly_browser import update


def buttons(markup):
    return [button for row in markup.inline_keyboard for button in row]


def test_target_three_lines_and_unique_roles(card):
    card.flight.update(aircraftType="E35L", aircraft="UP-TEST")
    card.blocks["route"].details["secondAlternate"] = "UAAA"
    text = render_card(card)
    assert text.splitlines()[0] == "ABC1265 UP-TEST 13NOV UAAA 0030z 0200z UGTB"
    assert "ETE 01:30" in text and "CREW VVI/YMA/RZA" in text
    assert "WX · CURRENT METAR" in text
    assert "Пролеты: нет данных" in text and "DCT TEST" not in text
    assert "CPT: Test" not in text
    card.blocks["crew"].details["codes"] = []
    assert "CREW нет данных" in render_card(card)


def test_metar_aviation_tokens_and_rvr_only_when_supplied():
    raw = "UAAA 130100Z 03002MPS 9999 OVC027 01/M02 Q1026 NOSIG"
    assert compact_metar(raw) == "03002MPS · 9999 · OVC027 · +1°C · Q1026"
    assert "RVR" not in compact_metar(raw)
    raw = (
        "UACC 130100Z 26002MPS 6000 -SN R22/P1500U BKN004 M01/M02 Q1029 TEMPO 1000 +SN"
    )
    text = compact_metar(raw)
    assert text == "26002MPS · 6000 · -SN · R22/P1500U · BKN004 · −1°C · Q1029"
    assert "+SN" not in text and "1000" not in text


def test_wx_shared_header_and_conflicting_source_times(card):
    raw = "UAAA 130100Z 03002MPS 9999 OVC027 01/M02 Q1026"
    rows = [
        {
            "station": "UAAA",
            "product": "METAR",
            "raw": raw,
            "metadata": "",
            "source": source,
        }
        for source in ("Leon MCP", "AWC")
    ]
    card.blocks["weather"].details["reports"] = rows
    text = wx_summary(card)
    assert text.startswith("WX\nUAAA (LEON/AWC) METAR " + raw.removeprefix("UAAA "))
    assert text.count(raw.removeprefix("UAAA ")) == 1
    rows[1]["raw"] = raw.replace("130100Z", "130130Z")
    text = wx_summary(card)
    assert "LEON) METAR 130100Z" in text and "AWC) METAR 130130Z" in text
    assert text.count("03002MPS") == 2


def test_full_weather_preserves_raw_without_repeated_metadata(card):
    metar = "UAAA 130100Z 03002MPS 9999 OVC027 01/M02 Q1026"
    taf = "TAF UAAA 130000Z 1300/1400 03002MPS 9999 SCT030"
    card.blocks["weather"].details["reports"] = [
        {
            "station": "UAAA",
            "product": product,
            "raw": raw,
            "metadata": "redundant UTC timestamp",
            "source": source,
        }
        for product, raw in (("METAR", metar), ("TAF", taf))
        for source in ("Leon MCP", "AWC")
    ]
    text = full_weather(card)
    assert text.count(metar) == 1 and text.count(taf) == 1
    assert text.count("Leon / AWC") == 1
    assert "redundant UTC timestamp" not in text
    assert "UACC (ALTN)" in text


@pytest.mark.parametrize("value", [2147483647, 2147483648, 9223372036854775807])
def test_ftl_sentinel_is_not_a_duration(value):
    assert seconds_duration(value) == "нет данных"
    assert seconds_duration(9 * 3600) == "09:00"


@pytest.fixture
def notams(card):
    start = datetime(2026, 11, 13, tzinfo=UTC)
    rows = [
        Notam(
            f"A{i:04d}/26",
            "UAAA",
            "N",
            None,
            f"A{i:04d}/26 NOTAMN\nE) FULL TEXT {i}",
            start - timedelta(days=1),
            start + timedelta(days=2),
            topic=f"TOPIC {i}",
        )
        for i in range(12)
    ]
    rows[1].schedule = "DAILY 1000-1100"
    rows[2].starts = start + timedelta(hours=1)
    card.flight.update(
        departureUTC=(start + timedelta(hours=2)).isoformat(),
        arrivalUTC=(start + timedelta(hours=3)).isoformat(),
    )
    card.blocks["notams"] = SourceBlock(
        "ForeFlight",
        "briefing",
        "partial",
        fetched_at=start,
        details={"notams": {"UAAA": rows, "UGTB": []}, "recognized": {"UAAA", "UGTB"}},
    )
    return card


def test_notam_filters_match_card_and_current_snapshot(notams):
    groups, known = notam_sets(notams, "UAAA")
    assert known and len(groups["T"]) == 12 and len(groups["A"]) == 11
    assert len(groups["R"]) == 1 and len(groups["C"]) == 10
    assert "UAAA (11/1/10)" in notam_summary(notams)
    assert groups["R"][0][1].identifier == "A0001/26"


def test_notam_pages_and_record_navigation(notams):
    text, markup = render_notams(notams, "token", 0, "nlD_T")
    assert "PAGE 1/1 · 1–12 / 12" in text
    for _, record in notam_sets(notams, "UAAA")[0]["T"]:
        assert record.raw in text and record.topic in text
    assert [b.text for b in markup.inline_keyboard[0]] == ["DEP UAAA", "ARR UGTB"]
    assert {b.text for b in markup.inline_keyboard[1]} == {
        "AFFECT 11",
        "REVIEW 1",
        "ACTIVE 10",
        "ALL 12",
    }
    assert not any(":nv" in b.callback_data for b in buttons(markup))
    assert all(len(b.callback_data.encode()) <= 64 for b in buttons(markup))
    _, arr = render_notams(notams, "token", 0, "nlA_T")
    assert not any(":nv" in b.callback_data for b in buttons(arr))


def test_notam_unknown_section_is_not_zero_and_bad_indices_fail(notams):
    notams.blocks["notams"].details["recognized"].remove("UGTB")
    text, markup = render_notams(notams, "token", 0, "nlA_A")
    assert "нет данных" in text and any(b.text == "AFFECT —" for b in buttons(markup))
    with pytest.raises(ValueError):
        render_notams(notams, "token", 0, "nlD_T", 3)
    with pytest.raises(ValueError):
        render_notams(notams, "token", 0, "nvD_T_999_00000000")


def test_notam_stale_record_cannot_open_different_text(notams):
    with pytest.raises(ValueError, match="Неизвестный экран"):
        render_notams(notams, "token", 0, "nvD_T_0_00000000")


def test_long_notam_full_text_is_accessible_without_truncation(notams):
    record = notams.blocks["notams"].details["notams"]["UAAA"][0]
    record.raw = "Ж" * 7500
    page = found = 0
    while True:
        text, markup = render_notams(notams, "token", 0, "nlD_T", page)
        found += text.count("Ж")
        assert len(text) <= 3800
        if not any(b.text == "NEXT" for b in buttons(markup)):
            break
        page += 1
    assert found == 7500 and page >= 2


@pytest.fixture
def identity():
    user, chat = (
        current_user_id.set(42),
        current_chat_id.set(42),
    )
    yield
    current_chat_id.reset(chat)
    current_user_id.reset(user)


@pytest.mark.asyncio
async def test_notam_callbacks_and_switches_edit_existing_message(notams, identity):
    for record in notams.blocks["notams"].details["notams"]["UAAA"]:
        record.raw += "\n" + "X" * 700
    listing = {
        "scope": (42, None),
        "expires": monotonic() + 60,
        "flights": [notams.flight],
        "snapshots": {0: notams},
    }
    ctx = SimpleNamespace(
        bot_data={"weekly_flight_lists": {"token": listing}}, user_data={}
    )
    for action, page in (
        ("notams", 0),
        ("nlD_T", 1),
        ("time", 0),
        ("codes", 0),
        ("nlD_R", 0),
        ("nlA_T", 0),
        ("card", 0),
    ):
        u = update(data=f"fc:token:0:{action}:{page}")
        await flight_browser.handle_selection(u, ctx)
        u.callback_query.edit_message_text.assert_awaited_once()
        u.effective_message.reply_text.assert_not_awaited()
    u = update(data="fc:token:0:nlD_T:0", chat=123)
    with pytest.raises(PermissionError):
        await flight_browser.handle_selection(u, ctx)


@pytest.mark.asyncio
async def test_awc_alternates_are_batched_and_unique(card, identity, monkeypatch):
    card.blocks["route"].details.update(secondAlternate="UAAA", takeOffAlternate="UACC")
    assert station_roles(card) == {
        "UAAA": ["DEP", "ALTN 2"],
        "UGTB": ["ARR"],
        "UACC": ["ALTN", "TO ALTN"],
    }

    async def get(codes, **kwargs):
        assert len(codes) <= 4
        return {
            "airports": codes,
            "products": [
                {"product": "metar", "data": []},
                {"product": "taf", "data": []},
            ],
        }

    api = AsyncMock(side_effect=get)
    monkeypatch.setattr(flight_weather, "get_weather", api)
    result = await flight_weather.alternate_reports(
        ["UAAA", "UGTB", "UACC", "UAKK", "UATT"]
    )
    assert api.await_count == 2 and not result["errors"]


@pytest.mark.asyncio
async def test_snapshot_stores_actual_crew_codes_and_loads_only_new_alternates(
    card, identity, monkeypatch
):
    monkeypatch.setattr(snapshots, "cache", SourceCache())
    monkeypatch.setattr(
        snapshots,
        "get_flight_schedule",
        AsyncMock(return_value={"flights": [card.flight], "complete": True}),
    )
    monkeypatch.setattr(
        snapshots,
        "get_flight_crew",
        AsyncMock(
            return_value={
                "crew": [
                    {
                        "flightNid": 7,
                        "crewMemberList": [
                            {"login": {"code": "VVI"}},
                            {"login": {"code": "YMA"}},
                            {"crewMemberNid": 123},
                        ],
                    }
                ]
            }
        ),
    )
    monkeypatch.setattr(
        snapshots, "load_duty", AsyncMock(return_value=SourceBlock("Leon"))
    )
    monkeypatch.setattr(
        snapshots,
        "selected_flight_weather",
        AsyncMock(return_value={"reports": [], "errors": [], "partial": True}),
    )
    card.blocks["route"].details["secondAlternate"] = "UAAA"
    monkeypatch.setattr(
        snapshots,
        "load_fast_foreflight",
        AsyncMock(return_value={"route": card.blocks["route"]}),
    )
    alt = AsyncMock(return_value={"reports": [], "errors": []})
    result = await snapshots.load_flight_snapshot(card.flight)
    assert result.blocks["crew"].details["codes"] == ["VVI", "YMA"]
    alt.assert_not_awaited()
    assert "123" not in render_card(result)


def test_us_visibility_and_vicinity_weather_keep_aviation_notation():
    text = compact_metar(
        "KTEST 130100Z VRB05KT 1 1/2SM VCSH R22/M0600FT BKN010 01/M02 A2992"
    )
    assert text == "VRB05KT · 1 1/2SM · VCSH · R22/M0600FT · BKN010 · +1°C · A2992"
