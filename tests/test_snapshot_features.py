import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from config.access import current_chat_id, current_user_id
from services import ai_analysis, flight_duty, flight_foreflight, snapshot
from services.briefing_notams import applicability, extract_notams
from services.snapshot_models import utc
from services.source_cache import SourceCache
from services.station_notes import NotesRepository
from services.weather_reports import metar_age_warning

FLIGHT = {
    "flightNid": 7,
    "flightNumber": "ABC205",
    "departure": "UAAA",
    "arrival": "UGTB",
    "departureUTC": "2026-11-12T23:30:00Z",
    "arrivalUTC": "2026-11-13T02:30:00Z",
    "aircraft": "TEST",
}
BRIEFING = """NOTAMs
Departure, Adequate UAAA - Departure
RUNWAY CLOSED
A0001/26 NOTAMN
Q) UAAA/QMRLC/IV/NBO/A/000/999/0000N00000E005
A) UAAA B) 2611120000 C) 2611132359
E) RWY CLOSED.
DAILY RESTRICTION
A0002/26 NOTAMN
A) UAAA B) 2611120000 C) 2611142359
D) DAILY 1000-1100
E) CHECK SCHEDULE.
Destination, Adequate UGTB - Destination
TAXIWAY CLOSED
B0001/26 NOTAMN
A) UGTB B) 2611120000 C) PERM
E) TWY CLOSED.
Alternate 1 UACC - Alternate
RUNWAY CLOSED
C0001/26 NOTAMN
A) UACC B) 2611120000 C) PERM
E) RWY CLOSED.
"""


@pytest.fixture
def identity():
    user = current_user_id.set(42)
    chat = current_chat_id.set(42)
    yield
    current_chat_id.reset(chat)
    current_user_id.reset(user)


def test_briefing_separates_stations_and_preserves_schedule_unknown():
    records, recognized = extract_notams(BRIEFING, ["UAAA", "UGTB"])
    assert recognized == {"UAAA", "UGTB"}
    assert [r.identifier for r in records["UAAA"]] == ["A0001/26", "A0002/26"]
    assert len(records["UGTB"]) == 1
    now = utc("2026-11-12T23:30:00Z")
    assert records["UAAA"][0].active_at(now) is True
    assert records["UAAA"][1].active_at(now) is None
    assert "RWY CLOSED." in records["UAAA"][0].raw
    assert records["UGTB"][0].permanent


def test_notam_now_differs_from_flight_and_expiry_is_exclusive():
    records, _ = extract_notams(BRIEFING, ["UAAA"])
    n = records["UAAA"][0]
    assert n.active_at(n.ends) is False
    assert n.overlaps(utc("2026-11-14T00:00:00Z"), utc("2026-11-14T01:00:00Z")) is False


def test_cancelled_and_replaced_records_are_not_counted_active():
    text = BRIEFING.replace(
        "DAILY RESTRICTION",
        "CANCELLED\nA0003/26 NOTAMC A0001/26\nA) UAAA B) 2611121200 C) 2611142359\nE) CANCELLED.\nDAILY RESTRICTION",
    )
    rows, _ = extract_notams(text, ["UAAA"])
    values = dict(
        (n.identifier, status)
        for n, status in applicability(
            rows["UAAA"], utc("2026-11-12T23:00:00Z"), utc("2026-11-13T03:00:00Z")
        )
    )
    assert values["A0001/26"] is False
    assert values["A0003/26"] is False


def test_unrecognized_briefing_does_not_report_zero_notams():
    with pytest.raises(ValueError):
        extract_notams("Unknown PDF contents", ["UAAA"])


@pytest.mark.asyncio
async def test_cache_coalesces_loads_and_does_not_cache_errors():
    cache = SourceCache(1)
    called = 0

    async def load():
        nonlocal called
        called += 1
        await asyncio.sleep(0)
        return called

    assert (
        await asyncio.gather(*(cache.get("x", 10, load) for _ in range(4))) == [1] * 4
    )
    assert not cache.locks

    async def fail():
        raise RuntimeError()

    with pytest.raises(RuntimeError):
        await cache.get("y", 10, fail)
    assert "y" not in cache.entries
    assert await cache.get("x", 10, load, refresh=True) == 2


@pytest.mark.asyncio
async def test_duty_crosses_midnight_without_inventing_rest(identity, monkeypatch):
    crew = [
        {
            "flightNid": 7,
            "crewMemberList": [
                {
                    "login": {"loginNid": 123},
                    "contact": {"name": "Test"},
                    "position": {"name": "PIC"},
                }
            ],
        }
    ]

    async def call(name, args):
        if name == "get-crew-ftl-sheet":
            return {"error": "Internal server error"}
        return [
            {
                "login": {"loginNid": 123},
                "definition": {"cdrType": "duty"},
                "startTimeIso": "2026-11-12T22:00:00Z",
                "endTimeIso": "2026-11-13T04:00:00Z",
            },
            {
                "login": {"loginNid": 123},
                "definition": {"cdrType": "duty"},
                "startTimeIso": "2026-11-14T12:00:00Z",
                "endTimeIso": "2026-11-14T16:00:00Z",
            },
        ]

    monkeypatch.setattr(flight_duty, "call_leon", call)
    block = await flight_duty.load_duty(FLIGHT, crew)
    assert "06:00" in block.text
    assert "14 Nov 2026 12:00Z" in block.text
    assert "Следующий допустимый старт после отдыха: не проверено" in block.text
    assert "Duty по графику" in block.details["summary"]


@pytest.mark.asyncio
async def test_member_nid_is_never_used_as_login_nid(identity, monkeypatch):
    call = AsyncMock()
    monkeypatch.setattr(flight_duty, "call_leon", call)
    block = await flight_duty.load_duty(
        FLIGHT, [{"flightNid": 7, "crewMemberList": [{"crewMemberNid": 123}]}]
    )
    call.assert_not_awaited()
    assert "loginNid" in block.text


@pytest.mark.asyncio
async def test_snapshot_partial_errors_preserve_crew_and_weather(identity, monkeypatch):
    monkeypatch.setattr(snapshot, "cache", SourceCache())
    monkeypatch.setattr(
        snapshot,
        "get_flight_schedule",
        AsyncMock(return_value={"flights": [FLIGHT], "complete": True}),
    )
    monkeypatch.setattr(
        snapshot, "get_flight_crew", AsyncMock(return_value={"crew": []})
    )
    monkeypatch.setattr(snapshot, "load_duty", AsyncMock(side_effect=TimeoutError()))
    monkeypatch.setattr(
        snapshot,
        "selected_flight_weather",
        AsyncMock(return_value={"answer": "METAR TEST"}),
    )
    monkeypatch.setattr(
        snapshot, "load_foreflight", AsyncMock(side_effect=RuntimeError())
    )
    result = await snapshot.load_flight_snapshot(FLIGHT)
    assert result.blocks["crew"].status == "ready"
    assert "METAR TEST" in result.blocks["weather"].text
    assert result.blocks["duty"].status == "unavailable"
    assert "количество не определено" in result.blocks["notams"].text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ff_departure,registration",
    [
        ("2026-11-12T23:30:00Z", "TEST"),
        ("2026-11-12T21:30:00Z", "TEST"),
        ("2026-11-12T23:30:00Z", "OTHER"),
    ],
)
async def test_foreflight_ete_survives_pdf_failure(
    monkeypatch, ff_departure, registration
):
    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        get_flight = AsyncMock(
            return_value={
                "performance": {
                    "times": {"timeToDestinationMinutes": 90, "totalTimeMinutes": 500}
                }
            }
        )
        get_document = AsyncMock(return_value={"url": "https://example.test/document"})
        get_overflight = AsyncMock(return_value={"overflownCountries": []})

    monkeypatch.setattr(flight_foreflight, "ForeFlightClient", Client)
    monkeypatch.setattr(
        flight_foreflight,
        "find_flight",
        AsyncMock(
            return_value=(
                {
                    "flightId": "test",
                    "departure": "UAAA",
                    "destination": "UGTB",
                    "departureTime": ff_departure,
                    "aircraftRegistration": registration,
                },
                "test",
            )
        ),
    )
    monkeypatch.setattr(
        flight_foreflight, "download_briefing", AsyncMock(side_effect=ValueError())
    )
    if registration == "OTHER":
        with pytest.raises(ValueError, match="aircraft"):
            await flight_foreflight.load_foreflight(FLIGHT)
        Client.get_document.assert_not_awaited()
        return
    result = await flight_foreflight.load_foreflight(FLIGHT)
    assert "01:30" in result["flighttime"].text
    assert "08:20" not in result["flighttime"].text
    assert result["notams"].status == "unavailable"
    if ff_departure != FLIGHT["departureUTC"]:
        assert "Расхождение вылета" in result["flighttime"].text


def test_notes_identity_permissions_idempotency_and_audit(tmp_path):
    repo = NotesRepository(tmp_path / "notes.db")
    repo.bind(1, "employee-1", "Crew")
    repo.bind(2, "employee-2", "Crew")
    repo.bind(3, "employee-3", "Ops")
    with pytest.raises(PermissionError):
        repo.list(99, "UAAA")
    note_id = repo.create(1, "UAAA", "Test observation", "request-1")
    assert repo.create(1, "UAAA", "Test observation", "request-1") == note_id
    with pytest.raises(PermissionError):
        repo.change(2, note_id, 1, "edit", "Unauthorized")
    with pytest.raises(PermissionError):
        repo.change(1, note_id, 1, "verify")
    repo.change(3, note_id, 1, "verify")
    assert repo.list(2, "UAAA")[0]["verified_by"] == 3
    with pytest.raises(ValueError):
        repo.change(1, note_id, 1, "edit", "Stale edit")
    repo.change(1, note_id, 2, "edit", "Updated observation")
    assert repo.list(2, "UAAA")[0]["verified_by"] is None
    repo.change(1, note_id, 3, "archive")
    assert repo.list(2, "UAAA") == []
    with repo.connect() as conn:
        history = conn.execute(
            "SELECT record_json FROM station_note_history WHERE note_id=? ORDER BY id",
            (note_id,),
        ).fetchall()
    assert len(history) == 4
    assert "Test observation" in history[0][0]


@pytest.mark.asyncio
async def test_ai_unknown_citations_and_offline_are_safe(monkeypatch):
    monkeypatch.setattr(ai_analysis, "cache", SourceCache())
    monkeypatch.setattr(ai_analysis.agent, "_inference_slots", asyncio.Semaphore(1))
    chat = AsyncMock(
        return_value=SimpleNamespace(
            message=SimpleNamespace(
                content='{"summary":"test","items":[{"severity":"info","text":"test","source_ids":["invented"]}],"limitations":[]}'
            )
        )
    )
    monkeypatch.setattr(ai_analysis.agent.client, "chat", chat)
    assert "временно недоступен" in await ai_analysis.analyze_sources(
        {"real": "fact"}, (1, 1)
    )
    chat.side_effect = OSError()
    assert "временно недоступен" in await ai_analysis.analyze_sources(
        {"real": "fact"}, (1, 1)
    )
    assert chat.call_args.kwargs["think"] is False
    assert "tools" not in chat.call_args.kwargs


def test_old_metar_and_unknown_observation_time_are_explicit():
    from datetime import UTC, datetime

    now = datetime(2026, 11, 12, 12, tzinfo=UTC)
    assert "устарел" in metar_age_warning("UAAA 120800Z 00000KT", "", now)
    assert "не определено" in metar_age_warning("UAAA NIL", "", now)
    assert not metar_age_warning("UAAA 121130Z 00000KT", "", now)


@pytest.mark.asyncio
async def test_private_note_preview_save_double_callback_and_group_denial(
    tmp_path, monkeypatch
):
    from bot import station_notes as ui

    repo = NotesRepository(tmp_path / "private.db")
    repo.bind(42, "employee-42", "Crew")
    monkeypatch.setattr(ui, "notes", repo)
    context = SimpleNamespace(user_data={})
    message = SimpleNamespace(
        text="",
        reply_markup=None,
        reply_text=AsyncMock(return_value=SimpleNamespace(message_id=900)),
        reply_to_message=None,
    )
    query = SimpleNamespace(
        data="sn:add:UAAA:0", answer=AsyncMock(), edit_message_text=AsyncMock()
    )
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id=42, type="private"),
        effective_user=SimpleNamespace(id=42, is_bot=False),
        effective_message=message,
        callback_query=query,
    )
    await ui.private_callback(update, context)
    draft = context.user_data["station_note_draft"]
    message.text = "Test observation"
    message.reply_to_message = SimpleNamespace(message_id=900)
    await ui.private_message(update, context)
    assert repo.list(42, "UAAA") == []
    query.data = f"sn:save:{draft['token']}:0"
    await ui.private_callback(update, context)
    await ui.private_callback(update, context)
    assert len(repo.list(42, "UAAA")) == 1
    assert "station_note_draft" not in context.user_data
    update.effective_chat.type = "supergroup"
    query.data = "sn:list:UAAA:0"
    query.edit_message_text.reset_mock()
    message.reply_text.reset_mock()
    await ui.private_callback(update, context)
    query.edit_message_text.assert_not_awaited()
    message.reply_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_note_draft_expiry_does_not_write(tmp_path, monkeypatch):
    from bot import station_notes as ui

    repo = NotesRepository(tmp_path / "expired.db")
    repo.bind(42, "employee-42", "Crew")
    monkeypatch.setattr(ui, "notes", repo)
    context = SimpleNamespace(
        user_data={
            "station_note_draft": {
                "token": "draft",
                "chat": 42,
                "station": "UAAA",
                "expires": 0,
                "text": "Expired",
            }
        }
    )
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id=42, type="private"),
        effective_user=SimpleNamespace(id=42, is_bot=False),
        effective_message=SimpleNamespace(reply_text=AsyncMock()),
        callback_query=SimpleNamespace(data="sn:save:draft:0", answer=AsyncMock()),
    )
    await ui.private_callback(update, context)
    assert repo.list(42, "UAAA") == []
