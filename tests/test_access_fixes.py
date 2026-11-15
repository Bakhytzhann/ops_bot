from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import BadRequest, Forbidden

from bot import bot
from bot.access import access_store
from bot.admission import handle_access_button, show_access
from bot.keyboards import SCHEDULE_BUTTON, home_keyboard
from bot.message_edits import edit_message_text
from tests.test_admission import context as context
from tests.test_admission import update


@pytest.mark.asyncio
async def test_revoked_start_creates_new_revision_and_keeps_old_notices(context):
    await bot.handle_message(update(), context)
    first = access_store.get(99)
    access_store.decide(7, 99, "approve", first["revision"])
    approved = access_store.get(99)
    access_store.decide(7, 99, "revoke", approved["revision"])
    revoked = access_store.get(99)
    context.bot.send_message.reset_mock()

    event = update()
    await bot.handle_message(event, context)
    pending = access_store.get(99)
    assert pending["status"] == "pending"
    assert pending["revision"] == revoked["revision"] + 1
    assert pending["decided_by"] == revoked["decided_by"]
    assert not access_store.is_allowed(99)
    assert context.bot.send_message.await_count == 2
    for call in context.bot.send_message.call_args_list:
        callbacks = [
            b.callback_data
            for row in call.kwargs["reply_markup"].inline_keyboard
            for b in row
        ]
        assert callbacks == [
            f"access:approve:99:{pending['revision']}",
            f"access:reject:99:{pending['revision']}",
        ]
    assert access_store.notified(99, first["revision"], 7)
    assert access_store.notified(99, pending["revision"], 7)
    assert (
        event.effective_message.reply_text.call_args.args[0]
        == "Заявка на повторное предоставление доступа отправлена администратору"
    )
    with pytest.raises(ValueError):
        access_store.decide(7, 99, "approve", first["revision"])

    context.bot.send_message.reset_mock()
    repeated = update()
    await bot.handle_message(repeated, context)
    assert access_store.get(99) == pending
    context.bot.send_message.assert_not_awaited()
    assert "на рассмотрении" in repeated.effective_message.reply_text.call_args.args[0]

    await handle_access_button(
        update(user=7, data=f"access:approve:99:{pending['revision']}"), context
    )
    assert access_store.is_allowed(99)


@pytest.mark.asyncio
async def test_pending_start_keeps_revision_and_does_not_resend(context):
    await bot.handle_message(update(), context)
    before = access_store.get(99)
    context.bot.send_message.reset_mock()
    event = update()
    await bot.handle_message(event, context)
    assert access_store.get(99) == before
    context.bot.send_message.assert_not_awaited()
    assert "на рассмотрении" in event.effective_message.reply_text.call_args.args[0]


@pytest.mark.asyncio
async def test_identical_content_and_markup_response_is_harmless():
    markup = home_keyboard()
    query = SimpleNamespace(
        message=SimpleNamespace(text="unchanged", reply_markup=markup),
        edit_message_text=AsyncMock(side_effect=BadRequest("Message is not modified")),
    )
    await edit_message_text(query, "unchanged", reply_markup=markup)
    query.edit_message_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_changed_markup_is_edited():
    query = SimpleNamespace(
        message=SimpleNamespace(text="same", reply_markup=None),
        edit_message_text=AsyncMock(),
    )
    await edit_message_text(query, "same", reply_markup=home_keyboard())
    query.edit_message_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_message_not_modified_is_treated_as_success():
    query = SimpleNamespace(
        edit_message_text=AsyncMock(
            side_effect=BadRequest(
                "Message is not modified: specified new message content and reply markup are exactly the same"
            )
        )
    )
    await edit_message_text(query, "same")
    query.edit_message_text.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error", [BadRequest("Message to edit not found"), Forbidden("Forbidden")]
)
async def test_other_telegram_errors_propagate(error):
    query = SimpleNamespace(edit_message_text=AsyncMock(side_effect=error))
    with pytest.raises(type(error)):
        await edit_message_text(query, "text")


@pytest.mark.asyncio
async def test_repeated_dashboard_callback_does_not_fail(context):
    event = update(user=7, data="access:list:0")
    event.callback_query.edit_message_text.side_effect = BadRequest(
        "Message is not modified"
    )
    await handle_access_button(event, context)
    await handle_access_button(event, context)
    assert event.callback_query.answer.await_count == 2


@pytest.mark.asyncio
async def test_dashboard_handles_known_identical_message(context):
    event = update(user=7)
    await show_access(event, context)
    call = event.effective_message.reply_text.call_args
    event.callback_query.message = SimpleNamespace(
        text=call.args[0], reply_markup=call.kwargs["reply_markup"]
    )
    event.callback_query.edit_message_text.side_effect = BadRequest(
        "Message is not modified"
    )
    await show_access(event, context, edit=True)
    event.callback_query.edit_message_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_approval_survives_unmodified_message_and_repeated_callback(context):
    access_store.request(99)
    event = update(user=7, data="access:approve:99:1")
    event.callback_query.edit_message_text.side_effect = BadRequest(
        "Message is not modified"
    )
    await handle_access_button(event, context)
    assert access_store.is_allowed(99)
    assert context.bot.send_message.await_count == 1
    assert context.bot.send_message.call_args.kwargs["reply_markup"] == home_keyboard()
    before = access_store.get(99)
    await handle_access_button(event, context)
    assert access_store.get(99) == before
    assert context.bot.send_message.await_count == 1
    assert event.callback_query.answer.call_args.kwargs["show_alert"]


@pytest.mark.asyncio
async def test_approval_and_notification_survive_other_edit_error(context):
    access_store.request(99)
    event = update(user=7, data="access:approve:99:1")
    event.callback_query.edit_message_text.side_effect = BadRequest(
        "Message to edit not found"
    )
    with pytest.raises(BadRequest, match="Message to edit not found"):
        await handle_access_button(event, context)
    assert access_store.is_allowed(99)
    assert context.bot.send_message.call_args.kwargs["chat_id"] == 99


@pytest.mark.asyncio
async def test_repeated_inline_menu_handles_unmodified_response(context):
    event = update(user=42, data="op:group_preparation")
    event.callback_query.edit_message_text.side_effect = BadRequest(
        "Message is not modified"
    )
    await bot.handle_button(event, context)
    await bot.handle_button(event, context)
    assert event.callback_query.answer.await_count == 2


def test_reply_keyboard_has_six_quick_actions():
    keyboard = home_keyboard()
    assert SCHEDULE_BUTTON == "SKED"
    assert [[button.text for button in row] for row in keyboard.keyboard] == [
        ["GROUND STATUS", "SKED", "OPS BRIEF"],
        ["FLEET DOCS", "ANALYTICS", "CHARTER CHECK"],
    ]
    assert keyboard.is_persistent
    assert not keyboard.one_time_keyboard


@pytest.mark.asyncio
@pytest.mark.parametrize("user", [42, 99])
async def test_start_responds_for_allowed_and_pending_users(context, user):
    event = update(user=user)
    await bot.handle_message(event, context)
    event.effective_message.reply_text.assert_awaited_once()
    if user == 42:
        assert "шесть кнопок" in event.effective_message.reply_text.call_args.args[0]


@pytest.mark.asyncio
async def test_schedule_button_runs_existing_sked(context, monkeypatch):
    show = AsyncMock()
    monkeypatch.setattr(bot, "show_week", show)
    event = update(user=42, text="SKED")
    await bot.handle_message(event, context)
    show.assert_awaited_once_with(event, context, edit=False)


@pytest.mark.asyncio
async def test_sequential_edits_do_not_trust_original_callback_message():
    query = SimpleNamespace(
        message=SimpleNamespace(text="original", reply_markup=None),
        edit_message_text=AsyncMock(),
    )
    await edit_message_text(query, "loading")
    await edit_message_text(query, "original")
    assert [call.args[0] for call in query.edit_message_text.call_args_list] == [
        "loading",
        "original",
    ]
