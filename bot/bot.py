import asyncio
import logging
import re
from html import escape
from time import perf_counter
from weakref import WeakValueDictionary

from httpx import HTTPError
from loguru import logger as timing_logger
from ollama import ResponseError
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

from agent.core import agent
from bot.commands import (
    COMMANDS,
    MENU_GROUPS,
    execute_command,
    help_text,
    menu_keyboard,
    natural_foreflight_command,
    telegram_commands,
)
from bot.flight_browser import handle_selection, invalidate_screen, show_week
from bot.keyboards import SCHEDULE_BUTTON, home_keyboard
from config.access import current_chat_id, current_user_id
from config.settings import settings
from integrations.foreflight.api_client import ForeFlightError
from services.operational_routes import route_operation
from services.schedule_dates import parse_schedule_period

logger = logging.getLogger(__name__)
_conversation_locks = WeakValueDictionary()

BOT_USERNAME = settings.telegram_bot_username

MENTION_PATTERN = re.compile(
    rf"(?<!\w)@{re.escape(BOT_USERNAME)}\b",
    re.IGNORECASE,
)


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
            or extract_question(message.text) is not None
            or pending_reply(update, context)
        )
    )


async def prompt_for_input(update, context, action):
    if not is_allowed(update):
        return
    text = (
        COMMANDS[action].prompt
        if action != "ai"
        else "Напишите вопрос для ИИ. Для операционных данных используйте /menu."
    )
    user = update.effective_user
    name = escape(getattr(user, "first_name", None) or "Пользователь")
    prompt = await update.effective_message.reply_text(
        f'<a href="tg://user?id={user.id}">{name}</a>, {escape(text)}\nОтветьте на это сообщение. /cancel — отмена.',
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
        await query.answer()
        await weekly_flow(update, context, selection=True)
        return
    invalidate_screen(update, context)
    action = (query.data or "").removeprefix("op:")
    if action == "menu" or action.startswith("group_"):
        group = action.removeprefix("group_") if action != "menu" else None
        if group is not None and group not in MENU_GROUPS:
            await query.answer("Раздел не найден.")
            return
        await query.answer()
        await query.edit_message_text(
            "OPS Bot · выберите действие", reply_markup=menu_keyboard(group)
        )
        return
    if action == "stations":
        await query.answer()
        await update.effective_message.reply_text(
            "Откройте SKED → Flight Card → WX или NOTAM. Личные заметки в текущем режиме отключены."
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
    if action not in COMMANDS and action != "ai":
        await query.answer("Откройте новое меню: /menu", show_alert=True)
        return
    await query.answer()
    key = (update.effective_chat.id, update.effective_user.id)
    lock = _conversation_locks.setdefault(key, asyncio.Lock())
    async with lock:
        await prompt_for_input(update, context, action)


async def weekly_flow(update, context, *, selection=False):
    """All weekly entry points share the same authorized private-chat context."""
    if not is_allowed(update):
        return
    user_context = current_user_id.set(update.effective_user.id)
    chat_context = current_chat_id.set(update.effective_chat.id)
    try:
        operation = (
            handle_selection(update, context)
            if selection
            else show_week(update, context, edit=True)
            if update.callback_query and update.callback_query.data
            else show_week(update, context)
        )
        await asyncio.wait_for(operation, timeout=120)
    except ValueError as exc:
        await update.effective_message.reply_text(
            str(exc), reply_markup=home_keyboard()
        )
    except TimeoutError:
        await update.effective_message.reply_text(
            "Истекло время ожидания Leon. Попробуйте ещё раз.",
            reply_markup=home_keyboard(),
        )
    except Exception:
        logger.exception("Weekly schedule request failed")
        await update.effective_message.reply_text(
            "Не удалось загрузить расписание или карточку рейса. Попробуйте позже.",
            reply_markup=home_keyboard(),
        )
    finally:
        current_chat_id.reset(chat_context)
        current_user_id.reset(user_context)


def is_allowed(update: Update) -> bool:
    chat = update.effective_chat
    user = update.effective_user

    if not chat or not user:
        return False

    if chat.type != "private":
        return False

    if chat.id != user.id or user.id not in settings.allowed_user_ids:
        return False

    return not user.is_bot


def extract_question(text: str) -> str | None:
    if text.lstrip().startswith(("/", "@")) and not MENTION_PATTERN.search(text):
        return None
    return MENTION_PATTERN.sub("", text).strip()


def extract_date(text: str) -> str | None:
    """Compatibility helper for single-date callers."""
    try:
        period = parse_schedule_period(text)
    except ValueError:
        return None
    if period and period.start == period.end:
        return period.start.isoformat()
    return None


def is_schedule_request(text: str) -> bool:
    """Identify schedule requests without hijacking crew queries."""

    lower = text.lower()

    if re.search(
        r"экипаж|пилот|командир|crew|captain",
        lower,
    ):
        return False

    return bool(
        re.search(
            r"расписан|рейсы|список рейсов|"
            r"какие рейсы|покажи рейс|schedule|flights",
            lower,
        )
    )


async def send_answer(message, answer: str):
    answer = answer.strip()

    if not answer:
        answer = "Модель не вернула ответ."

    for index in range(0, len(answer), 3900):
        await message.reply_text(
            answer[index : index + 3900],
            disable_web_page_preview=True,
            reply_markup=home_keyboard(),
        )


async def handle_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not is_allowed(update):
        return
    message = update.effective_message
    if not message or not message.text or not accepts_message(update, context):
        return
    key = (update.effective_chat.id, update.effective_user.id)
    # Keep each user's history ordered while other members proceed independently.
    lock = _conversation_locks.setdefault(key, asyncio.Lock())
    started = perf_counter()
    async with lock:
        try:
            await _handle_message(update, context)
        finally:
            timing_logger.info(
                "Telegram request duration_s={:.2f}", perf_counter() - started
            )


async def _handle_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not is_allowed(update):
        return

    message = update.effective_message

    if not message or not message.text:
        return

    if message.text.strip() == SCHEDULE_BUTTON:
        context.user_data.setdefault("operation_inputs", {}).pop(
            pending_key(update), None
        )
        await weekly_flow(update, context)
        return

    action = None
    command = parsed_command(message.text)
    if command:
        action, question = command
        if action in {"week", "sked"}:
            context.user_data.setdefault("operation_inputs", {}).pop(
                pending_key(update), None
            )
            await weekly_flow(update, context)
            return
        if action in {"start", "menu", "help"}:
            invalidate_screen(update, context)
            await message.reply_text(
                help_text()
                if action == "help"
                else "SKED → выберите рейс → Flight Card.",
                reply_markup=home_keyboard(),
            )
            return
        if action == "cancel":
            context.user_data.setdefault("operation_inputs", {}).pop(
                pending_key(update), None
            )
            await message.reply_text(
                "Ввод отменён. /menu — основные действия.", reply_markup=home_keyboard()
            )
            return
        if action not in COMMANDS and action != "ai":
            await message.reply_text("Неизвестная команда. /help — список команд.")
            return
        context.user_data.setdefault("operation_inputs", {}).pop(
            pending_key(update), None
        )
        if not question:
            await prompt_for_input(update, context, action)
            return
    else:
        pending = pending_reply(update, context)
        if pending:
            context.user_data["operation_inputs"].pop(pending_key(update), None)
            if pending["expires"] < perf_counter():
                await message.reply_text(
                    "Время ввода истекло. Выберите действие снова: /menu."
                )
                return
            action, question = pending["action"], message.text.strip()
        else:
            question = extract_question(message.text)
            if question is None:
                return

    if not question:
        await message.reply_text("Напишите вопрос.")
        return

    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    try:
        await context.bot.send_chat_action(
            chat_id=chat_id,
            action=ChatAction.TYPING,
            message_thread_id=message.message_thread_id,
        )

        # Provide the user identity to Leon tools.
        access_context = current_user_id.set(user_id)
        chat_context = current_chat_id.set(chat_id)

        try:
            if action == "ai":
                answer = await agent.chat(
                    message=question, chat_id=chat_id, user_id=user_id
                )
            elif action == "search":
                await asyncio.wait_for(
                    show_week(update, context, search=question), timeout=120
                )
                return
            elif action or natural_foreflight_command(question):
                answer = await asyncio.wait_for(
                    execute_command(
                        action or natural_foreflight_command(question), question
                    ),
                    timeout=120,
                )
            else:
                answer = await answer_natural(question, chat_id, user_id)

        finally:
            current_chat_id.reset(chat_context)
            current_user_id.reset(access_context)

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
        await message.reply_text("Ошибка обработки запроса. Проверьте консоль.")


async def answer_natural(question, chat_id, user_id):
    answer = await asyncio.wait_for(route_operation(question), timeout=120)
    if answer is not None:
        return answer
    if is_schedule_request(question):
        return await asyncio.wait_for(execute_command("flights", question), timeout=120)
    return await agent.chat(message=question, chat_id=chat_id, user_id=user_id)


async def warmup_model(application: Application):
    global BOT_USERNAME, MENTION_PATTERN
    BOT_USERNAME = application.bot.username
    MENTION_PATTERN = re.compile(rf"(?<!\w)@{re.escape(BOT_USERNAME)}\b", re.IGNORECASE)
    try:
        await application.bot.set_my_commands(
            telegram_commands(), scope=BotCommandScopeAllPrivateChats()
        )
    except TelegramError as exc:
        timing_logger.warning(
            "Telegram command menu setup failed: {}", type(exc).__name__
        )
    try:
        await asyncio.wait_for(agent.warmup(), timeout=60)
    except (TimeoutError, OSError, HTTPError, ResponseError) as exc:
        timing_logger.warning("Ollama warmup failed: {}", type(exc).__name__)


def create_application() -> Application:
    if not settings.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN not configured")

    token = settings.telegram_bot_token.get_secret_value()

    application = (
        Application.builder()
        .token(token)
        .concurrent_updates(settings.telegram_max_parallel)
        .post_init(warmup_model)
        .build()
    )

    application.add_handler(
        CallbackQueryHandler(handle_button, pattern=r"^(?:op:|wk:|fl:|fc:|sk:|sv:)")
    )
    application.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & filters.TEXT,
            handle_message,
        )
    )

    return application


def run_bot():
    application = create_application()

    print("=" * 45)
    print("OPS Bot")
    print("=" * 45)
    print(f"Model: {settings.ollama_model}")
    print(f"Ollama: {settings.ollama_host}")
    print(f"Bot: @{BOT_USERNAME}")
    print("Mode: authorized private chats only")
    print("Memory: 10 messages per user")
    print("Leon MCP: schedule direct routing")
    print("=" * 45)

    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    run_bot()
