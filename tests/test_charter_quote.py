"""Demo charter quote math and menu controls; no live tariff claims."""

import pytest

from bot.charter_quote import DEMO_ROUTES, estimate, menu, quote_view


def test_twenty_destination_scenarios_with_valid_positive_totals():
    assert len(DEMO_ROUTES) == 20
    assert len({x[1] for x in DEMO_ROUTES}) == 20
    for i in range(20):
        quote = estimate(i)
        assert quote["route_km"] >= quote["direct_km"] > 0
        assert quote["hours"] >= 1
        assert quote["total"] >= quote["subtotal"] > 0
        assert quote["sectors"] == 1


def test_roundtrip_is_double_one_way_with_same_assumptions():
    one = estimate(12, rate=6500, pax=8, reserve=10)
    two = estimate(12, rate=6500, pax=8, reserve=10, return_flight=True)
    assert two["subtotal"] == one["subtotal"] * 2
    assert abs(two["total"] - one["total"] * 2) <= 100
    assert two["sectors"] == 2


def test_quote_price_controls_change_total():
    base = estimate(12, rate=5500, pax=4, reserve=0)
    dear = estimate(12, rate=7500, pax=12, reserve=20)
    assert dear["total"] > base["total"]
    assert dear["reserve_amount"] > base["reserve_amount"]


@pytest.mark.parametrize(
    "params",
    [
        {"index": -1},
        {"index": 20},
        {"index": 0, "rate": 0},
        {"index": 0, "pax": 1000},
        {"index": 0, "reserve": -10},
    ],
)
def test_rejects_invalid_options(params):
    with pytest.raises(ValueError):
        estimate(**params)


def test_menu_and_quote_explicitly_label_demo_costs():
    text, markup = menu()
    assert "20" in text
    assert len(markup.inline_keyboard) > 10
    text, markup = quote_view(
        {
            "index": 12,
            "rate": 6500,
            "pax": 8,
            "reserve": 10,
            "return_flight": False,
        }
    )
    assert "НЕ коммерческое" in text
    assert "не подтверждены" in text
    assert "quote:mode:1" in str(markup)
