from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot import flight_browser
from services import flight_foreflight
from services.flight_summary import (
    notam_summary,
    render_card,
    route_summary,
    wx_summary,
)
from services.notam_filters import notam_sets
from tests.test_compact_card import card as card


def test_requested_two_line_header(card):
    card.flight.update(
        flightNumber="FJK1265",
        aircraft="UP-EM012",
        departure="ZBAD",
        arrival="UCFM",
        departureUTC="2026-11-16T12:30:00Z",
        arrivalUTC="2026-11-16T18:00:00Z",
        departureLocal="2026-11-16T20:30:00",
        arrivalLocal="2026-11-17T00:00:00",
    )
    card.blocks["flighttime"].details["eet_minutes"] = 324
    card.blocks["crew"].details["codes"] = ["LOM", "VVP", "ZAR"]
    assert render_card(card).splitlines()[:2] == [
        "FJK1265 UP-EM012 16NOV ZBAD 1230z 1800z UCFM",
        "Расчетное 05:24 · CREW LOM/VVP/ZAR",
    ]


def test_main_weather_keeps_full_original_metar_and_sources(card):
    raw = "UAAA 141846Z 03002MPS 9999 OVC027 01/M02 Q1026 NOSIG RMK ORIGINAL  TEXT"
    card.blocks["weather"].details["reports"] = [
        {"station": "UAAA", "product": "METAR", "source": source, "raw": raw}
        for source in ("Leon MCP", "AWC")
    ]
    text = wx_summary(card)
    assert "UAAA (LEON/AWC) METAR " + raw.removeprefix("UAAA ") in text
    assert text.count(raw.removeprefix("UAAA ")) == 1
    assert "UGTB NO WX REPORT" in text
    assert "UACC NO WX REPORT" in text


@pytest.mark.parametrize("raw", ["", "NIL", "N/A"])
def test_no_metar_is_explicit_without_invented_values(card, raw):
    card.blocks["weather"].details["reports"] = [
        {"station": "UAAA", "product": "METAR", "source": "AWC", "raw": raw}
    ]
    text = wx_summary(card)
    assert "UAAA NO WX REPORT" in text
    assert "UAAA (AWC" not in text


def test_route_uses_foreflight_country_names_including_return_to_country(card):
    card.blocks["route"].details["overflownCountries"] = [
        {"name": name}
        for name in ("Kazakhstan", "Uzbekistan", "Tajikistan", "Kazakhstan")
    ]
    assert route_summary(card) == "Пролеты: KAZ, UZB, TJK, KAZ"
    assert "DCT TEST" not in render_card(card)


def test_country_iso_code_is_used_without_inference_from_route(card):
    card.blocks["route"].details["route"] = "KAZAKHSTAN DCT UZBEKISTAN"
    assert route_summary(card) == "Пролеты: нет данных"
    card.blocks["route"].details["overflownCountries"] = [
        {"isoCode": "KZ"},
        {"name": "Uzbekistan"},
    ]
    assert route_summary(card) == "Пролеты: KAZ, UZB"


def test_notam_compact_counts_match_existing_classification_and_include_alternates(
    card,
):
    text = notam_summary(card)
    assert text.startswith("NOTAM (AFFECT / REVIEW / ACTIVE):\n")
    for station in ("UAAA", "UGTB"):
        groups, recognized = notam_sets(card, station)
        assert recognized
        counts = "/".join(str(len(groups[key])) for key in ("A", "R", "C"))
        assert f"{station} ({counts})" in text
    assert "ALTN: UACC (—/—/—)" in text
    card.blocks["notams"].details["recognized"].add("UACC")
    card.blocks["notams"].details["notams"]["UACC"] = []
    assert "ALTN: UACC (0/0/0)" in notam_summary(card)


def test_primary_airport_is_not_repeated_as_alternate(card):
    card.blocks["route"].details["secondAlternate"] = "UAAA"
    assert notam_summary(card).count("UAAA (") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overflight",
    [
        {"overflownCountries": [{"name": "Kazakhstan"}, {"name": "Uzbekistan"}]},
        {"overflownCountries": []},
        OSError("Overflight unavailable"),
    ],
)
async def test_overflight_loaded_alongside_briefing_and_failure_does_not_break_eet(
    monkeypatch, card, overflight
):
    client = SimpleNamespace(
        get_flight=AsyncMock(
            return_value={
                "flightData": {"routeToDestination": {"route": "DCT TEST"}},
                "performance": {"times": {"timeToDestinationMinutes": 324}},
            }
        ),
        get_document=AsyncMock(return_value={"url": "https://example.test/briefing"}),
        get_overflight=AsyncMock(),
    )
    if isinstance(overflight, Exception):
        client.get_overflight.side_effect = overflight
    else:
        client.get_overflight.return_value = overflight

    class Client:
        async def __aenter__(self):
            return client

        async def __aexit__(self, *args):
            pass

    flight = card.flight
    monkeypatch.setattr(flight_foreflight, "ForeFlightClient", Client)
    monkeypatch.setattr(
        flight_foreflight,
        "find_flight",
        AsyncMock(
            return_value=(
                {
                    "flightId": "test",
                    "departure": flight["departure"],
                    "destination": flight["arrival"],
                    "departureTime": flight["departureUTC"],
                    "aircraftRegistration": flight["aircraft"],
                },
                "matched",
            )
        ),
    )
    monkeypatch.setattr(
        flight_foreflight,
        "download_briefing",
        AsyncMock(side_effect=OSError("PDF unavailable")),
    )
    blocks = await flight_foreflight.load_foreflight(flight)
    client.get_overflight.assert_awaited_once_with("test")
    client.get_document.assert_awaited_once_with("test", "briefing")
    assert blocks["briefing"].details["url"] == "https://example.test/briefing"
    assert blocks["briefing"].status == "ready"
    assert blocks["flighttime"].details["eet_minutes"] == 324
    assert blocks["notams"].status == "unavailable"
    card.blocks.update(blocks)
    expected = (
        "Пролеты: KAZ, UZB"
        if isinstance(overflight, dict) and overflight["overflownCountries"]
        else "Пролеты: нет данных"
    )
    assert route_summary(card) == expected


def test_route_removes_only_consecutive_duplicate_countries(card):
    card.blocks["route"].details["overflownCountries"] = [
        {"name": name} for name in ("Kazakhstan", "China", " CHINA ", "Kazakhstan")
    ]
    assert route_summary(card) == "Пролеты: KAZ, CHN, KAZ"


def test_local_times_are_not_inferred_when_leon_fields_are_missing(card):
    card.flight.pop("departureLocal")
    card.flight.pop("arrivalLocal")
    header = render_card(card).splitlines()[0]
    assert "(LT" not in header
    assert "0030z 0200z" in header


def test_bold_entities_use_utf16_offsets_and_do_not_change_source_text(card):
    card.flight["aircraft"] = "UP-TEST 🛫 & <tag>"
    text = render_card(card)
    encoded = text.encode("utf-16-le")
    entities = flight_browser.card_entities(text, card)
    bold = [
        encoded[entity.offset * 2 : (entity.offset + entity.length) * 2].decode(
            "utf-16-le"
        )
        for entity in entities
    ]
    assert {"ABC1265", "13NOV", "UAAA", "UGTB", "0030z", "0200z"} <= set(bold)
    assert all(entity.type == "bold" and entity.length > 0 for entity in entities)
    assert "UP-TEST" not in bold and "LEON" not in bold
    assert "& <tag>" in text


def test_main_card_uses_only_selected_time_mode_without_local_annotations(card):
    utc_header = render_card(card).splitlines()[0]
    assert "0030z 0200z" in utc_header and "LT" not in utc_header
    local_header = render_card(card, {"time": "LT"}).splitlines()[0]
    assert "0530 LT 0600 LT" in local_header
    assert "(LT" not in local_header and "0030z" not in local_header


@pytest.mark.parametrize(
    "countries,expected",
    [
        ([{"isoCode": "KZ"}, {"isoCode": "CN"}, {"isoCode": "CN"}], "KAZ, CHN"),
        ([{"isoCode": "KAZ"}, {"isoCode": "chn"}], "KAZ, CHN"),
        ([{"name": "France"}, {"name": "United States"}], "FRA, USA"),
        ([{"isoCode": "ZZ", "name": "Unknown country"}], "нет данных"),
    ],
)
def test_overflight_iso_alpha3_format(card, countries, expected):
    card.blocks["route"].details["overflownCountries"] = countries
    assert route_summary(card) == "Пролеты: " + expected
