from datetime import date
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from api import charter_routes
from api.main import create_app
from application.ops_views import OpsViews
from config.settings import settings
from services import charter_brief, charter_input
from services.charter_input import CharterIntent
from tests.test_mini_app import BASE, signed_headers
from tests.test_mini_app import sources as sources

REQUEST = {
    "legs": [
        {
            "dep_icao": "UAAA",
            "arr_icao": "OMDB",
            "local_date": "2026-12-11",
            "time_mode": "STA",
            "local_time": "13:00",
        },
        {
            "dep_icao": "OMDB",
            "arr_icao": "UAAA",
            "local_date": "2026-12-13",
            "time_mode": "WINDOW",
        },
    ],
    "pax": 6,
}


def test_airport_and_relative_date_grounding():
    text = "Алматы — Аль-Мактум OMDW в пятницу к 13:00, обратно в воскресенье."
    airports = charter_input._airport_mentions(text)
    assert airports["Алматы"] == ["UAAA"]
    assert airports["Аль-Мактум"] == ["OMDW"]
    assert airports["OMDW"] == ["OMDW"]
    resolved = charter_input._resolve_relative_days(text, date(2026, 11, 24))
    assert "2026-11-27 к 13:00" in resolved
    assert "обратно в 2026-11-29" in resolved


def test_charter_confirmation_uses_per_leg_time_and_does_not_run_during_parse(
    sources, monkeypatch
):
    monkeypatch.setattr(settings, "charter_sales_user_ids", "42")
    task = CharterIntent.model_validate({"request": REQUEST})
    parse = AsyncMock(return_value=task)
    matrix = AsyncMock(return_value={"aircraft": []})
    monkeypatch.setattr(charter_routes, "parse_charter_request", parse)
    monkeypatch.setattr(OpsViews, "charter_matrix", matrix)

    with TestClient(create_app()) as client:
        headers = signed_headers()
        response = client.post(
            BASE + "/charter/parse", json={"text": "UAAA to OMDB"}, headers=headers
        )
        assert response.status_code == 200
        draft = response.json()["request"]
        assert draft["legs"][0]["target_time_utc"] == "2026-12-11T09:00:00Z"
        assert draft["legs"][1]["window_start_utc"] == "2026-12-12T20:00:00Z"
        assert draft["legs"][1]["window_end_utc"] == "2026-12-13T20:00:00Z"
        matrix.assert_not_called()

        draft["legs"][0]["target_time_utc"] = "2099-01-01T00:00:00Z"
        response = client.post(
            BASE + "/charter/matrix", json={"request": draft}, headers=headers
        )
        assert response.status_code == 200
        matrix.assert_awaited_once_with(task.request.model_dump(mode="json"))


def test_opportunities_open_for_ops_users_without_sales(sources, monkeypatch):
    monkeypatch.setattr(settings, "charter_sales_user_ids", "")
    opportunities = AsyncMock(return_value={"empty_legs": [], "open_windows": []})
    monkeypatch.setattr(OpsViews, "charter_opportunities", opportunities)

    with TestClient(create_app()) as client:
        headers = signed_headers()
        access = client.get(BASE + "/charter/access", headers=headers)
        assert access.json() == {"sales_access": False, "edit_rates": False}
        assert client.get(BASE + "/charter/rates", headers=headers).status_code == 403
        assert (
            client.get(
                BASE + "/charter/opportunities?refresh=true", headers=headers
            ).status_code
            == 200
        )
        opportunities.assert_awaited_once_with(refresh=True)


def test_current_fleet_crew_is_sales_only_and_independent_of_a_quote(
    sources, monkeypatch
):
    monkeypatch.setattr(settings, "charter_sales_user_ids", "")
    current = AsyncMock(return_value={"aircraft": [], "complete": True})
    monkeypatch.setattr(OpsViews, "charter_current_fleet", current)
    with TestClient(create_app()) as client:
        path = BASE + "/charter/current-fleet?refresh=true"
        assert client.get(path).status_code == 401
        headers = signed_headers()
        assert client.get(path, headers=headers).status_code == 403
        current.assert_not_called()
        monkeypatch.setattr(settings, "charter_sales_user_ids", "42")
        assert client.get(path, headers=headers).json() == {
            "aircraft": [],
            "complete": True,
        }
        current.assert_awaited_once_with(refresh=True)


def test_refine_applies_bounded_customer_edits_only(sources, monkeypatch):
    monkeypatch.setattr(settings, "charter_sales_user_ids", "42")
    edits = charter_brief.BriefEdits(remove=["Price"], wifi=True, valid_for_hours=24)
    monkeypatch.setattr(
        charter_brief, "request_analysis", AsyncMock(return_value=edits)
    )
    base = "Flight: UAAA → OMDB\nAircraft: E35L\nPrice: USD 30,000"
    with TestClient(create_app()) as client:
        headers = signed_headers()
        response = client.post(
            BASE + "/charter/brief/refine",
            json={
                "current_brief": base,
                "instruction": "Remove price, add Wi-Fi, valid 24h",
            },
            headers=headers,
        )
        assert response.status_code == 200
        assert (
            response.json()["brief"]
            == "Flight: UAAA → OMDB\nAircraft: E35L\nWi-Fi: Available\nValidity: 24 h"
        )

        response = client.post(
            BASE + "/charter/brief/refine",
            json={
                "current_brief": base + "\nPrice: floor USD 21,000",
                "instruction": "Keep all",
            },
            headers=headers,
        )
        assert response.status_code == 422
