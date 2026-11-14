from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import Forbidden

from bot import bot
from bot.access import AccessStore, access_store
from bot.admission import handle_access_button, request_access, show_access
from config.settings import settings


def update(user=99, text="/start", data=None, chat_type="private"):
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user, is_bot=False, first_name="Test"),
        effective_chat=SimpleNamespace(
            id=user if chat_type == "private" else -100, type=chat_type
        ),
        effective_message=SimpleNamespace(
            text=text, message_thread_id=None, reply_text=AsyncMock()
        ),
        callback_query=SimpleNamespace(
            data=data, answer=AsyncMock(), edit_message_text=AsyncMock()
        ),
    )


@pytest.fixture
def context(monkeypatch):
    monkeypatch.setattr(settings, "telegram_admin_ids", "7,8")
    return SimpleNamespace(
        user_data={},
        bot_data={},
        bot=SimpleNamespace(send_message=AsyncMock(), send_chat_action=AsyncMock()),
    )


def test_legacy_access_persists_and_revocation_survives_bootstrap(context):
    assert access_store.is_allowed(42)
    record = access_store.get(42)
    access_store.decide(7, 42, "revoke", record["revision"])
    restarted = AccessStore(settings.database_path)
    assert restarted.get(42)["status"] == "revoked"
    assert not restarted.is_allowed(42)
    assert restarted.is_allowed(7)


@pytest.mark.parametrize(
    "action,status", [("approve", "approved"), ("reject", "rejected")]
)
def test_decisions_persist_and_old_buttons_cannot_change_them(context, action, status):
    record = access_store.request(99, "New user")
    access_store.decide(7, 99, action, record["revision"])
    assert AccessStore(settings.database_path).get(99)["status"] == status
    with pytest.raises(ValueError):
        access_store.decide(8, 99, "approve", record["revision"])
    assert access_store.request(99)["status"] == status


def test_untrusted_actor_and_admin_revocation_denied(context):
    record = access_store.request(99)
    with pytest.raises(PermissionError):
        access_store.decide(99, 99, "approve", record["revision"])
    assert access_store.get(99)["status"] == "pending"
    with pytest.raises(ValueError):
        access_store.decide(7, 8, "revoke", 1)


def test_two_admins_cannot_decide_one_revision_twice(context):
    record = access_store.request(99)

    def decide(actor):
        try:
            return access_store.decide(actor, 99, "approve", record["revision"])[
                "status"
            ]
        except ValueError:
            return "stale"

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(decide, (7, 8))) == ["approved", "stale"]


@pytest.mark.asyncio
async def test_repeated_start_creates_one_request_and_one_notice_per_admin(context):
    first = update()
    await bot.handle_message(first, context)
    await bot.handle_message(update(), context)
    assert len([r for r in access_store.list() if r["user_id"] == 99]) == 1
    assert context.bot.send_message.await_count == 2
    for call in context.bot.send_message.call_args_list:
        callbacks = [
            b.callback_data
            for row in call.kwargs["reply_markup"].inline_keyboard
            for b in row
        ]
        assert callbacks == ["access:approve:99:1", "access:reject:99:1"]
    assert access_store.get(99)["status"] == "pending"


@pytest.mark.asyncio
async def test_undelivered_admin_notice_is_retried_without_new_request(context):
    context.bot.send_message.side_effect = Forbidden("not started")
    await request_access(update(), context)
    assert access_store.get(99)["revision"] == 1
    assert not access_store.notified(99, 1, 7)
    context.bot.send_message.side_effect = None
    await request_access(update(), context)
    assert access_store.notified(99, 1, 7)
    assert context.bot.send_message.await_count == 4


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "user,chat_type", [(99, "private"), (7, "group"), (7, "supergroup")]
)
async def test_forged_or_group_admin_callback_cannot_grant_access(
    context, user, chat_type
):
    access_store.request(99)
    event = update(user=user, data="access:approve:99:1", chat_type=chat_type)
    await handle_access_button(event, context)
    assert access_store.get(99)["status"] == "pending"
    event.callback_query.edit_message_text.assert_not_awaited()
    assert event.callback_query.answer.call_args.kwargs["show_alert"]


@pytest.mark.asyncio
async def test_admin_approve_menu_then_revoke_blocks_messages_and_old_buttons(
    context, monkeypatch
):
    access_store.request(99)
    await handle_access_button(update(user=7, data="access:approve:99:1"), context)
    assert access_store.is_allowed(99)
    assert context.bot.send_message.call_args.kwargs["reply_markup"] is not None
    record = access_store.get(99)
    await handle_access_button(
        update(user=7, data=f"access:revoke:99:{record['revision']}"), context
    )
    assert not access_store.is_allowed(99)
    operation = AsyncMock()
    monkeypatch.setattr(bot, "execute_command", operation)
    await bot.handle_message(update(text="/route ABC1265 на 2026-11-11"), context)
    await bot.handle_button(update(data="op:route"), context)
    operation.assert_not_awaited()
    assert not context.user_data
    await bot.handle_message(update(), context)
    assert access_store.get(99)["status"] == "pending"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "data",
    [
        "access:approve:99:NaN",
        "access:approve:99",
        "access:delete:99:1",
        "access:list:-1",
    ],
)
async def test_invalid_admin_callbacks_are_rejected(context, data):
    access_store.request(99)
    event = update(user=7, data=data)
    await handle_access_button(event, context)
    assert access_store.get(99)["status"] == "pending"
    assert event.callback_query.answer.call_args.kwargs["show_alert"]


@pytest.mark.asyncio
async def test_pending_users_cannot_use_operations_or_access_list(context, monkeypatch):
    operation = AsyncMock()
    monkeypatch.setattr(bot, "execute_command", operation)
    await bot.handle_message(update(), context)
    await bot.handle_message(update(text="/route ABC1265 на 2026-11-11"), context)
    await bot.handle_button(update(data="op:route"), context)
    await show_access(update(), context)
    operation.assert_not_awaited()
    assert not context.user_data


@pytest.mark.asyncio
async def test_access_dashboard_paginates_and_has_revoke_button(context):
    for user in range(100, 112):
        access_store.request(user)
    event = update(user=7)
    await show_access(event, context)
    markup = event.effective_message.reply_text.call_args.kwargs["reply_markup"]
    assert any(
        b.callback_data == "access:list:1"
        for row in markup.inline_keyboard
        for b in row
    )
    await handle_access_button(update(user=7, data="access:list:1"), context)
    assert access_store.get(42)["status"] == "approved"


@pytest.mark.asyncio
async def test_no_admins_records_request_without_granting_access(context, monkeypatch):
    monkeypatch.setattr(settings, "telegram_admin_ids", "")
    event = update()
    await bot.handle_message(event, context)
    context.bot.send_message.assert_not_awaited()
    assert access_store.get(99)["status"] == "pending"
    assert not access_store.is_allowed(99)


@pytest.mark.asyncio
async def test_user_is_notified_even_if_admin_screen_update_fails(context):
    access_store.request(99)
    event = update(user=7, data="access:approve:99:1")
    event.callback_query.edit_message_text.side_effect = Forbidden(
        "message unavailable"
    )
    with pytest.raises(Forbidden):
        await handle_access_button(event, context)
    assert access_store.is_allowed(99)
    assert context.bot.send_message.call_args.kwargs["chat_id"] == 99
