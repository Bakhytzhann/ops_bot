from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from application import operations
from bot import bot
from config.access import current_chat_id, current_user_id, require_access
from config.settings import settings
from services.weather_decode import summarize_metar
from services.weather_reports import format_awc_weather, format_reports


@pytest.mark.parametrize("user", [42, 99, 12345])
def test_only_authorized_private_user_passes_tool_access(monkeypatch, user):
    monkeypatch.setattr(settings, "telegram_allowed_user_ids", "42")
    u = current_user_id.set(user)
    c = current_chat_id.set(42)
    try:
        if user == 42:
            require_access()
        else:
            with pytest.raises(PermissionError):
                require_access()
    finally:
        current_chat_id.reset(c)
        current_user_id.reset(u)


@pytest.mark.parametrize("chat,user", [(123, 42), (123, 99), (42, None)])
def test_other_chats_and_missing_identity_denied(monkeypatch, chat, user):
    monkeypatch.setattr(settings, "telegram_allowed_user_ids", "42")
    u = current_user_id.set(user)
    c = current_chat_id.set(chat)
    try:
        with pytest.raises(PermissionError):
            require_access()
    finally:
        current_chat_id.reset(c)
        current_user_id.reset(u)


@pytest.mark.asyncio
async def test_legacy_route_command_executes_without_global_identity(monkeypatch):
    monkeypatch.setattr(settings, "telegram_allowed_user_ids", "42")
    message = SimpleNamespace(
        text="/route",
        message_thread_id=None,
        reply_text=AsyncMock(return_value=SimpleNamespace(message_id=900)),
    )
    update = SimpleNamespace(
        effective_message=message,
        effective_user=SimpleNamespace(id=42, is_bot=False, first_name="Member"),
        effective_chat=SimpleNamespace(id=42, type="private"),
        callback_query=SimpleNamespace(data="op:route", answer=AsyncMock()),
    )
    context = SimpleNamespace(
        user_data={}, bot=SimpleNamespace(send_chat_action=AsyncMock())
    )
    await bot.handle_message(update, context)
    assert context.user_data["operation_inputs"][(42, None)]["action"] == "route"

    async def direct(*args):
        assert current_user_id.get() is None
        assert current_chat_id.get() is None
        return "Group member response"

    monkeypatch.setattr(operations, "foreflight_operation", direct)
    update.effective_message = SimpleNamespace(
        text="ABC1265 на 2026-11-11",
        message_thread_id=None,
        reply_to_message=SimpleNamespace(message_id=900),
        reply_text=AsyncMock(),
    )
    previous = current_chat_id.get()
    await bot.handle_message(update, context)
    assert (
        update.effective_message.reply_text.call_args.args[0] == "Group member response"
    )
    assert current_chat_id.get() == previous


@pytest.mark.parametrize(
    "code,expected",
    [
        ("R08/0800U", "ВПП 08 800 м ↑"),
        ("R27L/M0500V1200D", "ВПП 27L <500–1200 м ↓"),
        ("R11/P6000FT", "ВПП 11 >6000 ft"),
    ],
)
def test_rvr_units_ranges_and_qualifiers(code, expected):
    text = summarize_metar(f"UTTT 111200Z 27005KT 0800 {code} -RA BKN002 M02/M05 Q1013")
    assert expected in text
    assert "200 ft (~61 м)" in text
    assert "-2 °C" in text and "дождь" in text


def test_missing_rvr_is_not_replaced_by_general_visibility():
    text = summarize_metar("UTTT 111200Z 00000KT 9999 SCT020 15/05 Q1015")
    assert "RVR: не указан" in text
    assert "10 км" not in text


def test_trend_and_remarks_do_not_override_current_conditions():
    text = summarize_metar(
        "UTTT 111200Z 00000KT 9999 SCT020 15/05 Q1015 TEMPO 0500 R08/0300 SN OVC001 M10/M12 RMK RA"
    )
    assert "2000 ft" in text and "15 °C" in text
    assert "RVR: не указан" in text
    assert "снег" not in text


@pytest.mark.parametrize(
    "cloud,expected",
    [
        ("VV///", "VV не определена"),
        ("BKN///", "высота не определена"),
        ("FEW005 BKN020", "500 ft"),
        ("CAVOK", "нет значимой облачности"),
    ],
)
def test_cloud_information_is_conservative(cloud, expected):
    assert expected in summarize_metar(f"UTTT 111200Z 00000KT {cloud} 15/05 Q1015")


def test_same_raw_report_is_not_duplicated_across_sources():
    raw = "UTTT 111200Z 00000KT CAVOK 15/05 Q1015"
    reports = [
        {"source": s, "station": "UTTT", "product": "METAR", "raw": raw, "metadata": ""}
        for s in ("Leon MCP", "AWC")
    ]
    text = format_reports(reports)
    assert text.count(raw) == 1
    assert "Leon MCP, AWC" in text
    assert "Расшифровка" not in text
    assert len(text) < 400


def test_default_weather_omits_taf_but_explicit_request_preserves_raw():
    metar, taf = (
        "UTTT 111200Z 00000KT CAVOK 15/05 Q1015",
        "TAF UTTT 111100Z 1112/1218 27010KT CAVOK TEMPO 1114/1118 3000 RA",
    )
    result = {
        "airports": ["UTTT"],
        "products": [
            {"product": "metar", "data": [{"icaoId": "UTTT", "rawOb": metar}]},
            {"product": "taf", "data": [{"icaoId": "UTTT", "rawTAF": taf}]},
        ],
    }
    text = format_awc_weather(result)
    assert metar in text and taf not in text
    assert taf in format_awc_weather(result, include_taf=True)


@pytest.mark.parametrize(
    "text,include_taf", [("погода EGLL", False), ("TAF EGLL", True)]
)
@pytest.mark.asyncio
async def test_weather_route_respects_explicit_taf_request(
    monkeypatch, text, include_taf
):
    from services import operational_routes as routes

    request = AsyncMock(return_value={"airports": ["EGLL"], "products": []})
    monkeypatch.setattr(routes, "get_weather", request)
    u, c = current_user_id.set(42), current_chat_id.set(42)
    try:
        await routes.route_operation(text)
    finally:
        current_chat_id.reset(c)
        current_user_id.reset(u)
    request.assert_awaited_once_with(["EGLL"], include_taf=include_taf)
