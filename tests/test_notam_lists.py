import re
from datetime import UTC, datetime, timedelta
from time import monotonic
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot import flight_browser
from bot.notam_browser import PAGE_BUDGET, render_notams, valid_action
from services.notam_filters import notam_sets
from services.snapshot_models import FlightSnapshot, Notam, SourceBlock
from tests.test_weekly_browser import update


def snapshot_for(sizes):
    now = datetime(2026, 11, 16, tzinfo=UTC)
    records = [
        Notam(
            identifier=f"A{index:04d}/26",
            station="ZBAD",
            kind="N",
            replaces=None,
            raw=f"A{index:04d}/26 NOTAMN\nE) "
            + chr(65 + index % 26) * size
            + f"\nEND-{index:04d}\n",
            starts=now - timedelta(days=1),
            ends=now + timedelta(days=2),
            topic=f"TOPIC {index:04d}",
        )
        for index, size in enumerate(sizes)
    ]
    snapshot = FlightSnapshot(
        {
            "flightNid": 1,
            "flightNumber": "ABC123",
            "departure": "ZBAD",
            "arrival": "UCFM",
            "departureUTC": (now + timedelta(hours=2)).isoformat(),
            "arrivalUTC": (now + timedelta(hours=4)).isoformat(),
        },
        blocks={
            "notams": SourceBlock(
                "ForeFlight briefing",
                fetched_at=now,
                details={
                    "notams": {"ZBAD": records, "UCFM": []},
                    "recognized": {"ZBAD", "UCFM"},
                },
            )
        },
    )
    return snapshot, records


def buttons(markup):
    return [button for row in markup.inline_keyboard for button in row]


def pages_for(snapshot, action="nlD_T"):
    result, page = [], 0
    while True:
        text, markup = render_notams(snapshot, "token", 0, action, page)
        assert len(text.encode("utf-16-le")) // 2 <= PAGE_BUDGET
        assert len(text) <= PAGE_BUDGET
        assert all(
            len(button.callback_data.encode()) <= 64 for button in buttons(markup)
        )
        assert not any(":nv" in button.callback_data for button in buttons(markup))
        result.append((text, markup))
        next_button = next(
            (button for button in buttons(markup) if button.text == "NEXT"), None
        )
        if next_button is None:
            return result
        next_page = int(next_button.callback_data.rsplit(":", 1)[1])
        assert next_page == page + 1
        page = next_page
        assert page < 1000


@pytest.mark.parametrize("count", [5, 50, 150])
def test_complete_lists_have_no_lost_or_duplicated_records(count):
    snapshot, records = snapshot_for([400] * count)
    pages = pages_for(snapshot)
    text = "\n".join(page[0] for page in pages)
    for record in records:
        assert text.count(record.raw) == 1
        assert record.topic in text
    ranges = [
        re.search(r"PAGE (\d+)/(\d+) · (\d+)–(\d+) / (\d+)", page[0]) for page in pages
    ]
    assert all(
        match and int(match[2]) == len(pages) and int(match[5]) == count
        for match in ranges
    )
    seen = [
        index for match in ranges for index in range(int(match[3]), int(match[4]) + 1)
    ]
    assert seen == list(range(1, count + 1))
    assert len(pages) == 1 if count == 5 else len(pages) > 1


def test_different_sizes_pack_dynamically_and_keep_complete_records():
    snapshot, records = snapshot_for([1500, 1500, 1500] + [40] * 20)
    pages = pages_for(snapshot)
    counts = []
    for text, _ in pages:
        counts.append(sum(record.raw in text for record in records))
    assert max(counts) > min(counts)
    assert max(counts) > 5
    assert sum(counts) == len(records)
    assert any(len(text) > 3500 for text, _ in pages)


@pytest.mark.parametrize(
    "raw",
    [
        "  ORIGINAL\n" + "LONG LINE\n" * 2000 + "\n  END  ",
        "🛫" * 6000 + "\nUNICODE END",
    ],
)
def test_oversized_notam_reassembles_exact_original_text(raw):
    snapshot, records = snapshot_for([1])
    record = records[0]
    record.raw = raw
    pages = pages_for(snapshot)
    assert len(pages) > 1
    fragments = []
    for index, (text, _) in enumerate(pages, 1):
        body = text.split("\n\n", 1)[1]
        label, _, fragment = body.partition("\n")
        assert record.identifier in label
        assert f"PART {index}/{len(pages)}" in label
        if index > 1:
            assert "CONTINUED" in label
        assert "1–1 / 1" in text
        fragments.append(fragment)
    assert "".join(fragments) == record.topic + "\n" + raw


def test_full_text_and_topic_preserve_whitespace():
    snapshot, records = snapshot_for([20])
    records[0].topic = "TOPIC  WITH\nEXACT SPACING"
    records[0].raw = "\n\nORIGINAL  TEXT\n\tTAB\n\n"
    text, _ = render_notams(snapshot, "token", 0, "nlD_T")
    assert records[0].topic + "\n" + records[0].raw in text


def test_next_previous_are_stable_and_only_controls_are_present():
    snapshot, _ = snapshot_for([700] * 50)
    pages = pages_for(snapshot)
    for index in range(len(pages) - 1, -1, -1):
        text, markup = render_notams(snapshot, "token", 0, "nlD_T", index)
        assert (text, markup) == pages[index]
        assert [button.text for button in markup.inline_keyboard[0]] == [
            "DEP ZBAD",
            "ARR UCFM",
        ]
        assert [button.text.split()[0] for button in markup.inline_keyboard[1]] == [
            "AFFECT",
            "REVIEW",
            "ACTIVE",
            "ALL",
        ]
        assert markup.inline_keyboard[-1][0].text == "BACK · Flight Card"
        assert {button.text for button in buttons(markup)[6:-1]} <= {"PREVIOUS", "NEXT"}
        previous = next(
            (button for button in buttons(markup) if button.text == "PREVIOUS"), None
        )
        assert bool(previous) == (index > 0)
        if previous:
            assert previous.callback_data == f"fc:token:0:nlD_T:{index - 1}"


@pytest.mark.parametrize("selected", ["A", "R", "C", "T"])
def test_filter_counts_and_lists_reuse_existing_classification(selected):
    snapshot, records = snapshot_for([400] * 12)
    now = snapshot.blocks["notams"].fetched_at
    records[1].schedule = "DAILY 1000-1100"
    records[2].starts = now + timedelta(hours=1)
    records[3].ends = now - timedelta(hours=2)
    groups, _ = notam_sets(snapshot, "ZBAD")
    pages = pages_for(snapshot, "nlD_" + selected)
    text = "\n".join(page[0] for page in pages)
    expected = [record for _, record in groups[selected]]
    for record in records:
        assert text.count(record.raw) == int(record in expected)
    assert [button.text for button in pages[0][1].inline_keyboard[1]] == [
        f"{name} {len(groups[key])}"
        for key, name in (
            ("A", "AFFECT"),
            ("R", "REVIEW"),
            ("C", "ACTIVE"),
            ("T", "ALL"),
        )
    ]
    assert f"/ {len(expected)}" in pages[0][0]


def test_station_switch_preserves_filter_and_resets_page():
    snapshot, records = snapshot_for([900] * 8)
    arrival = Notam(
        "B0001/26",
        "UCFM",
        "N",
        None,
        "B0001/26\nE) ARRIVAL",
        records[0].starts,
        records[0].ends,
    )
    snapshot.blocks["notams"].details["notams"]["UCFM"] = [arrival]
    _, markup = render_notams(snapshot, "token", 0, "nlD_C", 1)
    action, page = markup.inline_keyboard[0][1].callback_data.split(":")[3:]
    assert action == "nlA_C" and page == "0"
    text, arrival_markup = render_notams(snapshot, "token", 0, action, int(page))
    assert "NOTAM · ARR UCFM · ACTIVE 1" in text
    assert arrival.raw in text and records[0].raw not in text
    assert arrival_markup.inline_keyboard[0][0].callback_data == "fc:token:0:nlD_C:0"


@pytest.mark.parametrize("recognized", [True, False])
def test_empty_and_unrecognized_sections_remain_distinct(recognized):
    snapshot, _ = snapshot_for([])
    if not recognized:
        snapshot.blocks["notams"].details["recognized"].remove("ZBAD")
    text, markup = render_notams(snapshot, "token", 0, "nlD_T")
    assert "PAGE 1/1 · 0–0 / 0" in text
    assert ("нет записей" if recognized else "нет данных") in text
    assert markup.inline_keyboard[1][-1].text == ("ALL 0" if recognized else "ALL —")
    assert len(markup.inline_keyboard) == 3


@pytest.mark.parametrize(
    "action,page",
    [
        ("nlD_T", -1),
        ("nlD_T", 999),
        ("nvD_T_0_00000000", 0),
        ("nlX_T", 0),
    ],
)
def test_invalid_pages_and_obsolete_record_actions_are_rejected(action, page):
    snapshot, _ = snapshot_for([20])
    with pytest.raises(ValueError):
        render_notams(snapshot, "token", 0, action, page)
    assert valid_action(action) == (action == "nlD_T")


@pytest.mark.asyncio
async def test_notam_next_previous_callbacks_use_cached_card_without_reloading(
    monkeypatch,
):
    snapshot, _ = snapshot_for([900] * 8)
    pages = pages_for(snapshot)
    listing = {
        "scope": (42, None),
        "expires": monotonic() + 60,
        "flights": [snapshot.flight],
        "snapshots": {0: snapshot},
    }
    context = SimpleNamespace(
        bot_data={"weekly_flight_lists": {"token": listing}}, user_data={}
    )
    load = AsyncMock()
    monkeypatch.setattr(flight_browser, "load_flight_snapshot", load)
    next_button = next(
        button for button in buttons(pages[0][1]) if button.text == "NEXT"
    )
    event = update(data=next_button.callback_data)
    await flight_browser.handle_selection(event, context)
    assert event.callback_query.edit_message_text.call_args.args[0] == pages[1][0]
    previous = next(
        button for button in buttons(pages[1][1]) if button.text == "PREVIOUS"
    )
    back = update(data=previous.callback_data)
    await flight_browser.handle_selection(back, context)
    assert back.callback_query.edit_message_text.call_args.args[0] == pages[0][0]
    load.assert_not_awaited()
