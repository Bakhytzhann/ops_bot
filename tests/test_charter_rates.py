from concurrent.futures import ThreadPoolExecutor

from services.charter_rates import CharterRates, airport_profile, calculate_economics

RATE = {
    "registration": "UP-LA001",
    "currency": "USD",
    "pax_hour_rate": 5000,
    "ferry_hour_rate": 2500,
    "overnight_fee": 1000,
    "fuel_included": True,
    "margin_pct": 10,
}
INTERNATIONAL = {
    "scope_type": "category",
    "scope_key": "international",
    "currency": "USD",
    "handling_landing_est": 500,
    "pax_fee_per_head": 50,
    "overflight_avg_per_hr": 100,
}
OUTBOUND = {
    "departure": "UAAA",
    "arrival": "OMDB",
    "departure_utc": "2026-11-26T07:00:00Z",
    "block_minutes": 180,
    "airborne_minutes": 150,
    "fuel_kg": 3000,
}
INBOUND = {
    "departure": "OMDB",
    "arrival": "UAAA",
    "departure_utc": "2026-11-28T07:00:00Z",
    "block_minutes": 180,
    "fuel_kg": 2700,
}
FERRY = {
    "departure": "UCFM",
    "arrival": "UAAA",
    "departure_utc": "2026-11-26T05:00:00Z",
    "block_minutes": 60,
    "fuel_kg": 1000,
}


def test_rate_versions_remain_atomic_across_competing_writes(tmp_path):
    store = CharterRates(tmp_path / "rates.db")
    original = store.save_rate(RATE, 101)

    def update(value):
        return store.save_rate({**RATE, "pax_hour_rate": value}, 102)

    with ThreadPoolExecutor(max_workers=2) as executor:
        saved = list(executor.map(update, (6000, 7100)))

    current = store.snapshot()
    active = current["rates"][0]
    assert len(current["rates"]) == 1
    assert active["version"] == 3
    assert {row["version"] for row in saved} == {2, 3}
    second, first = current["archive"]
    assert first["id"] == original["id"]
    assert first["pax_hour_rate"] == 5000
    assert first["updated_by"] == "101"
    assert first["valid_to"] == first["archived_at"] == second["valid_from"]
    assert second["valid_to"] == second["archived_at"] == active["valid_from"]


def test_quote_economics_charges_ferry_and_fuel_and_applies_precise_airport_profile(
    tmp_path,
):
    store = CharterRates(tmp_path / "rates.db")
    store.save_rate({**RATE, "fuel_included": False, "fuel_price_per_kg": 0.8}, 101)
    store.save_airport_profile(INTERNATIONAL, 101)
    dubai_profile = store.save_airport_profile(
        {
            **INTERNATIONAL,
            "scope_type": "icao",
            "scope_key": "omdb",
            "handling_landing_est": 700,
            "operating_hours_utc": "00:00-24:00",
            "techstop_turn_minutes": 90,
        },
        101,
    )
    snapshot = store.snapshot()
    assert airport_profile(snapshot, "OMDB", "international") == dubai_profile
    assert (
        airport_profile(snapshot, "UAAA", "international")["scope_type"] == "category"
    )
    result = calculate_economics(
        snapshot,
        registration="UP-LA001",
        aircraft_type="CL60",
        legs=[OUTBOUND, INBOUND],
        ferry=FERRY,
        stay_days=2,
        passengers=5,
    )
    assert result == {
        "currency": "USD",
        "pax_cost": 30000,
        "ferry_cost": 2500,
        "wait_cost": 2000,
        "fees_est": 2850,
        "fuel_addon": 5360,
        "floor": 42710,
        "target": 46981,
        "rate_version": 1,
        "missing": [],
    }


def test_missing_ferry_and_wrong_fuel_currency_block_floor(tmp_path):
    store = CharterRates(tmp_path / "rates.db")
    store.save_rate({**RATE, "fuel_included": False}, 101)
    store.save_airport_profile(INTERNATIONAL, 101)
    result = calculate_economics(
        store.snapshot(),
        registration="UP-LA001",
        aircraft_type="CL60",
        legs=[OUTBOUND],
        ferry=None,
        ferry_required=None,
        stay_days=0,
        passengers=5,
        fuel_price_per_kg=1,
        fuel_currency="EUR",
    )
    assert result["pax_cost"] == 15000
    assert result["floor"] is result["target"] is None
    assert result["missing"] == [
        "Fuel price per kg in USD for the dry rate",
        "Positioning route and block hours",
    ]


def test_type_rate_charges_daily_shortfall_including_idle_day(tmp_path):
    store = CharterRates(tmp_path / "rates.db")
    store.save_rate(
        {**RATE, "registration": None, "aircraft_type": "cl60", "daily_min_hours": 4},
        101,
    )
    store.save_airport_profile(INTERNATIONAL, 101)
    result = calculate_economics(
        store.snapshot(),
        registration="UP-LA002",
        aircraft_type="CL60",
        legs=[OUTBOUND, INBOUND],
        ferry=FERRY,
        stay_days=2,
        passengers=5,
    )
    assert result["wait_cost"] == 25000
    assert result["fuel_addon"] == 0
    assert result["missing"] == []
