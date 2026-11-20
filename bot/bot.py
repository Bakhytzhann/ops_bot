"""Private, authorized Telegram adapter for button-driven operations."""

import asyncio
import logging
import re
from hashlib import sha256
from html import escape
from time import perf_counter

from loguru import logger as timing_logger
from telegram import BotCommandScopeAllPrivateChats, ForceReply, Update
from telegram.constants import ChatAction
from telegram.error import Conflict, TelegramError
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
from bot.analytics import analytics_callback, show_analytics
from bot.charter_check import charter_callback, show_charter
from bot.charter_quote import quote_callback
from bot.commands import (
    COMMANDS,
    execute_command,
    help_text,
)
from bot.fleet_docs import fleet_docs_keyboard, fleet_docs_response
from bot.flight_browser import (
    accepts_sked_number,
    select_sked_number,
    handle_selection,
    invalidate_screen,
    is_card_notice_callback,
    show_week,
)
from bot.ground_turns import (
    clear_ground_turn_input,
    deactivate_ground,
    ground_text_accepted,
    handle_ground_text,
    ground_turn_callback,
    handle_ground_turn_input,
    has_ground_turn_input,
    show_ground_turns,
)
from bot.keyboards import SCHEDULE_BUTTON
from bot.main_screen import HOME_ACTIONS, dispatch_screen
from bot.message_edits import edit_message_text
from bot.ops_brief import ops_brief_callback, show_ops_brief
from config.instance_lock import windows_instance
from config.settings import settings
from integrations.foreflight.api_client import ForeFlightError

logger = logging.getLogger(__name__)
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
    if pending and (
        (reply and reply.message_id == pending["message_id"])
        or getattr(update, "main_screen", None) is not None
    ):
        return pending
    return None


def accepts_message(update, context):
    message = update.effective_message
    return bool(
        message
        and message.text
        and (
            parsed_command(message.text)
            or message.text.strip() in HOME_ACTIONS
            or pending_reply(update, context)
            or has_ground_turn_input(context)
            or ground_text_accepted(context, message.text)
            or accepts_sked_number(update, context)
        )
    )


async def prompt_for_input(update, context, action):
    if not is_allowed(update) or action not in COMMANDS:
        return
    user = update.effective_user
    name = escape(getattr(user, "first_name", None) or "Пользователь")
    instruction = (
        "Введите ответ в поле сообщения. Кнопка «Отмена» — отмена."
        if getattr(update, "main_screen", None) is not None
        else "Ответьте на это сообщение. /cancel — отмена."
    )
    prompt = await update.effective_message.reply_text(
        f'<a href="tg://user?id={user.id}">{name}</a>, {escape(COMMANDS[action].prompt)}\n{instruction}',
        parse_mode="HTML",
        reply_markup=ForceReply(selective=True),
    )
    screen = getattr(update, "main_screen", None)
    if prompt is None or (
        screen is not None
        and screen.generation != context.user_data.get("screen_generation", 0)
    ):
        return
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
    if (query.data or "").startswith("opb:"):
        await ops_brief_callback(update, context)
        return
    if (query.data or "").startswith("ana:"):
        await analytics_callback(update, context)
        return
    if (query.data or "").startswith("quote:"):
        await quote_callback(update, context)
        return
    if (query.data or "").startswith("char:"):
        await charter_callback(update, context)
        return
    if (query.data or "").startswith("gt:"):
        await ground_turn_callback(update, context)
        return
    if (query.data or "").startswith("docs:"):
        category = query.data.removeprefix("docs:")
        try:
            response = fleet_docs_response(category)
        except ValueError:
            await query.answer("Раздел не найден.", show_alert=True)
            return
        invalidate_screen(update, context)
        context.user_data.setdefault("operation_inputs", {}).pop(
            pending_key(update), None
        )
        await query.answer()
        await update.effective_message.reply_text(
            response, reply_markup=fleet_docs_keyboard()
        )
        return
    parts = (query.data or "").split(":")
    if len(parts) == 5 and parts[0] == "fc" and parts[3] in {"summary", "report"}:
        await query.answer("Эта кнопка больше недоступна.", show_alert=True)
        return
    if (query.data or "").startswith(("wk:", "fl:", "fc:", "sk:", "sv:")):
        if not is_card_notice_callback(query.data):
            await query.answer()
        await weekly_flow(update, context, selection=True)
        return
    action = (query.data or "").removeprefix("op:")
    if action == "cancel":
        context.user_data.setdefault("operation_inputs", {}).pop(
            pending_key(update), None
        )
        await query.answer()
        await edit_message_text(query, "Ввод отменён.")
        return
    if action == "week":
        await query.answer()
        await weekly_flow(update, context)
        return
    await query.answer("Эта кнопка больше недоступна.", show_alert=True)
    return


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
            await update.effective_message.reply_text(str(exc))
    except TimeoutError:
        await update.effective_message.reply_text(
            "Истекло время ожидания источников. Попробуйте ещё раз.",
        )
    except Exception:
        logger.exception("Pre-flight request failed")
        await update.effective_message.reply_text(
            "Не удалось загрузить расписание или карточку рейса. Попробуйте позже.",
        )


async def send_answer(message, answer):
    answer = answer.strip() or "Источник не предоставил результат."
    screen = getattr(message, "main_screen", None)
    if screen is not None:
        await screen.render(answer, disable_web_page_preview=True)
        return
    for index in range(0, len(answer), 3900):
        await message.reply_text(
            answer[index : index + 3900],
            disable_web_page_preview=True,
        )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not private_user(update):
        return
    message = update.effective_message
    if not message or not message.text:
        return
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
    if text in HOME_ACTIONS or command:
        if text != "SKED" and not (command and command[0] in {"week", "sked"}):
            context.user_data.pop("active_sked", None)
        clear_ground_turn_input(context)
        if text != "GROUND STATUS":
            deactivate_ground(context)
    if text == "OPS BRIEF":
        await show_ops_brief(update, context)
        return
    if text == "ANALYTICS":
        await show_analytics(update, context)
        return
    if text == "CHARTER CHECK":
        await show_charter(update, context)
        return
    if text == "GROUND STATUS":
        await show_ground_turns(update, context)
        return
    if text == SCHEDULE_BUTTON or (command and command[0] in {"week", "sked"}):
        context.user_data.setdefault("operation_inputs", {}).pop(
            pending_key(update), None
        )
        await weekly_flow(update, context)
        return
    if text == "FLEET DOCS" or (
        command and command[0] in {"start", "menu", "help", "cancel", "access"}
    ):
        context.user_data.setdefault("operation_inputs", {}).pop(
            pending_key(update), None
        )
        invalidate_screen(update, context)
        if command and command[0] == "access":
            await show_access(update, context)
        elif text == "FLEET DOCS":
            await message.reply_text(
                "FLEET DOCS · выберите раздел", reply_markup=fleet_docs_keyboard()
            )
        elif command and command[0] == "cancel":
            await message.reply_text("Ввод отменён.")
        elif command and command[0] == "help":
            await message.reply_text(help_text())
        elif command and command[0] == "menu":
            await message.reply_text("Выберите раздел на клавиатуре.")
        # /start only installs the persistent keyboard; no duplicate reply.
        return
    if not command and has_ground_turn_input(context):
        await handle_ground_turn_input(update, context)
        return
    if not command and accepts_sked_number(update, context):
        await select_sked_number(update, context)
        return
    if not command and ground_text_accepted(context, text):
        await handle_ground_text(update, context)
        return
    if command:
        action, arguments = command
        if action not in COMMANDS:
            await message.reply_text("Команда недоступна.")
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
            await message.reply_text("Время ввода истекло. Выберите действие заново.")
            return
        action, arguments = pending["action"], text
        if action not in COMMANDS:
            await message.reply_text("Операция больше недоступна.")
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
    from bot.access import access_store

    with access_store.connect():
        pass
    if not settings.admin_ids:
        timing_logger.warning(
            "TELEGRAM_ADMIN_IDS is empty; admission decisions are unavailable"
        )
    try:
        await application.bot.delete_my_commands()
        await application.bot.delete_my_commands(scope=BotCommandScopeAllPrivateChats())
    except TelegramError as exc:
        timing_logger.warning(
            "Telegram command menu setup failed: {}", type(exc).__name__
        )


async def screen_message_handler(update, context):
    pending = context.user_data.get("operation_inputs", {}).get(pending_key(update))
    if not is_allowed(update) or (not accepts_message(update, context) and not pending):
        return await handle_message(update, context)
    return await dispatch_screen(update, context, handle_message)


async def screen_button_handler(update, context):
    return await dispatch_screen(update, context, handle_button)


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
    application.add_handler(CallbackQueryHandler(screen_button_handler))
    application.add_handler(
        MessageHandler(filters.ChatType.PRIVATE & filters.TEXT, screen_message_handler)
    )
    return application


async def managed_polling(application, wait_for_stop, on_ready):
    updater = application.updater
    stop_requested = asyncio.Event()
    conflict = None

    def polling_error(error):
        nonlocal conflict
        if isinstance(error, Conflict):
            conflict = error
            stop_requested.set()
        else:
            logger.warning("Telegram polling error: %s", type(error).__name__)

    loop = asyncio.get_running_loop()

    async def poll():
        async with application:
            await application.post_init(application)
            if stop_requested.is_set():
                return
            try:
                await updater.start_polling(
                    allowed_updates=Update.ALL_TYPES,
                    drop_pending_updates=True,
                    error_callback=polling_error,
                )
                await application.start()
                if not stop_requested.is_set():
                    on_ready()
                await stop_requested.wait()
            finally:
                try:
                    if updater.running:
                        await updater.stop()
                finally:
                    if application.running:
                        await application.stop()

    wait_for_stop(lambda: loop.call_soon_threadsafe(stop_requested.set))
    await poll()
    if conflict is not None:
        raise conflict


def run_bot(*, watch_for_stop=None, on_ready=None):
    application = create_application()
    # Regenerating a token leaves the bot ID intact.
    bot_id = application.bot.token.partition(":")[0]
    lock_name = sha256(bot_id.encode()).hexdigest()
    with windows_instance(f"Global\\OPSBot.Polling.{lock_name}"):
        if watch_for_stop is None:
            print("OPS Bot · private authorized chats · button-driven operations")
            application.run_polling(
                allowed_updates=Update.ALL_TYPES, drop_pending_updates=True
            )
        else:
            asyncio.run(managed_polling(application, watch_for_stop, on_ready))


if __name__ == "__main__":
    run_bot()
