"""Ground Status free-form input, source selection, UTC parsing and summaries."""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from bot import ground_turns as ui
from services import ground_input
from services.ground_turns import GroundTurnStore
from tests.test_bot_commands import update_for


@pytest.mark.asyncio
async def test_qwen_interval_is_validated_and_kept_in_utc(monkeypatch):
    from services import ai_analysis

    qwen = AsyncMock(
        return_value=SimpleNamespace(
            flight="", kind="FUEL", start="1310", end="1345",
            note="", critical=False,
        )
    )
    monkeypatch.setattr(ai_analysis, "request_analysis", qwen)
    now = datetime(2026, 11, 20, 15, 0, tzinfo=UTC)
    flight, entries = await ground_input.parse_ground_input(
        "заправка 1310/1345", ("chat", 1), now=now
    )
    assert flight == ""
    assert [(kind, when) for kind, when, _ in entries] == [
        ("FUEL_STARTED", now.replace(hour=13, minute=10)),
        ("FUEL_COMPLETED", now.replace(hour=13, minute=45)),
    ]


@pytest.mark.asyncio
async def test_free_note_keeps_original_text(monkeypatch):
    from services import ai_analysis

    qwen = AsyncMock(
        return_value=SimpleNamespace(
            flight="", kind="NOTE", start="", end="",
            note="ТЗ поздно приехал из-за пробки", critical=False,
        )
    )
    monkeypatch.setattr(ai_analysis, "request_analysis", qwen)
    _, entries = await ground_input.parse_ground_input(
        "ТЗ поздно приехал из-за пробки", ("chat", 1)
    )
    assert entries[0][0] == "NOTE"
    assert entries[0][2]["text"] == "ТЗ поздно приехал из-за пробки"


@pytest.mark.asyncio
async def test_explicit_flight_updates_card_and_deletes_message(monkeypatch):
    now = datetime.now(UTC) - timedelta(hours=1)
    turn = {
        "key": "UAAA:o:123", "station": "UAAA", "outbound_id": 123,
        "outbound_number": "ABC123", "registration": "UP-ABC",
        "status": "active", "reference_time": now,
    }
    monkeypatch.setattr(ui, "require_user", lambda _: None)
    monkeypatch.setattr(
        ui,
        "load_ground_flights",
        AsyncMock(return_value={"cards": [turn], "fetched_at": now}),
    )
    monkeypatch.setattr(
        ui,
        "parse_ground_input",
        AsyncMock(return_value=("ABC123", [("CHOCKS_ON", now, {})])),
    )
    record = Mock(return_value=True)
    monkeypatch.setattr(
        ui,
        "ground_turns",
        SimpleNamespace(
            record=record, observe=Mock(), events=Mock(return_value=[]),
        ),
    )
    message = update_for("ABC123 колодки 1245z")
    message.effective_message.message_id = 555
    context = SimpleNamespace(
        user_data={},
        bot=SimpleNamespace(delete_message=AsyncMock()),
    )
    await ui.handle_ground_text(message, context)
    assert record.call_args.args == (turn, "CHOCKS_ON")
    assert record.call_args.kwargs["request_id"] == "message:42:555:0"
    context.bot.delete_message.assert_awaited_once_with(
        chat_id=42, message_id=555
    )


def test_station_statistics_only_use_manual_records_and_keep_critical_notes(tmp_path):
    store = GroundTurnStore(tmp_path / "ground.sqlite")
    now = datetime.now(UTC)
    turn = {
        "key": "UAAA:o:123", "station": "UAAA",
        "outbound_id": 123, "inbound_id": None,
        "planned_out": now, "planned_in": None,
    }
    store.observe({**turn, "actual_out": now})
    assert store.events(turn["key"]) == []
    store.record(
        turn, "NOTE", actor_id=42, event_at=now,
        request_id="message:42:1",
        detail={"text": "Тележка зацепила крыло ВС", "critical": True},
    )
    store.record(
        turn, "FUEL_STARTED", actor_id=42,
        event_at=now - timedelta(minutes=35), request_id="message:42:2",
    )
    store.record(
        turn, "FUEL_COMPLETED", actor_id=42,
        event_at=now - timedelta(minutes=10), request_id="message:42:3",
    )
    report = store.station_report("UAAA")
    assert report["turns"] == 1
    assert report["note_count"] == 1
    assert report["critical_count"] == 1
    assert report["metrics"]["Заправка"]["mean_minutes"] == 25
