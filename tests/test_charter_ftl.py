from unittest.mock import AsyncMock

import pytest

from services import charter_ftl


def rule():
    return {
        "reporting_time_default": "01:00",
        "reporting_time_ferry": "00:45",
        "duty_start_offset": "00:10",
        "duty_after_last_sector_time": "00:30",
        "fdp_length_multi_acclimatised": "00:00 [ 08:00 07:30 ]\n06:00 [ 12:00 11:00 ]",
        "minimum_rest_at_home_base_time": "12:00",
        "minimum_rest_outside_home_base_time": "10:00",
        "minimum_rest_at_home_base_duty_multiplier": "1",
        "minimum_rest_outside_home_base_duty_multiplier": "1",
    }


def program(return_start="2026-12-01T09:00:00Z", return_end="2026-12-01T13:00:00Z"):
    return [
        {
            "departure": "UAAA",
            "arrival": "OMDW",
            "departure_utc": "2026-12-01T03:00:00Z",
            "arrival_utc": "2026-12-01T07:00:00Z",
            "ferry": True,
        },
        {
            "departure": "OMDW",
            "arrival": "UAAA",
            "departure_utc": return_start,
            "arrival_utc": return_end,
        },
    ]


@pytest.mark.asyncio
async def test_future_quote_counts_ferry_ground_and_release_without_roster(monkeypatch):
    loader = AsyncMock(return_value=[rule(), rule()])
    monkeypatch.setattr(charter_ftl, "_load_aircraft_rules", loader)
    result = await charter_ftl.quote_ftl({"id": 123, "registration": "TEST"}, program())

    loader.assert_awaited_once()
    assert loader.await_args.args[0] == 123
    assert result["status"] == "fits"
    assert result["report_utc"] == "2026-12-01T02:05:00+00:00"
    assert result["fdp_start_utc"] == "2026-12-01T02:15:00+00:00"
    assert result["release_utc"] == "2026-12-01T13:30:00+00:00"
    assert result["required_duty_minutes"] == 685
    assert result["duty_limit_minutes"] == 700
    assert result["duty_margin_minutes"] == 15
    assert result["required_fdp_minutes"] == 645
    assert result["fdp_limit_minutes"] == 660


@pytest.mark.parametrize(
    ("start", "end", "status"),
    [
        ("2026-12-01T11:00:00Z", "2026-12-01T15:00:00Z", "limited"),
        ("2026-12-02T09:00:00Z", "2026-12-02T13:00:00Z", "fits_after_rest"),
    ],
)
def test_quote_identifies_crossing_sector_or_a_full_rest_break(start, end, status):
    result = charter_ftl._calculate(
        charter_ftl._sectors(program(start, end)), [rule(), rule()]
    )

    assert result["status"] == status
    if status == "limited":
        assert result["first_limited_segment"] == 2
        assert result["duty_margin_minutes"] == -105
    else:
        assert result["required_rest_minutes"] == 720
        assert [(p["first_sector"], p["last_sector"]) for p in result["periods"]] == [
            (1, 1),
            (2, 2),
        ]


@pytest.mark.asyncio
async def test_no_numeric_allowance_if_route_is_missing(monkeypatch):
    loader = AsyncMock()
    monkeypatch.setattr(charter_ftl, "_load_aircraft_rules", loader)
    result = await charter_ftl.quote_ftl(
        {"id": 123}, [{**program()[0], "arrival_utc": None}]
    )

    assert result["status"] == "unavailable"
    assert "duty_limit_minutes" not in result
    loader.assert_not_called()
