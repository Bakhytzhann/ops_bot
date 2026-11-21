from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

from fastapi.testclient import TestClient

from api import ops_routes
from api.main import create_app
from application import ops_views
from config.settings import settings
from services import charter_intelligence
from services.ground_turns import GroundTurnStore
from services.station_notes import NotesRepository
from tests.test_mini_app import BASE, DAY, LEG, signed_headers
from tests.test_mini_app import sources as sources


def test_ground_event_uses_resolved_turn_and_shared_idempotent_log(
    sources, monkeypatch, tmp_path
):
    store = GroundTurnStore(tmp_path / "turns.sqlite3")
    monkeypatch.setattr(ops_routes, "ground_turns", store)
    monkeypatch.setattr(ops_views, "ground_turns", store)
    now = datetime.now(UTC)
    turn = {
        "key": "UAAA:o:121",
        "station": "UAAA",
        "registration": LEG["aircraft"],
        "aircraft_type": LEG["aircraftType"],
        "inbound_id": None,
        "outbound_id": 121,
        "planned_out": now + timedelta(minutes=30),
        "actual_out": None,
        "status": "active",
        "reference_time": now,
    }
    loader = AsyncMock(
        return_value={
            "cards": [turn],
            "schedule_complete": True,
            "fetched_at": now,
        }
    )
    monkeypatch.setattr(ops_views, "load_ground_flights", loader)
    request = {
        "request_id": str(uuid4()),
        "kind": "BLOCKER_OPEN",
        "process": "fuel",
        "text": "Truck en route",
        "expected_at": (now + timedelta(minutes=10)).isoformat(),
        "station": "WRNG",
    }
    path = BASE + "/ground/turns/UAAA:o:121/events"
    with TestClient(create_app()) as client:
        assert client.post(path, json=request).status_code == 401
        loader.assert_not_called()
        headers = signed_headers()
        assert (
            client.post(
                path.replace("121", "999"), json=request, headers=headers
            ).status_code
            == 404
        )
        for _ in range(2):
            response = client.post(path, json=request, headers=headers)
            assert response.status_code == 200
            assert response.json()["turn"]["station"] == "UAAA"
            assert (
                response.json()["timing"]["blockers"][0]["reason"] == "Truck en route"
            )
            assert len(response.json()["events"]) == 1
        log = store.events(turn["key"])
        assert len(log) == 1
        assert log[0]["actor_id"] == 42
        assert log[0]["source"] == "telegram"
        report = client.get(BASE + "/ground/stations/UAAA", headers=headers).json()
        assert report["events"] == 1


def test_ground_report_preview_is_read_only_and_batch_is_atomic(
    sources, monkeypatch, tmp_path
):
    database = tmp_path / "turns.sqlite3"
    store, notes = GroundTurnStore(database), NotesRepository(database)
    monkeypatch.setattr(ops_views, "ground_turns", store)
    monkeypatch.setattr(ops_routes, "ground_turns", store)
    monkeypatch.setattr(ops_routes, "notes", notes)
    now = datetime.now(UTC) - timedelta(seconds=1)
    turn = {
        "key": "UAAA:o:121",
        "station": "UAAA",
        "registration": LEG["aircraft"],
        "aircraft_type": LEG["aircraftType"],
        "inbound_id": None,
        "outbound_id": 121,
        "outbound_number": "OPS201",
        "outbound_arrival": "UACC",
        "planned_out": now + timedelta(minutes=30),
        "actual_out": None,
        "status": "active",
        "reference_time": now,
    }
    monkeypatch.setattr(
        ops_views,
        "load_ground_flights",
        AsyncMock(
            return_value={
                "cards": [turn],
                "schedule_complete": True,
                "fetched_at": now,
            }
        ),
    )
    report = [
        ("FUEL_STARTED", now, {}),
        ("BLOCKER_OPEN", now, {"process": "pax", "reason": "Waiting for bags"}),
        ("STATION_NOTE", now, {"text": "New handling agent", "critical": True}),
    ]
    parser = AsyncMock(return_value=("OPS999", report))
    monkeypatch.setattr(ops_routes, "parse_ground_input", parser)
    path = BASE + "/ground/turns/" + turn["key"]
    with TestClient(create_app()) as client:
        assert client.post(path + "/parse", json={"text": "Report"}).status_code == 401
        headers = signed_headers()
        assert (
            client.post(
                path + "/parse", json={"text": "Report"}, headers=headers
            ).status_code
            == 422
        )
        parser.return_value = ("OPS201", report)
        preview = client.post(path + "/parse", json={"text": "Report"}, headers=headers)
        assert preview.status_code == 200
        assert preview.json()["station_notes_allowed"] is False
        assert store.events(turn["key"]) == []
        assert (
            client.post(
                path + "/events/batch", json=preview.json(), headers=headers
            ).status_code
            == 403
        )
        assert store.events(turn["key"]) == []

        notes.bind(42, "test-staff", "Ops")
        preview = client.post(
            path + "/parse", json={"text": "Report"}, headers=headers
        ).json()
        assert preview["station_notes_allowed"] is True
        assert notes.list(42, "UAAA") == []
        for _ in range(2):
            result = client.post(path + "/events/batch", json=preview, headers=headers)
            assert result.status_code == 200
            assert [event["kind"] for event in result.json()["events"]] == [
                "FUEL_STARTED",
                "BLOCKER_OPEN",
            ]
        assert notes.list(42, "UAAA")[0]["category"] == "safety"
        assert len(notes.list(42, "UAAA")) == 1
        view = client.get(BASE + "/ground", headers=headers).json()
        assert view["cards"][0]["blockers"][0]["reason"] == "Waiting for bags"
        resolved = client.post(
            path + "/events/batch",
            headers=headers,
            json={
                "request_id": str(uuid4()),
                "items": [{"kind": "BLOCKER_CLOSE", "process": "pax"}],
            },
        )
        assert resolved.status_code == 200
        assert resolved.json()["timing"]["blockers"] == []
        assert (
            client.get(BASE + "/ground", headers=headers).json()["cards"][0]["blockers"]
            == []
        )


def test_checklist_uses_versioned_selected_leg_and_only_its_items(sources, monkeypatch):
    mcp = AsyncMock(
        return_value={
            "result": [
                {
                    "flightNid": 121,
                    "label": "Fuel",
                    "status": [{"caption": "CONFIRMED"}],
                    "comment": "private",
                },
                {"flightNid": 122, "label": "Another flight", "status": []},
            ]
        }
    )
    monkeypatch.setattr(charter_intelligence, "call_leon", mcp)
    with TestClient(create_app()) as client:
        headers = signed_headers()
        query = {"day": DAY, "end_day": "2026-11-26"}
        sked = client.get(BASE + "/schedule", params=query, headers=headers).json()
        version = next(leg["version"] for leg in sked["flights"] if leg["id"] == 121)
        path = BASE + "/flights/121/checklist"
        params = {"day": DAY, "week": DAY, "version": "0" * 24}
        assert client.get(path, params=params, headers=headers).status_code == 409
        mcp.assert_not_called()
        for _ in range(2):
            response = client.get(
                path, params={**params, "version": version}, headers=headers
            )
            assert response.json()["items"] == [
                {"item": "FUEL", "statuses": ["CONFIRMED"]}
            ]
        mcp.assert_awaited_once_with(
            "get-flight-list-ops-checklist-items", {"flightNidList": [121]}
        )


def test_sales_permissions_are_checked_before_source_or_cache(sources, monkeypatch):
    requests = AsyncMock(return_value=[{"id": 123, "label": "RFQ"}])
    monkeypatch.setattr(ops_views, "list_charter_requests", requests)
    with TestClient(create_app()) as client:
        headers = signed_headers()
        path = BASE + "/charter/sales"
        assert client.get(path, headers=headers).status_code == 403
        assert client.get(path + "/123", headers=headers).status_code == 403
        requests.assert_not_called()
        monkeypatch.setattr(settings, "charter_sales_user_ids", "42")
        response = client.get(path, headers=headers)
        assert response.json()["requests"] == [{"id": 123, "label": "RFQ"}]
        monkeypatch.setattr(settings, "charter_sales_user_ids", "")
        assert client.get(path, headers=headers).status_code == 403
        requests.assert_awaited_once()


def test_documents_filter_unmatched_aircraft_and_unsafe_links(sources, monkeypatch):
    async def provider(tool, params):
        if tool == "get-fleet":
            return [{"aircraftNid": 1, "registration": "TEST-1"}]
        assert params["aircraftNidList"] == [1]
        return [
            {"aircraftNid": 2, "documentName": "Unmatched"},
            {
                "aircraftNid": 1,
                "aircraftRegistration": "TEST-1",
                "documentName": "Certificate",
                "files": [
                    {"fileName": "Unsafe", "signedUrl": "javascript:alert(1)"},
                    {
                        "fileName": "Valid",
                        "signedUrl": "https://example.test/valid.pdf",
                    },
                ],
            },
        ]

    mcp = AsyncMock(side_effect=provider)
    monkeypatch.setattr(ops_views, "call_leon", mcp)
    with TestClient(create_app()) as client:
        path = BASE + "/fleet/aircraft/1/documents"
        assert client.get(path).status_code == 401
        mcp.assert_not_called()
        result = client.get(path, headers=signed_headers()).json()
        assert result["status"] == "PARTIAL"
        assert result["documents"][0]["files"] == [
            {"name": "Valid", "url": "https://example.test/valid.pdf"}
        ]
        assert len(result["documents"]) == 1
