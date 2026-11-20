from datetime import UTC, datetime

from services import ground_timing


def stamp(hour, minute):
    return datetime(2026, 11, 19, hour, minute, tzinfo=UTC)


def event(kind, hour, minute, **detail):
    return {"kind": kind, "event_at": stamp(hour, minute), "detail": detail}


def test_ground_deltas_keep_original_baseline_and_wait_after_ready_separate():
    timing = ground_timing.turn_timing(
        {
            "planned_in": stamp(13, 5),
            "planned_out": stamp(14, 10),
            "actual_in": stamp(13, 15),
            "actual_out": stamp(14, 18),
        },
        [
            event("OUT_BLOCK", 14, 20),
            event("REPORTED_READY", 14, 12),
        ],
        baseline={"planned_in": stamp(13, 5), "planned_out": stamp(14, 0)},
        now=stamp(14, 25),
    )
    assert timing["target"] == stamp(14, 10)
    assert timing["inbound_delta_minutes"] == 10
    assert timing["departure_delta_minutes"] == 18
    assert timing["station_delta_minutes"] == 8
    assert timing["after_ready_minutes"] == 6
    assert timing["movement_conflicts"] == {"OUT": stamp(14, 20)}


def test_parallel_blockers_use_latest_eta_and_keep_unknown_open():
    turn = {"planned_out": stamp(14, 0)}
    target = event("TARGET", 13, 40, target_at=stamp(14, 2).isoformat())
    fuel = event(
        "BLOCKER_OPEN", 13, 45, process="fuel", expected_at=stamp(14, 5).isoformat()
    )
    pax = event(
        "BLOCKER_OPEN", 13, 47, process="pax", expected_at=stamp(14, 12).isoformat()
    )
    timing = ground_timing.turn_timing(turn, [target, fuel, pax], now=stamp(13, 50))
    assert timing["target_source"] == "внутренняя цель"
    assert timing["forecast_ready"] == stamp(14, 12)
    assert timing["risk_minutes"] == 10
    timing = ground_timing.turn_timing(
        turn,
        [target, fuel, pax, event("BLOCKER_OPEN", 13, 49, process="handling")],
        now=stamp(13, 50),
    )
    assert timing["forecast_ready"] is None
    assert timing["ready_not_before"] == stamp(14, 12)
    assert timing["unknown_blockers"]


def test_station_remark_labels_fallback_and_keeps_fuel_time_and_wait_separate(
    monkeypatch,
):
    def fuel(station, aircraft_type, *, exclude_turn):
        assert exclude_turn == "selected"
        count = 18 if station is None and aircraft_type == "E35L" else 14
        return {"count": count, "median_minutes": 22, "p80_minutes": 31}

    def wait(station, aircraft_type, *, exclude_turn):
        count = 16 if station == "UACC" and aircraft_type is None else 1
        return {"count": count, "median_minutes": 10, "p80_minutes": 17}

    def history(station, aircraft_type, *, exclude_turn):
        return {
            "fuel_duration": fuel(station, aircraft_type, exclude_turn=exclude_turn),
            "fuel_wait": wait(station, aircraft_type, exclude_turn=exclude_turn),
            "ground_duration": {"count": 0},
        }

    monkeypatch.setattr(ground_timing.ground_turns, "station_history", history)
    remarks = ground_timing.station_remarks("UACC", "E35L", exclude_turn="selected")
    assert "Длительность заправки · по компании, E35L: медиана 22 мин" in remarks[0]
    assert "Ожидание подачи топливной машины · UACC, все типы ВС" in remarks[1]
