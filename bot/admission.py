import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError

from bot.access import access_store
from bot.keyboards import home_keyboard
from bot.message_edits import edit_message_text
from config.settings import settings

logger = logging.getLogger(__name__)


def private_user(update):
    chat, user = update.effective_chat, update.effective_user
    return bool(
        chat
        and user
        and chat.type == "private"
        and chat.id == user.id
        and not user.is_bot
    )


def is_allowed(update):
    return private_user(update) and access_store.is_allowed(update.effective_user.id)


def require_user(update):
    if not is_allowed(update):
        raise PermissionError("Authorized private chat required")


def decision_buttons(record):
    user, revision = record["user_id"], record["revision"]
    actions = (
        (("Approve", "approve"), ("Reject", "reject"))
        if record["status"] == "pending"
        else (("Revoke", "revoke"),)
        if record["status"] == "approved"
        else ()
    )
    return [
        InlineKeyboardButton(
            f"{label} · {user}", callback_data=f"access:{action}:{user}:{revision}"
        )
        for label, action in actions
    ]


async def request_access(update, context):
    if not private_user(update):
        return
    user = update.effective_user
    record = access_store.request(
        user.id, getattr(user, "full_name", None) or getattr(user, "first_name", "")
    )
    if record["status"] == "pending":
        for admin in settings.admin_ids:
            if access_store.notified(user.id, record["revision"], admin):
                continue
            try:
                await context.bot.send_message(
                    chat_id=admin,
                    text=f"Заявка на доступ OPS Bot\n{record['name']}\nTelegram ID: {user.id}",
                    reply_markup=InlineKeyboardMarkup([decision_buttons(record)]),
                )
                access_store.mark_notified(user.id, record["revision"], admin)
            except TelegramError:
                logger.warning("Access request notification could not be delivered")
        text = (
            "Заявка на повторное предоставление доступа отправлена администратору"
            if record["resubmitted"]
            else "Заявка на доступ находится на рассмотрении администратора."
        )
        if not settings.admin_ids:
            text += " Администратор пока не настроен."
    else:
        text = {
            "rejected": "Заявка отклонена. Обратитесь к администратору.",
            "revoked": "Доступ отозван. Обратитесь к администратору.",
            "approved": "Доступ одобрен. Откройте /menu.",
        }[record["status"]]
    await update.effective_message.reply_text(text, reply_markup=home_keyboard())


async def show_access(update, context, page=0, *, edit=False):
    if not private_user(update) or update.effective_user.id not in settings.admin_ids:
        return
    records = access_store.list(page)
    lines = ["Доступ пользователей · страница " + str(page + 1)]
    rows = []
    for record in records[:10]:
        lines.append(f"{record['user_id']} · {record['status']} · {record['name']}")
        buttons = decision_buttons(record)
        if buttons and record["user_id"] not in settings.admin_ids:
            rows.append(buttons)
    navigation = []
    if page:
        navigation.append(
            InlineKeyboardButton("←", callback_data=f"access:list:{page - 1}")
        )
    if len(records) > 10:
        navigation.append(
            InlineKeyboardButton("→", callback_data=f"access:list:{page + 1}")
        )
    if navigation:
        rows.append(navigation)
    rows.append([InlineKeyboardButton("Обновить", callback_data=f"access:list:{page}")])
    markup = InlineKeyboardMarkup(rows)
    if edit:
        await edit_message_text(
            update.callback_query, "\n".join(lines), reply_markup=markup
        )
    else:
        await update.effective_message.reply_text("\n".join(lines), reply_markup=markup)


async def handle_access_button(update, context):
    query = update.callback_query
    if not query:
        return
    if not private_user(update) or update.effective_user.id not in settings.admin_ids:
        await query.answer(
            "Только администратор может управлять доступом.", show_alert=True
        )
        return
    parts = (query.data or "").split(":")
    try:
        if len(parts) == 3 and parts[:2] == ["access", "list"] and parts[2].isdigit():
            await query.answer()
            await show_access(update, context, int(parts[2]), edit=True)
            return
        if (
            len(parts) != 4
            or parts[0] != "access"
            or parts[1] not in {"approve", "reject", "revoke"}
            or not all(value.isdigit() for value in parts[2:])
        ):
            raise ValueError("Некорректная кнопка доступа.")
        record = access_store.decide(
            update.effective_user.id, int(parts[2]), parts[1], int(parts[3])
        )
    except (ValueError, PermissionError) as exc:
        await query.answer(str(exc), show_alert=True)
        return
    text = {
        "approved": "Доступ к OPS Bot одобрен. Выберите раздел.",
        "rejected": "Заявка на доступ к OPS Bot отклонена.",
        "revoked": "Доступ к OPS Bot отозван.",
    }[record["status"]]
    try:
        await query.answer("Решение сохранено.")
        await edit_message_text(
            query, f"Пользователь {record['user_id']} · {record['status']}"
        )
    finally:
        try:
            await context.bot.send_message(
                chat_id=record["user_id"],
                text=text,
                reply_markup=home_keyboard()
                if record["status"] == "approved"
                else None,
            )
        except TelegramError:
            logger.warning(
                "Access decision saved; user notification could not be delivered"
            )
