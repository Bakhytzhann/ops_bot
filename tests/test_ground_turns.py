from datetime import UTC, datetime, timedelta

import pytest

from services.ground_turns import GroundTurnStore

START = datetime(2026, 10, 8, 9, 30, tzinfo=UTC)


def turn(number, station="UACC", aircraft_type="E35L"):
    return {
        "key": f"{station}:o:{number}",
        "station": station,
        "inbound_id": number + 1000,
        "outbound_id": number,
        "registration": "UP-ABC",
        "aircraft_type": aircraft_type,
        "planned_in": START,
        "planned_out": START + timedelta(hours=1),
    }


def record(store, card, kind, minutes, *, actor=42, request=None, detail=None):
    return store.record(
        card,
        kind,
        actor_id=actor,
        event_at=START + timedelta(minutes=minutes),
        request_id=request or f"telegram:{kind}:{minutes}",
        detail=detail,
    )


def test_shared_events_are_idempotent_and_keep_baseline(tmp_path):
    store = GroundTurnStore(tmp_path / "turns.sqlite")
    card = turn(10)
    store.observe(card)
    changed = {**card, "planned_out": START + timedelta(hours=2)}
    assert record(store, changed, "IN_BLOCK", 7, actor=43)
    assert not record(store, changed, "IN_BLOCK", 7, actor=43)
    assert record(
        store,
        changed,
        "BLOCKER_OPEN",
        9,
        detail={"process": "fuel", "expected_at": "2026-10-08T15:00+05:00"},
    )

    events = store.events(card["key"])
    assert [event["actor_id"] for event in events] == [43, 42]
    assert events[1]["detail"]["expected_at"] == "2026-10-08T10:00:00.000000+00:00"
    assert datetime.fromisoformat(events[0]["event_at"]) == START + timedelta(minutes=7)
    assert all(
        event["source"] == "telegram" and event["recorded_at"] for event in events
    )
    assert datetime.fromisoformat(
        store.baseline(card["key"])["planned_out"]
    ) == START + timedelta(hours=1)
    assert store.baseline(turn(11)["key"]) is None


def test_observe_keeps_flight_identity_without_importing_leon_actuals(tmp_path):
    store = GroundTurnStore(tmp_path / "turns.sqlite")
    card = turn(10)
    observed = {
        **card,
        "actual_in": START,
        "actual_out": START + timedelta(hours=1),
    }
    store.observe(observed)
    store.observe(observed)
    assert store.events(card["key"]) == []
    assert record(store, card, "OUT_BLOCK", 63)
    events = store.events(card["key"])
    assert [(event["kind"], event["source"]) for event in events] == [
        ("OUT_BLOCK", "telegram"),
    ]
    assert datetime.fromisoformat(
        store.recent_outs([card["key"]])[card["key"]]
    ) == START + timedelta(minutes=63)
    assert store.station_history("UACC")["ground_duration"]["count"] == 0


def test_station_statistics_separate_service_wait_and_missing_data(tmp_path):
    store = GroundTurnStore(tmp_path / "ground.sqlite")
    east = turn(11)
    west = turn(12, station="UAAA", aircraft_type="CL60")
    partial = turn(13)
    for card in (east, west):
        assert record(store, card, "IN_BLOCK", 0)
        assert record(store, card, "OUT_BLOCK", 60)
        assert record(store, card, "FUEL_STARTED", 10)
        assert record(store, card, "FUEL_COMPLETED", 30)
    assert record(store, east, "FUEL_STARTED", 12, request="telegram:correction")
    assert record(store, east, "FUEL_REQUESTED", 1)
    assert record(store, east, "FUEL_TRUCK_ARRIVED", 11)
    assert record(store, east, "BLOCKER_OPEN", 2, detail={"process": "fuel"})
    assert record(store, east, "BLOCKER_OPEN", 3, detail={"process": "fuel"})
    assert record(store, east, "BLOCKER_OPEN", 4, detail={"process": "handling"})
    assert record(store, partial, "FUEL_STARTED", 10)

    station = store.station_history("UACC", "E35L")
    assert station["turn_count"] == 2
    assert station["fuel_duration"]["count"] == 1
    assert station["fuel_duration"]["median_minutes"] == 18
    assert station["fuel_wait"]["median_minutes"] == 10
    assert station["ground_duration"]["median_minutes"] == 60
    assert station["blocker_counts"] == {"fuel": 1, "handling": 1}
    assert store.fuel_stats(None)["count"] == 2
    assert store.fuel_stats(None)["p80_minutes"] == 20
    assert store.fuel_stats("UACC", exclude_turn=east["key"])["count"] == 0


def test_record_requires_aware_time_and_specific_blocker(tmp_path):
    store = GroundTurnStore(tmp_path / "ground.sqlite")
    with pytest.raises(ValueError, match="часовым поясом"):
        store.record(
            turn(50),
            "IN_BLOCK",
            actor_id=42,
            event_at=START.replace(tzinfo=None),
            request_id="telegram:without-timezone",
        )
    with pytest.raises(ValueError, match="процесс"):
        record(store, turn(50), "BLOCKER_OPEN", 10, detail={"process": "unknown"})
    assert not store.events(turn(50)["key"])
