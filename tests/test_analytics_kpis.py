"""Management, commercial, finance and timing KPIs for real flight categories."""

from datetime import UTC, datetime

from services.analytics_report import aggregate, render_analytics


def make(nid, dep, arr, origin, destination, *, ferry=None, tail="UP-AAA"):
    return {
        "flightNid": nid,
        "aircraft": tail,
        "departureUTC": dep,
        "arrivalUTC": arr,
        "departure": origin,
        "arrival": destination,
        "ferry": ferry,
        "cancelled": False,
    }


def summary():
    flights = [
        make(
            1, "2026-11-11T08:00:00Z", "2026-11-11T10:00:00Z",
            "UAAA", "UACC", ferry=False,
        ),
        make(
            2, "2026-11-13T12:00:00Z", "2026-11-13T13:00:00Z",
            "UACC", "UAAA", ferry=True,
        ),
        make(
            3, "2026-11-14T12:00:00Z", "2026-11-14T13:00:00Z",
            "UAAA", "UAII",
        ),
    ]
    data = aggregate(flights, datetime(2026, 11, 18, 12, tzinfo=UTC))
    return {
        **data,
        "period": "7d",
        "start": datetime(2026, 11, 12).date(),
        "end": datetime(2026, 11, 18).date(),
        "complete_windows": 1,
        "total_windows": 1,
        "complete": True,
    }


def test_ferry_proportions_and_idle_times():
    r = summary()
    assert r["ferry_count"] == 1
    assert r["nonferry_count"] == 1
    assert r["unknown_ferry"] == 1
    assert r["ferry_minutes"] == 60
    assert r["ferry_routes"]["UACC → UAAA"] == 1
    assert len(r["idle"]) == 1
    assert r["idle_minutes"] == 3000
    assert r["idle_72h"] == 0


def test_all_departments_show_real_aviation_kpis():
    r = summary()
    for view in ("management", "commercial", "finance", "timings"):
        text = render_analytics(r, view)
        assert "ferry" in text.lower()
        assert "Простой" in text or "просто" in text.lower()
        assert "1" in text
    assert "50.0%" in render_analytics(r, "commercial")
    assert "фактический downtime" in render_analytics(r, "timings")


def test_unknown_ferry_remains_unknown():
    flights = [
        make(
            1, "2026-11-11T08:00:00Z", "2026-11-11T10:00:00Z",
            "UAAA", "UACC",
        )
    ]
    r = aggregate(flights, datetime(2026, 11, 18, tzinfo=UTC))
    assert r["unknown_ferry"] == 1
    assert r["ferry_count"] == 0
    assert r["nonferry_count"] == 0
