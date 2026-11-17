"""Route operational Telegram output to one durable message per private chat.

The transport adapters keep the existing flight and command handlers independent
of message ownership. Access notifications remain separate from navigation.
"""

import asyncio
from contextlib import suppress
from secrets import token_hex
from weakref import WeakValueDictionary

from telegram import (
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MessageEntity,
)
from telegram.error import BadRequest, TelegramError

from bot.keyboards import QUICK_ACTIONS, home_keyboard

_write_locks = WeakValueDictionary()
HOME_ACTIONS = {label for row in QUICK_ACTIONS for label in row}


PAGE_SIZE = 3900


def text_pages(text):
    """Respect Telegram's UTF-16 offsets, including emoji and other non-BMP text."""
    pages = []
    start = units = offset = 0
    for index, char in enumerate(text):
        size = 2 if ord(char) > 0xFFFF else 1
        if units + size > PAGE_SIZE:
            pages.append((text[start:index], offset))
            start = index
            offset += units
            units = 0
        units += size
    pages.append((text[start:], offset))
    return pages


class MainScreen:
    def __init__(self, update, context):
        self.context = context
        self.chat_id = update.effective_chat.id
        self.user_id = update.effective_user.id
        self.message_id = context.user_data.get("main_screen_message_id")
        self.generation = context.user_data.get("screen_generation", 0)
        self.keyboard_installed = False

    async def install_keyboard(self):
        # Keep this message: deleting the reply-keyboard carrier can hide the
        # keyboard in Telegram clients. The editable screen is a separate message.
        await self.context.bot.send_message(
            chat_id=self.chat_id,
            text="OPS Bot",
            reply_markup=home_keyboard(),
            disable_notification=True,
        )
        self.keyboard_installed = True
        self.context.user_data["home_keyboard_installed"] = True

    async def render(self, text, **kwargs):
        text = text.strip() or "Источник не предоставил результат."
        markup = kwargs.pop("reply_markup", None)
        if isinstance(markup, ForceReply):
            markup = InlineKeyboardMarkup(
                [[InlineKeyboardButton("Отмена", callback_data="op:cancel")]]
            )
        elif not isinstance(markup, InlineKeyboardMarkup):
            markup = None
        rows = list(markup.inline_keyboard) if markup else []
        rows = [
            [button for button in row if button.callback_data != "op:menu"]
            for row in rows
        ]
        rows = [row for row in rows if row]
        if self.context.user_data.get("screen_back"):
            rows.append([InlineKeyboardButton("← Назад", callback_data="screen:back")])
        markup = InlineKeyboardMarkup(rows)
        # Generic reports used to be sent as multiple messages. Keep every page
        # available through inline navigation instead of overwriting each chunk.
        pages = text_pages(text)
        token = token_hex(6)
        state = {"token": token, "pages": pages, "markup": markup, "kwargs": kwargs}
        if self.generation != self.context.user_data.get("screen_generation", 0):
            return None
        self.context.user_data["main_screen_pages"] = state
        return await self.page(state, 0)

    async def page(self, state, index):
        if self.generation != self.context.user_data.get("screen_generation", 0):
            return None
        pages = state["pages"]
        state["page"] = index
        rows = [
            [b for b in row if b.callback_data != "screen:back"]
            for row in state["markup"].inline_keyboard
        ]
        rows = [row for row in rows if row]
        if self.context.user_data.get("screen_back"):
            rows.append([InlineKeyboardButton("← Назад", callback_data="screen:back")])
        if len(pages) > 1:
            navigation = []
            if index:
                navigation.append(
                    InlineKeyboardButton(
                        "←", callback_data=f"screen:{state['token']}:{index - 1}"
                    )
                )
            navigation.append(
                InlineKeyboardButton(
                    f"{index + 1}/{len(pages)}",
                    callback_data=f"screen:{state['token']}:{index}",
                )
            )
            if index + 1 < len(pages):
                navigation.append(
                    InlineKeyboardButton(
                        "→", callback_data=f"screen:{state['token']}:{index + 1}"
                    )
                )
            rows.insert(0, navigation)
        text, offset = pages[index]
        kwargs = dict(state["kwargs"])
        if "entities" in kwargs:
            end = offset + len(text.encode("utf-16-le")) // 2
            entities = []
            for entity in kwargs["entities"]:
                start = max(offset, entity.offset)
                stop = min(end, entity.offset + entity.length)
                if start < stop:
                    entities.append(
                        MessageEntity(
                            type=entity.type,
                            offset=start - offset,
                            length=stop - start,
                            url=entity.url,
                            user=entity.user,
                            language=entity.language,
                            custom_emoji_id=entity.custom_emoji_id,
                        )
                    )
            kwargs["entities"] = entities
        return await self._write(
            text, reply_markup=InlineKeyboardMarkup(rows), **kwargs
        )

    async def _write(self, text, **kwargs):
        # Serialize writes, not the upstream API requests. A later button press
        # invalidates earlier responses without holding up new navigation.
        key = (self.chat_id, self.user_id)
        lock = _write_locks.setdefault(key, asyncio.Lock())
        async with lock:
            if self.generation != self.context.user_data.get("screen_generation", 0):
                return None
            self.message_id = self.context.user_data.get("main_screen_message_id")
            bot = self.context.bot
            if self.message_id is not None:
                try:
                    result = await bot.edit_message_text(
                        chat_id=self.chat_id,
                        message_id=self.message_id,
                        text=text,
                        **kwargs,
                    )
                    await self._finish_screen(bot)
                    return result
                except BadRequest as exc:
                    error = str(exc).lower()
                    if error.startswith("message is not modified"):
                        await self._finish_screen(bot)
                        return self
                    if not any(
                        reason in error
                        for reason in (
                            "message to edit not found",
                            "message can't be edited",
                            "message_id_invalid",
                        )
                    ):
                        raise
            elif not self.context.user_data.get("home_keyboard_installed"):
                await self.install_keyboard()
            message = await bot.send_message(chat_id=self.chat_id, text=text, **kwargs)
            self.message_id = message.message_id
            self.context.user_data["main_screen_message_id"] = self.message_id
            await self._finish_screen(bot)
            return message

    async def _finish_screen(self, bot):
        """Retire the last completed screen only after its replacement renders."""
        state = self.context.user_data
        if self.generation != state.get("screen_generation", 0):
            return
        old_id = state.get("last_completed_screen_id")
        state["last_completed_screen_id"] = self.message_id
        state.pop("pending_screen_message_id", None)
        if old_id and old_id != self.message_id:
            # Cleanup must not hide a successfully loaded result.
            with suppress(AttributeError, TelegramError):
                await bot.delete_message(chat_id=self.chat_id, message_id=old_id)


class ScreenMessage:
    def __init__(self, original, screen):
        self.original = original
        self.main_screen = screen

    def __getattr__(self, name):
        return getattr(self.original, name)

    async def reply_text(self, text, **kwargs):
        return await self.main_screen.render(text, **kwargs)


class ScreenCallback:
    def __init__(self, original, screen):
        self.original = original
        self.main_screen = screen

    def __getattr__(self, name):
        return getattr(self.original, name)

    @property
    def message(self):
        return ScreenMessage(self.original.message, self.main_screen)

    async def edit_message_text(self, text, **kwargs):
        return await self.main_screen.render(text, **kwargs)


class ScreenUpdate:
    def __init__(self, original, screen):
        self.original = original
        self.main_screen = screen

    def __getattr__(self, name):
        return getattr(self.original, name)

    @property
    def effective_message(self):
        return ScreenMessage(self.original.effective_message, self.main_screen)

    @property
    def callback_query(self):
        query = getattr(self.original, "callback_query", None)
        return ScreenCallback(query, self.main_screen) if query else None


async def dispatch_screen(update, context, handler):
    from bot.admission import is_allowed

    if not is_allowed(update):
        return await handler(update, context)

    query = getattr(update, "callback_query", None)
    text = getattr(update.effective_message, "text", "") or ""
    restarting = (
        not query and text.split(" ", 1)[0].split("@", 1)[0].lower() == "/start"
    )
    known_id = context.user_data.get("main_screen_message_id")
    if query and query.message.message_id != known_id:
        await query.answer(
            "Экран устарел. Выберите раздел на нижней клавиатуре.",
            show_alert=True,
        )
        return

    if query and (query.data or "").startswith("screen:"):
        parts = query.data.split(":")
        if query.data == "screen:back":
            previous = context.user_data.pop("screen_back", None)
            if not previous:
                await query.answer("Предыдущий экран уже недоступен.", show_alert=True)
                return
            context.user_data["screen_generation"] = (
                context.user_data.get("screen_generation", 0) + 1
            )
            screen = MainScreen(update, context)
            context.user_data["main_screen_pages"] = previous
            await query.answer()
            return await screen.page(previous, previous.get("page", 0))
        state = context.user_data.get("main_screen_pages")
        if (
            len(parts) != 3
            or not state
            or parts[1] != state["token"]
            or not parts[2].isdigit()
            or int(parts[2]) >= len(state["pages"])
        ):
            await query.answer(
                "Страница устарела. Откройте раздел снова.", show_alert=True
            )
            return
        context.user_data["screen_generation"] = (
            context.user_data.get("screen_generation", 0) + 1
        )
        screen = MainScreen(update, context)
        await query.answer()
        return await screen.page(state, int(parts[2]))

    if restarting:
        context.user_data.pop("main_screen_message_id", None)
        context.user_data.pop("last_completed_screen_id", None)
        context.user_data.pop("pending_screen_message_id", None)
        for name in (
            "operation_inputs",
            "flight_navigation",
            "main_screen_pages",
            "screen_back",
        ):
            context.user_data.pop(name, None)
    elif not query and text in HOME_ACTIONS:
        previous = context.user_data.get("main_screen_pages")
        if previous:
            # Only one previous screen, in memory. Not a chat history.
            context.user_data["screen_back"] = previous

    if query and (query.data or "").startswith("docs:"):
        previous = context.user_data.get("main_screen_pages")
        if previous:
            context.user_data["screen_back"] = previous
    elif query and (query.data or "").startswith(("fl:", "fc:", "wk:", "sk:", "sv:")):
        # These screens have their own contextual return actions.
        context.user_data.pop("screen_back", None)

    context.user_data["screen_generation"] = (
        context.user_data.get("screen_generation", 0) + 1
    )
    screen = MainScreen(update, context)
    if restarting:
        await screen.install_keyboard()
    elif not query and text in HOME_ACTIONS:
        # Reply keyboard sends a real chat message. Remove only these six
        # service commands; leave ordinary user input and slash commands alone.
        with suppress(AttributeError, TelegramError):
            await context.bot.delete_message(
                chat_id=screen.chat_id, message_id=update.effective_message.message_id
            )

        # A new message puts the active work below the old, still readable
        # result. Keep the previous completed screen until a result is rendered.
        lock = _write_locks.setdefault((screen.chat_id, screen.user_id), asyncio.Lock())
        async with lock:
            if screen.generation != context.user_data.get("screen_generation", 0):
                return
            previous_pending = context.user_data.pop("pending_screen_message_id", None)
            if previous_pending:
                with suppress(TelegramError):
                    await context.bot.delete_message(
                        chat_id=screen.chat_id, message_id=previous_pending
                    )
            if not context.user_data.get("home_keyboard_installed"):
                await screen.install_keyboard()
            status = await context.bot.send_message(
                chat_id=screen.chat_id,
                text=f"{text} · Обработка запроса…",
                disable_notification=True,
            )
            context.user_data["pending_screen_message_id"] = status.message_id
            context.user_data["main_screen_message_id"] = status.message_id
            screen.message_id = status.message_id
    return await handler(ScreenUpdate(update, screen), context)
