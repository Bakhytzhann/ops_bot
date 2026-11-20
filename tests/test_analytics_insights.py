from datetime import date

from services.analytics_insights import build_insights, render_insights
from services.journey_history import JourneyHistory


def sample(count, total=None, median=None):
    return {"count": count, "total": total, "median": median}


def by_id(report):
    return {metric["id"]: metric for metric in report["metrics"]}


def test_insights_distinguishes_population_and_measurement_coverage(tmp_path):
    start, end = date(2026, 8, 20), date(2026, 11, 17)
    stats = JourneyHistory(tmp_path / "analytics.db").summary(start, end)
    stats.update(
        scheduled_count=165,
        cancelled_count=20,
        actual_count=130,
        reported_unknown_ferry_count=130,
        covered_days=90,
        complete=True,
        last_synced_at="2026-11-18T21:04:00+00:00",
        planned_block_minutes=sample(140, 23_456, 150),
        route_aircraft_count=11,
        block_minutes=sample(130, 23_886, 162),
        block_delta_minutes=sample(130, 1910, 15),
        airborne_minutes=sample(130, 21_528, 138),
        distance_nm=sample(127, 141_516, 939),
        fuel={"KG": sample(130, 401_489, 2711)},
        routes=[{"departure": "UAAA", "arrival": "UACC", "count": 25}],
        aircraft_types=[{"type": "GLF6", "count": 100}],
    )

    report = build_insights(stats)
    cards = by_id(report)
    assert "90/90 (100,0%)" in report["overview"]
    assert "130/145 (89,7%)" in cards["block"]["coverage"]
    assert "+31 ч 50 мин" in cards["block"]["finding"]
    assert "130/130 (100,0%)" in cards["fuel"]["coverage"]
    assert "0/130 (0,0%)" in cards["fuel"]["coverage"]
    assert "401 489,0 KG" in cards["fuel"]["finding"]
    assert "25/130 (19,2%)" in cards["geography"]["coverage"]
    assert "127/130 (97,7%)" in cards["geography"]["coverage"]
    assert "100/130 (76,9%)" in cards["geography"]["coverage"]
    assert "11/130 (8,5%)" in cards["geography"]["coverage"]
    assert "140/145 (96,6%)" in cards["schedule"]["coverage"]
    assert "0/130 (0,0%)" in cards["ferry"]["coverage"]
    assert "определить пока нельзя" in cards["ferry"]["finding"]
    assert "20/165 (12,1%)" in cards["cancellations"]["finding"]
    assert "не измерено" in cards["margin"]["coverage"]
    assert all(
        card["purpose"] and card["missing"] and card["action"]
        for card in cards.values()
    )

    rendered = render_insights(report, ("margin", "margin", "unknown"))
    assert rendered.index(cards["margin"]["title"]) < rendered.index(
        cards["block"]["title"]
    )
    assert rendered.count(cards["margin"]["title"]) == 1
    assert all(card["title"] in rendered for card in cards.values())


def test_insights_explains_partial_source_failure_and_never_divides_by_zero(tmp_path):
    start, end = date(2026, 11, 12), date(2026, 11, 18)
    stats = JourneyHistory(tmp_path / "analytics.db").summary(start, end)
    stats["source_failed"] = True

    report = build_insights(stats)
    cards = by_id(report)
    assert "ещё не загружен" in report["overview"]
    assert "0/7" in report["overview"]
    assert "не удалось обновить" in report["overview"]
    assert "0/0" not in render_insights(report)
    assert "Плановое блок-время" not in cards["schedule"]["finding"]
