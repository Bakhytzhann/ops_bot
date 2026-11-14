"""Private, authorized Telegram adapter for button-driven operations."""

import asyncio
import logging
import re
from html import escape
from time import perf_counter
from weakref import WeakValueDictionary

from loguru import logger as timing_logger
from telegram import BotCommandScopeAllPrivateChats, ForceReply, Update
from telegram.constants import ChatAction
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from bot.admission import (
    handle_access_button,
    is_allowed,
    private_user,
    request_access,
    show_access,
)
from bot.commands import (
    COMMANDS,
    MENU_GROUPS,
    execute_command,
    help_text,
    menu_keyboard,
    telegram_commands,
)
from bot.flight_browser import (
    handle_selection,
    invalidate_screen,
    is_card_notice_callback,
    show_week,
)
from bot.keyboards import HOME_SECTIONS, SCHEDULE_BUTTON, home_keyboard
from bot.message_edits import edit_message_text
from config.settings import settings
from integrations.foreflight.api_client import ForeFlightError

logger = logging.getLogger(__name__)
_conversation_locks = WeakValueDictionary()
BOT_USERNAME = settings.telegram_bot_username


def parsed_command(text):
    match = re.match(
        r"^/([a-z_]+)(?:@([\w]+))?(?:\s+(.*))?$",
        text.strip(),
        re.IGNORECASE | re.DOTALL,
    )
    if not match or (match[2] and match[2].casefold() != BOT_USERNAME.casefold()):
        return None
    return match[1].lower(), (match[3] or "").strip()


def pending_key(update):
    return (
        update.effective_chat.id,
        getattr(update.effective_message, "message_thread_id", None),
    )


def pending_reply(update, context):
    pending = (
        (getattr(context, "user_data", None) or {})
        .get("operation_inputs", {})
        .get(pending_key(update))
    )
    reply = getattr(update.effective_message, "reply_to_message", None)
    if pending and reply and reply.message_id == pending["message_id"]:
        return pending
    return None


def accepts_message(update, context):
    message = update.effective_message
    return bool(
        message
        and message.text
        and (
            parsed_command(message.text)
            or message.text.strip() == SCHEDULE_BUTTON
            or message.text.strip() in HOME_SECTIONS
            or pending_reply(update, context)
        )
    )


async def prompt_for_input(update, context, action):
    if not is_allowed(update) or action not in COMMANDS:
        return
    user = update.effective_user
    name = escape(getattr(user, "first_name", None) or "Пользователь")
    prompt = await update.effective_message.reply_text(
        f'<a href="tg://user?id={user.id}">{name}</a>, {escape(COMMANDS[action].prompt)}\nОтветьте на это сообщение. /cancel — отмена.',
        parse_mode="HTML",
        reply_markup=ForceReply(selective=True),
    )
    context.user_data.setdefault("operation_inputs", {})[pending_key(update)] = {
        "action": action,
        "message_id": prompt.message_id,
        "expires": perf_counter() + 600,
    }


async def handle_button(update, context):
    query = update.callback_query
    if not query:
        return
    if not is_allowed(update):
        await query.answer(
            "Доступ разрешён только в личном чате для авторизованных пользователей.",
            show_alert=True,
        )
        return
    if (query.data or "").startswith(("wk:", "fl:", "fc:", "sk:", "sv:")):
        if not is_card_notice_callback(query.data):
            await query.answer()
        await weekly_flow(update, context, selection=True)
        return
    action = (query.data or "").removeprefix("op:")
    if (
        action not in COMMANDS
        and action not in {"menu", "week", "help"}
        and not action.startswith("group_")
    ):
        await query.answer("Откройте новое меню: /menu", show_alert=True)
        return
    invalidate_screen(update, context)
    context.user_data.setdefault("operation_inputs", {}).pop(pending_key(update), None)
    if action == "menu" or action.startswith("group_"):
        group = action.removeprefix("group_") if action != "menu" else None
        if group is not None and group not in MENU_GROUPS:
            await query.answer("Раздел не найден.")
            return
        await query.answer()
        await edit_message_text(
            query, "OPS Bot · выберите действие", reply_markup=menu_keyboard(group)
        )
        return
    if action == "week":
        await query.answer()
        await weekly_flow(update, context)
        return
    if action == "help":
        await query.answer()
        await update.effective_message.reply_text(
            help_text(), reply_markup=menu_keyboard()
        )
        return
    await query.answer()
    key = (update.effective_chat.id, update.effective_user.id)
    lock = _conversation_locks.setdefault(key, asyncio.Lock())
    async with lock:
        await prompt_for_input(update, context, action)


async def weekly_flow(update, context, *, selection=False):
    if not is_allowed(update):
        return
    try:
        operation = (
            handle_selection(update, context)
            if selection
            else show_week(
                update,
                context,
                edit=bool(
                    getattr(update, "callback_query", None)
                    and update.callback_query.data
                ),
            )
        )
        await asyncio.wait_for(operation, timeout=120)
    except ValueError as exc:
        if selection and is_card_notice_callback(update.callback_query.data):
            await update.callback_query.answer(str(exc), show_alert=True)
        else:
            await update.effective_message.reply_text(
                str(exc), reply_markup=home_keyboard()
            )
    except TimeoutError:
        await update.effective_message.reply_text(
            "Истекло время ожидания источников. Попробуйте ещё раз.",
            reply_markup=home_keyboard(),
        )
    except Exception:
        logger.exception("Pre-flight request failed")
        await update.effective_message.reply_text(
            "Не удалось загрузить расписание или карточку рейса. Попробуйте позже.",
            reply_markup=home_keyboard(),
        )


async def send_answer(message, answer):
    answer = answer.strip() or "Источник не предоставил результат."
    for index in range(0, len(answer), 3900):
        await message.reply_text(
            answer[index : index + 3900],
            disable_web_page_preview=True,
            reply_markup=home_keyboard(),
        )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not private_user(update):
        return
    message = update.effective_message
    if not message or not message.text:
        return
    key = (update.effective_chat.id, update.effective_user.id)
    lock = _conversation_locks.setdefault(key, asyncio.Lock())
    async with lock:
        command = parsed_command(message.text)
        if not is_allowed(update):
            if command and command[0] == "start":
                await request_access(update, context)
            return
        if not accepts_message(update, context):
            return
        started = perf_counter()
        try:
            await _handle_message(update, context)
        finally:
            timing_logger.info(
                "Telegram request duration_s={:.2f}", perf_counter() - started
            )


async def _handle_message(update, context):
    if not is_allowed(update):
        return
    message = update.effective_message
    if not message or not message.text:
        return
    command = parsed_command(message.text)
    text = message.text.strip()
    if text == SCHEDULE_BUTTON or (command and command[0] in {"week", "sked"}):
        context.user_data.setdefault("operation_inputs", {}).pop(
            pending_key(update), None
        )
        await weekly_flow(update, context)
        return
    if text in HOME_SECTIONS or (
        command and command[0] in {"start", "menu", "help", "cancel", "access"}
    ):
        context.user_data.setdefault("operation_inputs", {}).pop(
            pending_key(update), None
        )
        invalidate_screen(update, context)
        if command and command[0] == "access":
            await show_access(update, context)
        elif text in HOME_SECTIONS:
            await message.reply_text(
                text, reply_markup=menu_keyboard(HOME_SECTIONS[text])
            )
        elif command[0] == "help":
            await message.reply_text(help_text(), reply_markup=menu_keyboard())
        elif command[0] == "menu":
            await message.reply_text(
                "OPS Bot · SKED и предполётная подготовка.",
                reply_markup=menu_keyboard(),
            )
        elif command[0] == "cancel":
            await message.reply_text(
                "Ввод отменён. Выберите раздел.", reply_markup=home_keyboard()
            )
        else:
            await message.reply_text(
                "OPS Bot · SKED → рейс → Flight Card. Выберите раздел.",
                reply_markup=home_keyboard(),
            )
        return
    if command:
        action, arguments = command
        if action not in COMMANDS:
            await message.reply_text("Неизвестная команда. /menu — доступные операции.")
            return
        context.user_data.setdefault("operation_inputs", {}).pop(
            pending_key(update), None
        )
        if not arguments:
            await prompt_for_input(update, context, action)
            return
    else:
        pending = pending_reply(update, context)
        if not pending:
            return
        context.user_data["operation_inputs"].pop(pending_key(update), None)
        if pending["expires"] < perf_counter():
            await message.reply_text(
                "Время ввода истекло. Выберите действие снова: /menu."
            )
            return
        action, arguments = pending["action"], text
        if action not in COMMANDS:
            await message.reply_text("Операция больше недоступна. Откройте /menu.")
            return
    try:
        await context.bot.send_chat_action(
            chat_id=update.effective_chat.id, action=ChatAction.TYPING
        )
        if action == "search":
            await asyncio.wait_for(
                show_week(update, context, search=arguments), timeout=120
            )
            return
        answer = await asyncio.wait_for(execute_command(action, arguments), timeout=120)
        if is_allowed(update):
            await send_answer(message, answer)
    except ForeFlightError as exc:
        await message.reply_text(
            f"ForeFlight: запрос не выполнен (HTTP {exc.status_code or 'нет ответа'}). Проверьте Dispatch или повторите позже."
        )
    except ValueError as exc:
        await message.reply_text(str(exc))
    except TimeoutError:
        await message.reply_text(
            "Истекло время ожидания ответа. Попробуйте позже или сократите период."
        )
    except PermissionError:
        await message.reply_text("Доступ к операционным данным запрещён.")
    except Exception:
        logger.exception("Telegram request failed")
        await message.reply_text("Ошибка обработки запроса. Попробуйте позже.")


async def setup_telegram(application):
    global BOT_USERNAME
    BOT_USERNAME = application.bot.username
    # Import legacy allowlist into persistent storage before serving updates.
    from bot.access import access_store

    for user in settings.allowed_user_ids:
        access_store.get(user)
    if not settings.admin_ids:
        timing_logger.warning(
            "TELEGRAM_ADMIN_IDS is empty; admission decisions are unavailable"
        )
    try:
        await application.bot.set_my_commands(
            telegram_commands(), scope=BotCommandScopeAllPrivateChats()
        )
    except TelegramError as exc:
        timing_logger.warning(
            "Telegram command menu setup failed: {}", type(exc).__name__
        )


def create_application():
    if not settings.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN not configured")
    application = (
        Application.builder()
        .token(settings.telegram_bot_token.get_secret_value())
        .concurrent_updates(settings.telegram_max_parallel)
        .post_init(setup_telegram)
        .build()
    )
    application.add_handler(
        CallbackQueryHandler(handle_access_button, pattern=r"^access:")
    )
    application.add_handler(CallbackQueryHandler(handle_button))
    application.add_handler(
        MessageHandler(filters.ChatType.PRIVATE & filters.TEXT, handle_message)
    )
    return application


def run_bot():
    application = create_application()
    print("OPS Bot · private authorized chats · button-driven operations")
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    run_bot()
